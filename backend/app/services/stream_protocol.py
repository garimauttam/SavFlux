"""
stream_protocol.py — the one definition of SavFlux's streaming wire format.

WHY THIS EXISTS
---------------
Every streaming endpoint in the product (single review, multi review, the code
agent, the code writer, chat) interleaves *telemetry* with *prose* in one plain
text body. Until now each producer spelled the framing out by hand:

    __STATUS__Scanning `f.py`...{"step": "starting"}__STATUS_END__     (review)
    __STATUS__{"step": "tool", "message": "..."}__STATUS_END__         (agent)

Two shapes, and five independent client-side parsers to keep in step with them.
The consequences were real: `useChat` splits the two parts with `/\\{[^}]*\\}$/`,
which cannot match a payload containing a nested object — and the review stream
does emit one (`coverage` carries `provider_circuit`). Any client that parsed
strictly (the agent panel did) silently dropped every marker that arrived in the
*other* shape, because the `catch` around `JSON.parse` swallowed it.

So the format lives here. Producers call `status_event()`; they do not hand-roll
markers. Clients get `decode_status()` as the reference for what a decoder must
tolerate, and the test suite pins the round trip.

THE CANONICAL MARKER

    __STATUS__{"step": "tool_done", "message": "...", ...}__STATUS_END__\\n

  * exactly one JSON object between the markers, newline after the closer;
  * `step` is the discriminator (existing consumers switch on it) and `message`
    is the human line — the message is *inside* the object, which is what makes
    one decoder possible for every stream;
  * scalars only, plus bounded `args` / `result` objects — see BOUND below.

LEGACY MARKERS STILL PARSE. `decode_status` accepts the pre-canonical
`text{json}` form and a bare message, so a browser bundle built before this
change keeps labelling a newer server, and vice versa. The legacy prefix is not
produced anywhere any more; it is only understood.

BOUND
-----
A status marker is re-parsed by the client on every streamed chunk and rendered
in a step list, so a payload that carries a file or a diff is pure cost with no
reader benefit. `bound_event()` truncates long strings and caps list lengths, so
a tool that returns something enormous degrades the *display*, not the stream.
`MAX_STATUS_BYTES` is an enforced contract, asserted in the tests.
"""

from __future__ import annotations

import json

STATUS_OPEN = "__STATUS__"
STATUS_CLOSE = "__STATUS_END__"
ERROR_OPEN = "__ERROR__"
ERROR_CLOSE = "__ERROR_END__"
SECTION_OPEN = "__SECTION_START__"
SECTION_CLOSE = "__SECTION_END__"

#: Longest accepted marker, counting the delimiters and the trailing newline.
#: Generous for a telemetry line (a 40-char message plus a dozen scalars is
#: ~200 bytes) and small enough that a runaway list cannot flood the client.
MAX_STATUS_BYTES = 4096

#: Fields that are volatile by construction: a clock and a uuid.
#:
#: A determinism check must ignore exactly these and nothing else, so "which
#: fields may a reproducibility test drop" has one answer in the repo. Adding a
#: field here is a deliberate act — every entry weakens the guarantee.
VOLATILE_STATUS_FIELDS = frozenset({"run_id", "elapsed_ms", "t_ms"})

#: Per-string truncation budget inside a status payload.
MAX_FIELD_CHARS = 180
#: The human line gets a larger budget than a field: the reasoning block is a
#: sentence, and a sentence cut at 180 characters is a sentence with a hole in it.
MAX_MESSAGE_CHARS = 420
#: How many entries of a list are carried into a status payload.
#:
#: 12 because `max_steps` is capped at 12, and the two lists a client must see in
#: full are `plan_steps` and `interrupted` — a plan clipped to six items renders as
#: a four-tool agent. Cosmetic lists (a tool's `preview`) apply their own, smaller
#: cap in the producer, which is where "how much is readable" is actually known.
#: The protocol's job is only to stop a payload being enormous.
MAX_LIST_ITEMS = 12
#: How many keys a marker may carry before the surplus is reported as a count.
MAX_STATUS_FIELDS = 24
#: Keys kept when everything else had to be dropped to meet the byte budget.
ALWAYS_KEEP = ("step", "message", "tool", "step_id", "plan_id")

