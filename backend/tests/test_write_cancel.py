"""
Tests for cancelling a code write that nobody is waiting for.

The writer is the cheaper half of this handshake — one sequential model call, no
fan-out — but it is the call that produces *code*, which means it is the longest one
in the product and the one a user is most likely to give up on. The tests pin the two
claims that matter: a Stop before the call means the model was never asked at all,
and a Stop during it means the provider's stream is actually closed rather than left
to drain into an empty socket.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from app.services.stream_protocol import STATUS_CLOSE, STATUS_OPEN


def _markers_of(text: str) -> list[dict]:
    markers, cursor = [], 0
    while True:
        start = text.find(STATUS_OPEN, cursor)
        if start == -1:
            return markers
        end = text.find(STATUS_CLOSE, start)
        markers.append(json.loads(text[start + len(STATUS_OPEN):end]))
        cursor = end + len(STATUS_CLOSE)


class _FakeStream:
    """
    A stand-in provider stream that counts tokens and notices its own closure.

    `closed` is the interesting part: an early `return` out of an `async for` is
    enough to stop *printing*, but unless the async iterator is aclosed the provider
    connection keeps being read. The flag is what distinguishes those two, and only
    one of them frees the model.
    """

    def __init__(self, total: int, armed_after: int | None = None, state: dict | None = None):
        self.total = total
        self.armed_after = armed_after
        self.state = state if state is not None else {}
        self.state.setdefault("tokens", 0)
        self.closed = False

    def astream(self, messages):
        outer = self

        async def gen():
            try:
                for i in range(outer.total):
                    yield SimpleNamespace(content=f"tok{i} ")
                    outer.state["tokens"] += 1
                    if outer.armed_after is not None and outer.state["tokens"] >= outer.armed_after:
                        outer.state["armed"] = True
            finally:
                outer.closed = True

        return gen()

    def with_config(self, **_):
        return self


def _drive(files_total=60, armed_after=None, should_stop=None, context_sources=(), fake=None,
           sources_touched=None, state=None):
    from app.services import write_agent as wa

    stream = fake or _FakeStream(files_total, armed_after)
    state = state if state is not None else {}

    def _get_vectorstore():
        if sources_touched is not None:
            sources_touched.append(True)

        class _Store:
            @staticmethod
            def _get(**_):
                # The reader leaving "while the context was being fetched" is the
                # interesting case, so the store itself arms the stop.
                if state.get("arm_on_read"):
                    state["armed"] = True
                return {"documents": [], "metadatas": []}

            _collection = SimpleNamespace(get=_get)

        return _Store()

    async def collect():
        with patch.object(wa, "get_chat_llm", lambda *a, **k: stream), \
             patch.object(wa, "get_token_callback", lambda: object()), \
             patch.object(wa, "error_event", lambda msg: f"__ERROR__{msg}"), \
             patch.object(wa, "increment_request", lambda *a, **k: None):
            import app.services.ingestion_service as ing
            with patch.object(ing, "_get_vectorstore", _get_vectorstore):
                return "".join([tok async for tok in wa.stream_code_write(
                    prompt="add validation", language="python", file_name="auth.py",
                    context_sources=list(context_sources), mode="generate",
                    should_stop=should_stop,
                )])

    out = asyncio.run(collect())
    return out, stream


async def _always():
    return True


def test_a_write_stopped_before_it_started_asked_the_model_nothing():
    """Zero tokens, zero vector-store reads, and a marker that says only what is true."""
    touched: list[bool] = []
    out, stream = _drive(should_stop=_always, context_sources=("pkg/a.py",), sources_touched=touched)
    markers = _markers_of(out)

    assert stream.state["tokens"] == 0, "an abandoned write still ran the model"
    assert touched == [], "an abandoned write still read the vector store"
    cancelled = [m for m in markers if m["step"] == "cancelled"]
    assert len(cancelled) == 1
    marker = cancelled[0]
    assert "no context read, no model call" in marker["message"]
    # The honesty rule: a stage that never ran is absent, never reported as 0 ms.
    # `elapsed_ms` is the only duration here, and it is the run's own.
    assert set(marker) == {"step", "message", "mode", "elapsed_ms"}, sorted(marker)
    assert marker["mode"] == "generate"


def test_a_stop_mid_generation_ends_early_and_closes_the_stream():
    """
    The run must stop within a bounded number of tokens and aclose the provider stream.

    600 tokens offered, the reader gone from token 40 on: anything near 600 means the
    cadence check is not running, and `closed` False means the connection is still
    being read by a generator nobody is waiting on.
    """
    state: dict = {"armed": False}

    class _Fake(_FakeStream):
        def __init__(self):
            super().__init__(600, armed_after=40, state=state)

    fake = _Fake()
    # The predicate is asked before the model call and then every 32 tokens; it
    # reports the reader as gone as soon as the fake stream has passed token 40.
    async def stopping():
        return bool(state.get("armed"))

    out, stream = _drive(fake=fake, should_stop=stopping)
    markers = _markers_of(out)
    cancelled = next(m for m in markers if m["step"] == "cancelled")

    assert 40 <= cancelled["tokens"] < 128, cancelled["tokens"]
    assert cancelled["incomplete"] is True
    assert "the file is incomplete" in cancelled["message"]
    assert len(re.findall(r"tok\d+ ", out)) == cancelled["tokens"]
    assert fake.closed, "the provider stream was left open"
    assert "__ERROR__" not in out, "a Stop is not a failure"


def test_a_write_with_no_stop_predicate_is_unchanged():
    """The default path keeps streaming to the end and gains no cancelled marker."""
    out, stream = _drive(files_total=70)
    markers = _markers_of(out)

    assert stream.state["tokens"] == 70
    assert not any(m["step"] == "cancelled" for m in markers)
    assert out.rstrip().endswith("tok69")


def test_a_stopped_write_is_not_reported_as_an_error():
    """
    An abandoned run must not wear the provider-failure marker.

    `__ERROR__` on this stream means "the model could not answer", and the UI renders
    it as a red failure with a retry affordance. A user who pressed Stop does not want
    either of those, and a failure that never happened pollutes the run's statistics.
    """
    out, _stream = _drive(should_stop=_always)

    assert "__ERROR__" not in out
    assert not any(m["step"] == "error" for m in _markers_of(out))


def test_a_stop_while_the_context_is_being_read_skips_the_generation():
    """
    The reader leaves *after* the run started: the stage in flight finishes, the
    model call that would have followed it does not happen.

    The first test's predicate is true from the very first check, so it passes even
    if the guard in front of the generation is deleted — which is the guard that
    matters, because a real Stop always arrives mid-run. This one arms itself from
    inside the vector-store read, so the sequence the writer walks is the ordinary
    one: context marker, context result, then a refusal to generate.
    """
    state = {"arm_on_read": True}
    touched: list[bool] = []
    fake = _FakeStream(200, state=state)

    async def stopping():
        return bool(state.get("armed"))

    out, _stream = _drive(
        fake=fake, should_stop=stopping, context_sources=("pkg/a.py",),
        sources_touched=touched, state=state,
    )
    markers = _markers_of(out)
    cancelled = next(m for m in markers if m["step"] == "cancelled")

    assert touched == [True], "the context read was skipped, so this is not the case it claims"
    assert any(m["step"] == "context" for m in markers)
    assert fake.state["tokens"] == 0, "the generation ran for a reader who had gone"
    assert "before the model call" in cancelled["message"]
    # Nothing about a generation that never started is reported — no token count, no
    # "incomplete" flag, and no duration for it.
    assert "tokens" not in cancelled and "incomplete" not in cancelled
