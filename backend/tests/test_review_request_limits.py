"""
Tests for the multi-review request cap.

The cap used to be a bare 200 with no test, no configuration, and nothing on
the page that showed it. A 238-file selection was refused by a number that had
no technical basis behind it, while the button read "Review 238 files" — so
this file pins the three things that make a limit honest: it is configurable,
it is discoverable before the request, and when it does fire it says what to do
rather than leaking Pydantic's internal prefix.
"""

from __future__ import annotations

import pydantic
import pytest

from app.api.review import ReviewMultiRequest, _max_files_per_request

SOURCE = "https://github.com/garimauttam/SavFlux::frontend/src/component.tsx"


def _files(n: int) -> list[dict]:
    return [
        {"file_path": f"{SOURCE}?{i}", "file_name": f"f{i}.tsx", "language": "tsx"}
        for i in range(n)
    ]


def test_a_real_repo_is_not_refused_by_an_arbitrary_number():
    """The bug: 238 files, cap 200, nothing said why."""
    assert len(ReviewMultiRequest(files=_files(238)).files) == 238


def test_the_cap_is_configurable_rather_than_hardcoded():
    from app.core.config import get_settings

    # Read at call time, so a deployment (or a test) can change it without the
    # module being reloaded.
    assert _max_files_per_request() == get_settings().review_max_files_per_request
    assert _max_files_per_request() >= 1000, (
        "the default must cover a large monorepo; a real repo was refused at 200"
    )


def test_the_refusal_says_what_to_do_and_how_many():
    cap = _max_files_per_request()
    with pytest.raises(pydantic.ValidationError) as exc:
        ReviewMultiRequest(files=_files(cap + 7))

    msg = exc.value.errors()[0]["msg"]
    assert f"{cap + 7} files selected" in msg
    assert str(cap) in msg
    assert "Deselect 7 file(s)" in msg
    # Names the escape hatch, so the reader is not left thinking the ceiling is
    # a law of nature.
    assert "REVIEW_MAX_FILES_PER_REQUEST" in msg


def test_the_refusal_does_not_leak_pydantics_internal_prefix():
    """A plain ValueError reaches the UI as "Value error: <message>"."""
    cap = _max_files_per_request()
    with pytest.raises(pydantic.ValidationError) as exc:
        ReviewMultiRequest(files=_files(cap + 1))

    assert "Value error" not in exc.value.errors()[0]["msg"]
    assert exc.value.errors()[0]["type"] == "too_many_files"


def test_an_empty_selection_is_still_refused_in_a_readable_way():
    with pytest.raises(pydantic.ValidationError) as exc:
        ReviewMultiRequest(files=[])

    assert exc.value.errors()[0]["type"] == "no_files"
    assert "Value error" not in exc.value.errors()[0]["msg"]


def test_the_page_can_learn_the_cap_before_building_a_request(client):
    """
    Advertising a ceiling the page does not show is the same defect as
    advertising a capability the page lacks — the reader only finds out by
    being refused.
    """
    resp = client.get("/api/v1/review/limits")

    assert resp.status_code == 200
    body = resp.json()
    assert body["max_files_per_request"] == _max_files_per_request()
    # The body limit is a real one and belongs in the same answer, because a
    # large paste can hit it before the file count does.
    assert body["max_body_bytes"] == 10 * 1024 * 1024