_TRAILER = "…"


# ── Encoding ──────────────────────────────────────────────────────────────────


def bound_event(payload: dict) -> dict:
    """
    Shrink a status payload so it is safe to inline in a stream.

    Nested dicts are bounded recursively; lists are kept (the UI renders them as
    the result of a tool call) but truncated in both length and element size.
    Anything that is not JSON-serialisable at all becomes its truncated repr —
    a missing field in the UI is worse than an ugly one.
    """
    bounded: dict = {}
    for key, value in payload.items():
        bounded[key] = _bound_value(value, MAX_MESSAGE_CHARS if key == "message" else MAX_FIELD_CHARS)
    dropped = 0
    if len(bounded) > MAX_STATUS_FIELDS:
        dropped = len(bounded) - MAX_STATUS_FIELDS
        keep = {key for key in ALWAYS_KEEP if key in bounded}
        bounded = {key: value for key, value in bounded.items() if key in keep} | dict(
            list(bounded.items())[: MAX_STATUS_FIELDS - len(keep)]
        )
    if dropped:
        bounded["fields_dropped"] = dropped
    return bounded


def _bound_value(value, budget: int):
    if isinstance(value, str):
        return _truncate(value, budget)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(key): _bound_value(item, budget) for key, item in list(value.items())[:16]}
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:MAX_LIST_ITEMS]
        return [_bound_value(item, budget) for item in items]
    return _truncate(repr(value), budget)


