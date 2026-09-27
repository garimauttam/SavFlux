"""
Tests for the stream wire format.

The format is what five front-end consumers and six back-end producers agree on,
so these tests are about the agreement rather than any one panel:

  * the encoder emits the canonical shape, and cannot be talked out of it;
  * the decoder understands the legacy shape too, because a browser bundle and a
    server process are not deployed atomically;
  * a marker stays small no matter what a tool returns, because it is re-parsed
    per streamed chunk;
  * the byte budget is enforced, not documented. A limit nobody measures is a wish.
"""

from __future__ import annotations

import json

import pytest

from app.services.stream_protocol import (
    ALWAYS_KEEP,
    ERROR_OPEN,
    MAX_FIELD_CHARS,
    MAX_LIST_ITEMS,
    MAX_MESSAGE_CHARS,
    MAX_STATUS_BYTES,
    STATUS_CLOSE,
    STATUS_OPEN,
    VOLATILE_STATUS_FIELDS,
    bound_event,
    decode_status,
    deterministic_view,
    encode_event,
    error_event,
    is_protocol_token,
    is_status_marker,
    section_event,
    status_event,
    strip_protocol_markers,
)


def _payload(marker: str, open_tag: str = STATUS_OPEN, close_tag: str = STATUS_CLOSE) -> dict:
    assert marker.startswith(open_tag) and marker.endswith(close_tag + "\n")
    return json.loads(marker[len(open_tag):-len(close_tag) - 1])


# ── Encoding ──────────────────────────────────────────────────────────────────


def test_canonical_marker_is_one_json_object_with_the_message_inside():
    marker = status_event("Scanning `a.py`", step="starting", mode="fast")
    assert marker.startswith(f"{STATUS_OPEN}{{")
    payload = _payload(marker)
    assert payload == {"step": "starting", "mode": "fast", "message": "Scanning `a.py`"}


def test_a_caller_cannot_produce_two_messages():
    """
    `message` is a named parameter, so a **spread that happens to contain one is
    a TypeError rather than a marker with two messages in it. Loud at the call
    site beats a payload whose meaning depends on dict ordering.
    """
    with pytest.raises(TypeError):
        status_event("the real line", step="writing", message="sneaky")
    with pytest.raises(TypeError):
        status_event("the real line", **{"step": "writing", "message": "sneaky"})


def test_unicode_is_not_escaped():
    """`→` costs three bytes escaped; the body is UTF-8 either way."""
    marker = status_event("plan: read → write", step="starting")
    assert "→" in marker
    assert "\\u2192" not in marker


def test_long_fields_are_truncated_and_marked():
    payload = _payload(status_event("m", step="tool", args={"query": "x" * 400}))
    assert payload["args"]["query"].endswith("…")
    assert len(payload["args"]["query"]) == MAX_FIELD_CHARS


def test_the_message_gets_a_bigger_budget_than_a_field():
    """
    A reasoning line is a sentence. Clipping it at the field budget cuts clauses
    out of the one thing the panel shows a user who wants to audit the plan.
    """
    long_line = "why " * 200
    payload = _payload(status_event(long_line, step="reasoning"))
    assert len(payload["message"]) == MAX_MESSAGE_CHARS
    assert MAX_MESSAGE_CHARS > MAX_FIELD_CHARS


def test_lists_are_capped_not_dumped():
    payload = _payload(status_event("m", step="tool_done", preview=[f"f{i}.py" for i in range(50)]))
    assert len(payload["preview"]) == MAX_LIST_ITEMS


def test_surplus_fields_are_reported_as_a_count():
    """Silently dropping fields would make a card lie about what a tool returned."""
    payload = _payload(status_event("m", step="timing", **{f"f{i}": i for i in range(40)}))
    assert payload["fields_dropped"] > 0


def test_a_marker_never_exceeds_the_byte_budget():
    """Long strings and long lists, bounded down to a telemetry line."""
    marker = status_event(
        "q" * 6000, step="tool_done",
        preview=["a" * 300] * 40, nested={"blob": "y" * 5000},
    )

    assert len(marker.encode("utf-8")) <= MAX_STATUS_BYTES, len(marker)
    payload = _payload(marker)
    assert payload["step"] == "tool_done"
    assert len(payload["message"]) == MAX_MESSAGE_CHARS


def test_the_last_shrink_pass_keeps_only_the_identity_fields():
    """
    Too many *fields* is the case lists cannot solve: bounding each value to
    MAX_FIELD_CHARS still leaves more than the budget when a caller spreads a
    whole object into a marker. The final pass keeps identity only, so a misuse
    degrades the display instead of flooding the stream.
    """
    marker = status_event("m", step="tool_done", **{f"key_{i}": "z" * 400 for i in range(40)})
    payload = _payload(marker)

    assert len(marker.encode("utf-8")) <= MAX_STATUS_BYTES, len(marker.encode("utf-8"))
    assert payload["step"] == "tool_done"
    assert set(payload) <= set(ALWAYS_KEEP) | {"message", "fields_dropped"}


