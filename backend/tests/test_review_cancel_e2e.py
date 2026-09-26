"""
An HTTP client that hangs up mid-stream, against the real ASGI app.

Everything else in cancellation-land is a unit test: a predicate that returns True, a
route that forwards a callable. What those cannot reach is the part that only shows up
on a real server — when a client disconnects from a `StreamingResponse`, Starlette may
*abandon* the response's generator instead of unwinding it, and an abandoned frame runs
no `finally` of mine at all. This drives `main.app` through the raw ASGI interface,
sends `http.disconnect` after the first body chunk, and asks what the app did next.

What this harness can prove is bounded by scheduling: the response's teardown is
requested synchronously but delivered on a later pass of the loop, so the assertions
here are the ones that do not depend on which pass — nobody's review completed for a
reader who had left, and the files queued behind the concurrency semaphore never
started. That a call in flight is torn down rather than left suspended is asserted in
`test_batch_review.py`, where the consumer task is ours to cancel.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

REVIEW_TIMEOUT = 10.0


async def _post_and_hang_up(app, path: str, body: dict, *, hang_up_after: int = 1):
    """POST `body` through ASGI and disconnect once `hang_up_after` chunks arrive."""
    receive_queue: asyncio.Queue = asyncio.Queue()
    await receive_queue.put(
        {"type": "http.request", "body": json.dumps(body).encode("utf-8"), "more_body": False}
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 5555),
        "server": ("testserver", 80),
    }

    chunks: list[bytes] = []
    disconnected = False

    async def receive():
        nonlocal disconnected
        if disconnected:
            # Both Starlette's disconnect listener and `Request.is_disconnected` read
            # this channel, and a message taken by one must not hide the hang-up from
            # the other: a closed socket keeps reporting itself.
            return {"type": "http.disconnect"}
        message = await receive_queue.get()
        if message.get("type") == "http.disconnect":
            disconnected = True
        return message

    async def send(message: dict) -> None:
        nonlocal disconnected
        if message["type"] == "http.response.start":
            return
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not disconnected and len(chunks) >= hang_up_after:
                await receive_queue.put({"type": "http.disconnect"})
                disconnected = True
            return
        raise AssertionError(f"unexpected ASGI message: {message['type']}")

    async def run():
        await app(scope, receive, send)

    return run, chunks


class _Patches:
    """A stack of `patch.object` results, so both tests can share one scenario."""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *_exc):
        for patcher in reversed(self._patches):
            patcher.stop()
        return False


def _sleeping_review(recorder):
    """A per-file review that never finishes on its own, and records how it ended."""

    async def fake(file_name, content, language, repo_context="", model_override=""):  # noqa: ANN001
        recorder["started"].append(file_name)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            recorder["cancelled"].append(file_name)
            raise
        finally:
            recorder["ended"].append(file_name)
        recorder["finished"].append(file_name)
        yield "never reached"

    return fake


def _sleeping_batch(recorder):
    async def fake(files_slice, repo_context_map=None, model_override=""):  # noqa: ANN001
        names = [info["file_name"] for info in files_slice]
        recorder["started"].append(f"batch:{len(names)}")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            recorder["cancelled"].extend(names)
            raise
        finally:
            recorder["ended"].extend(names)
        recorder["finished"].extend(names)
        for info in files_slice:
            yield f"=== FILE: {info['file_name']} ===\n## Security\nnone\n=== END FILE ===\n"

    return fake


def _scenario(tmp_path, *, blind_poll: bool) -> dict:
    """
    Six model-bound files, two at a time, and a client that vanishes after one chunk.

    The files carry security-sensitive constructs *and* enough lines for the planner to
    judge them un-settleable by the parser alone: a file it routes static has no model
    call to cancel, and the run would prove nothing by finishing.
    """
    from main import app
    from app.services import multi_review_agent as mra

    body = ["import hashlib", "import requests", "", "def check(token, secret, user):"]
    for i in range(14):
        body += [
            "    total = 0",
            "    for part in token.split('.'):",
            "        if len(part) > 64 and part.startswith('x'):",
            f"            total += {i}",
            "    if len(secret) < 8:",
            "        raise ValueError('short secret')",
            "    resp = requests.get('http://internal', verify=False)",
            "    return resp.ok and total < 100",
            "",
        ]
    content = "\n".join(body)

    files = []
    for i in range(6):
        path = tmp_path / f"auth{i}.py"
        path.write_text(content, encoding="utf-8")
        files.append({"file_path": str(path), "file_name": path.name, "language": "py"})

    recorder = {"started": [], "cancelled": [], "finished": [], "ended": []}
    settings = SimpleNamespace(
        review_mode="fast", review_max_full_files=80, review_llm_budget=80,
        review_concurrency=2, llm_provider="ollama", summary_mixture_models="",
        review_cache_enabled=False,
    )

    async def scenario():
        patches = [
            patch.object(mra, "stream_fast_code_review", _sleeping_review(recorder)),
            patch.object(mra, "stream_code_review", _sleeping_review(recorder)),
            patch.object(mra, "stream_batch_code_review", _sleeping_batch(recorder)),
            patch.object(mra, "get_settings", lambda: settings),
        ]
        if blind_poll:
            async def _never_disconnected(self):  # noqa: ANN001
                return False

            patches.append(patch.object(Request, "is_disconnected", _never_disconnected))

        with _Patches(patches):
            run, _chunks = await _post_and_hang_up(
                app, "/api/v1/review/multi", {"files": files}, hang_up_after=1,
            )
            # Its own task, as it is in production: the server runs one task per
            # response, and the stream hangs its cleanup off that task finishing.
            # Awaiting the response inline would attach the hook to this test instead.
            await asyncio.wait_for(asyncio.create_task(run()), REVIEW_TIMEOUT)
            await asyncio.sleep(0.1)
            return {key: list(value) for key, value in recorder.items()}

    return asyncio.run(scenario())


def test_a_client_that_hangs_up_changes_the_route_says_nothing_worse(tmp_path, isolated_data_dir):
    """
    The response ends, and no review is completed for the reader who left.

    The route accepting a disconnect mid-stream is the assumption every guard in
    `multi_review_agent` makes; if it instead raised out of the streaming response, or
    ran the whole repo review to completion regardless, that is what this catches.
    """
    run = _scenario(tmp_path, blind_poll=False)

    # The run really reached the model — otherwise "nothing completed" would pass by
    # having done no work at all.
    assert run["started"], run
    # Only what the semaphore allows may be in flight; the queued files never start.
    assert len(run["started"]) <= 2, run["started"]
    assert run["finished"] == [], f"reviews completed for a reader who left: {run['finished']}"


def test_a_disconnect_the_poll_never_sees_completes_no_reviews(tmp_path, isolated_data_dir):
    """
    With `Request.is_disconnected` suppressed, the run still produces no verdicts.

    This is the case the poll-based guards alone cannot serve: `is_disconnected` reads
    the same channel as Starlette's own disconnect listener, so the listener can take
    the message and the predicate stays False for the rest of the response — measured,
    not assumed, in this harness. The route must still neither complete reviews nor
    fail, which is what it does here.
    """
    run = _scenario(tmp_path, blind_poll=True)

    assert run["finished"] == [], run["finished"]
    assert len(run["started"]) <= 2, run["started"]