def _truncate(text: str, budget: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= budget:
        return text
    return text[: budget - 1] + _TRAILER


def encode_event(payload: dict) -> str:
    """
    Serialise a status payload, guaranteed within `MAX_STATUS_BYTES`.

    Three passes, each strictly smaller than the last: as bounded, then without
    the list fields (a preview is the first thing worth losing), then keeping only
    `ALWAYS_KEEP`. The last pass cannot overflow, because the payload it encodes
    has five keys with bounded values — so the byte budget is a real contract
    rather than an aspiration, and it is asserted in the tests.
    """
    attempts = (
        payload,
        {key: value for key, value in payload.items() if not isinstance(value, (list, tuple))},
        {key: _truncate(str(value), MAX_FIELD_CHARS) for key, value in payload.items()
         if key in ALWAYS_KEEP},
    )
    for index, attempt in enumerate(attempts):
        encoded = _dumps(attempt)
        if len(encoded.encode("utf-8")) <= MAX_STATUS_BYTES or index == len(attempts) - 1:
            return encoded
    raise AssertionError("unreachable: the last pass always returns")


def _dumps(payload: dict) -> str:
    # `ensure_ascii=False`: the messages carry `→`, `·` and `…`, and escaping them
    # costs three bytes each in a line the client re-parses per chunk, for nothing
    # — the body is UTF-8 either way.
    #
    # Default separators (`", "` / `": "`), deliberately: that is the shape every
    # emitter in this repo produced before this module existed, and several tests
    # assert on marker text rather than parsed payloads. Compact JSON would save
    # ~2 bytes per field and break them for no benefit — a client parses this, it
    # does not read it.
    return json.dumps(payload, ensure_ascii=False)


def status_event(message: str = "", **fields) -> str:
    """
    One telemetry line: `__STATUS__{json}__STATUS_END__\\n`.

    `message` is the human-readable line the UI shows; every other keyword is a
    structured field (`step`, `tool`, `args`, `elapsed_ms`, …). Passing an
    explicit `message` in `fields` is not supported — the parameter wins, so a
    caller cannot accidentally emit two of them.
    """
    # `message` is set last so a caller cannot overwrite it through **fields and
    # end up with two of them in one object.
    payload = bound_event({**fields, "message": message})
    return STATUS_OPEN + encode_event(payload) + STATUS_CLOSE + "\n"


def error_event(message: str) -> str:
    """
    A terminal failure notice. Deliberately not JSON: every consumer already
    treats the inside of `__ERROR__…__ERROR_END__` as plain text, and an error
    path is the worst place to introduce a format change.
    """
    return ERROR_OPEN + (message or "stream failed")[:200] + ERROR_CLOSE + "\n"


def section_event(**fields) -> str:
    """
    Open a per-file section on the review stream.

    The payload stays the compact JSON `useMultiReview` already parses, so this
    is a framing helper rather than a format change: one place spells the
    markers out, and the section protocol cannot drift from the status one.
    """
    return SECTION_OPEN + json.dumps(fields, ensure_ascii=False) + SECTION_CLOSE + "\n"


def is_status_marker(chunk: str) -> bool:
    """True for a streamed token that is telemetry rather than prose."""
    return chunk.startswith(STATUS_OPEN)


def is_protocol_token(chunk: str) -> bool:
    """True for any token a consumer should never append to the visible text."""
    return chunk.startswith((STATUS_OPEN, ERROR_OPEN, SECTION_OPEN))


def strip_protocol_markers(text: str) -> str:
    """
    Remove every complete marker from `text`, leaving only prose.

    Used where a stream is consumed as a string instead of a live feed — the
    webhook review, and the tests. Unclosed markers are dropped too: a marker
    with no closer is a truncated telemetry line, never answer text.
    """
    out = text
    for open_tag, close_tag in (
        (STATUS_OPEN, STATUS_CLOSE),
        (ERROR_OPEN, ERROR_CLOSE),
        (SECTION_OPEN, SECTION_CLOSE),
    ):
        while True:
            start = out.find(open_tag)
            if start == -1:
                break
            end = out.find(close_tag, start)
            if end == -1:
                out = out[:start]
                break
            out = out[:start] + out[end + len(close_tag):].lstrip("\n")
    return out


# ── Decoding (reference implementation for clients) ───────────────────────────


def decode_status(payload: str) -> dict:
    """
    Turn the inside of a `__STATUS__…__STATUS_END__` marker into a dict.

    Accepts, in order of preference:
      1. `{"step": …}`            — canonical, `message` inside the object;
      2. `Some text {"step": …}`   — the pre-canonical shape; the text becomes
                                     `message` unless the object carries one, so
                                     an old server and a new server read alike;
      3. `Some text`               — no object at all, message only.

    Never raises: a marker the client cannot understand must degrade to a
    message line, not to a dropped step or an exception in a stream loop.
    """
    raw = (payload or "").strip()
    if not raw:
        return {}

    parsed = _parse_object(raw)
    if parsed is not None:
        parsed.setdefault("message", "")
        return parsed

    # Legacy `text{json}`: take the *last* object that parses, because the text
    # may itself contain braces (markdown, file names) and JSON may nest.
    for index in range(len(raw)):
        if raw[index] != "{":
            continue
        parsed = _parse_object(raw[index:])
        if parsed is not None:
            parsed.setdefault("message", raw[:index].strip())
            return parsed

    return {"message": raw}


def _parse_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def deterministic_view(payload: dict) -> dict:
    """
    A status payload with the volatile fields removed.

    This is what a reproducibility check compares. Kept here rather than in the
    test so the test cannot quietly widen it (the usual failure mode of "compare
    the dicts, minus the ones that differ").
    """
    return {key: value for key, value in payload.items() if key not in VOLATILE_STATUS_FIELDS}


def encode_for_test(payload: dict) -> str:  # pragma: no cover - test helper
    """Marker for tests that build a fake upstream stream by hand."""
    return STATUS_OPEN + json.dumps(payload) + STATUS_CLOSE + "\n"