def test_encode_event_keeps_list_fields_while_they_fit():
    with_lists = encode_event({"step": "tool_done", "preview": ["a.py"]})
    assert "preview" in with_lists


# ── Errors, sections, filters ─────────────────────────────────────────────────


def test_error_event_stays_plain_text_and_capped():
    marker = error_event("boom " * 100)
    assert marker.startswith(ERROR_OPEN)
    body = marker[len(ERROR_OPEN):marker.index("__ERROR_END__")]
    assert len(body) <= 200


def test_error_event_never_produces_an_empty_marker():
    assert "stream failed" in error_event("")


def test_section_event_carries_the_identity_the_ui_keys_on():
    marker = section_event(id="src/a.py", file_name="a.py")
    assert marker.startswith("__SECTION_START__")
    assert _payload(marker, "__SECTION_START__", "__SECTION_END__") == {
        "id": "src/a.py", "file_name": "a.py",
    }


def test_protocol_tokens_are_recognised_and_prose_is_not():
    assert is_status_marker(status_event("m", step="x"))
    for token in (status_event("m", step="x"), error_event("m"), section_event(id="a")):
        assert is_protocol_token(token)
    assert not is_protocol_token("## 🐛 Bugs & Risks\nNone found.\n")


def test_strip_removes_complete_and_truncated_markers_alike():
    text = (
        "intro "
        + status_event("m", step="tool")
        + "body\n"
        + STATUS_OPEN
        + '{"step": "unterminated'
    )
    assert strip_protocol_markers(text) == "intro body\n"


# ── Decoding ──────────────────────────────────────────────────────────────────


def test_decode_canonical():
    assert decode_status('{"step": "done", "message": "ok"}') == {"step": "done", "message": "ok"}


def test_decode_legacy_text_then_json():
    decoded = decode_status('Scanning `a.py`...{"step": "starting", "mode": "fast"}')
    assert decoded["step"] == "starting"
    assert decoded["message"] == "Scanning `a.py`..."


def test_decode_legacy_with_nested_objects():
    """
    The old clients used /\\{[^}]*\\}$/ to find the payload, which cannot cross a
    nested object — and the review stream emits one (`coverage` carries
    `provider_circuit`). Brute-forcing the trailing object is why this works.
    """
    decoded = decode_status('Coverage: 1/2 {"step": "coverage", "circuit": {"open": true}}')
    assert decoded["step"] == "coverage"
    assert decoded["circuit"] == {"open": True}
    assert decoded["message"] == "Coverage: 1/2"


def test_decode_bare_text_is_a_message_not_a_failure():
    assert decode_status("Generating answer...") == {"message": "Generating answer..."}


def test_decode_garbage_is_an_empty_dict():
    assert decode_status("{not json at all") == {"message": "{not json at all"}
    assert decode_status("") == {}
    assert decode_status("[1, 2]") == {"message": "[1, 2]"}


def test_a_message_inside_the_object_wins_over_a_legacy_prefix():
    """
    If a marker somehow carries both, the object is authoritative: it came from
    the same encoder that produced `step`, and the prefix is what a *previous*
    version of the server put there. Two sources, one winner, stated.
    """
    decoded = decode_status('stale prefix {"step": "x", "message": "authoritative"}')
    assert decoded["message"] == "authoritative"
    assert decode_status('prefix {"step": "x"}')["message"] == "prefix"


def test_encode_decode_round_trip_for_every_scalar_shape():
    fields = {
        "step": "tool_done", "tool": "read_file", "ok": True, "elapsed_ms": 12,
        "count": 3, "ratio": 0.5, "note": None, "preview": ["a.py", "b.py"],
    }
    marker = status_event("done", **fields)
    inner = marker[len(STATUS_OPEN):-len(STATUS_CLOSE) - 1]
    assert decode_status(inner) == {**fields, "message": "done"}


# ── The determinism exemption list ────────────────────────────────────────────


def test_volatile_fields_are_exactly_the_clock_and_the_id():
    """
    A reproducibility check is allowed to ignore these and nothing else, so the
    list must be small and named — every entry weakens the guarantee.
    """
    assert VOLATILE_STATUS_FIELDS == frozenset({"run_id", "elapsed_ms", "t_ms"})


def test_deterministic_view_drops_only_those():
    payload = {"step": "complete", "run_id": "abc", "elapsed_ms": 9, "ok": True, "t_ms": 1}
    assert deterministic_view(payload) == {"step": "complete", "ok": True}


def test_bound_event_is_idempotent():
    once = bound_event({"message": "x" * 500, "preview": ["y" * 500]})
    assert bound_event(once) == once
