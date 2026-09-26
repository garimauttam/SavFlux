"""
Tests for the code agent's event stream — the contract the UI is built on.

The agent surface used to be a flat bullet list of status lines, and it was a flat
bullet list because that is all the stream contained: "read_file: net.py" with no
arguments, no result and no duration, three times in a row for a run that read
three files. These tests pin what replaced it.

What each one is really guarding:

  * **pairing** — a start marker without a matching finish (or vice versa) makes a
    step spin forever in a UI that renders state from the pair. Tool name alone
    cannot pair them, because `read_file` runs several times per run; the ids can.
  * **redaction** — the arguments a card shows must not include the file it read or
    the diff it built. Those are payloads; the report carries them.
  * **budget** — a run that could not finish must say so. A silent stop at 4 of 7
    steps looks exactly like a 4-step plan.
  * **cancellation** — Stop has to end the run *and* say where it stopped.
  * **the byte budget** — a marker is re-parsed per streamed chunk, so it is a
    display surface, not a transport.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.api.agent import MAX_READS_INVESTIGATE, MAX_READS_WRITE, _run_agent, build_plan
from app.services.agent_run import RunRecorder, plan_reasoning, result_preview, summarize_args
from app.services.agent_tools import ToolResult
from app.services.stream_protocol import MAX_STATUS_BYTES, STATUS_CLOSE, STATUS_OPEN, decode_status

NET_PY = (
    "import hashlib\n"
    "import requests\n"
    "\n"
    "def fetch(url):\n"
    "    return requests.get(url, verify=False)\n"
    "\n"
    "def cache_key(data):\n"
    "    return hashlib.md5(data).hexdigest()\n"
)
OTHER_PY = "import ssl\n\ndef ctx():\n    return ssl._create_unverified_context()\n"
REPO = "https://github.com/octo/demo"


def _doc(content: str, source: str, file_name: str):
    from langchain_core.documents import Document

    return Document(
        page_content=content,
        metadata={"source": f"{REPO}::{source}", "file_name": file_name, "language": "py"},
    )


def _reader(by_source: dict):
    """
    Stand-in for `read_indexed_file` with the same signature.

    The real one takes `(path, *, source=None, repo_url=None)` and callers use
    both forms — `agent_tools.read_file` passes the index id positionally,
    `fix_service` passes a repo-relative path plus a `source=` keyword. A fake
    narrower than the function it replaces fails in ways the product does not.
    """
    def read(path, *, source=None, repo_url=None):
        for key in (source, path, f"{REPO}::{path}"):
            if key and key in by_source:
                return by_source[key]
        return ""
    return read


def _indexed(*pairs: tuple[str, str, str]):
    """Patch retrieval + index reads so a run is hermetic (no network, no disk)."""
    docs = [_doc(content, path, name) for content, path, name in pairs]
    by_source = {f"{REPO}::{path}": content for content, path, _ in pairs}

    return (
        patch("app.services.retrieval_service.retrieve_chunks", new=AsyncMock(return_value=docs)),
        patch("app.services.indexed_content.read_indexed_file",
              new=_reader(by_source)),
    )


def _collect(goal: str, *, repo_url: str | None = REPO, max_steps: int = 8,
             tools: list[str] | None = None, confirm_digest: str | None = None,
             should_stop=None, pairs=((NET_PY, "pkg/net.py", "net.py"),)) -> tuple[list[dict], str]:
    """Run the agent to completion and return (markers, report text)."""
    retrievals, reader = _indexed(*pairs)

    async def drive() -> str:
        text = ""
        with retrievals, reader:
            async for chunk in _run_agent(goal, repo_url, max_steps, tools, confirm_digest,
                                          should_stop=should_stop):
                text += chunk
        return text

    raw = asyncio.run(drive())
    markers, report = [], raw
    cursor = 0
    while True:
        start = report.find(STATUS_OPEN, cursor)
        if start == -1:
            break
        end = report.find(STATUS_CLOSE, start)
        if end == -1:
            pytest.fail(f"unterminated status marker at {start}: {raw[start:start + 200]!r}")
        markers.append(decode_status(report[start + len(STATUS_OPEN):end]))
        report = report[:start] + report[end + len(STATUS_CLOSE):]
        cursor = start
    return markers, report.strip()


# ── Pairing ───────────────────────────────────────────────────────────────────


def test_every_started_step_finishes_with_the_same_id():
    markers, _ = _collect("map the retry logic in net.py")
    started = [m for m in markers if m["step"] == "tool"]
    finished = [m for m in markers if m["step"] in ("tool_done", "tool_error")]

    assert started and finished
    assert {m["step_id"] for m in started} == {m["step_id"] for m in finished}
    for marker in started + finished:
        assert marker["plan_id"] == f"plan:{marker['tool']}"


def test_repeated_invocations_of_one_tool_get_distinct_ids():
    """
    `read_file` runs once per retrieved file. With tool names as the only key, a
    UI cannot tell which result belongs to which read — the defect this exists for.
    """
    markers, _ = _collect(
        "look at the tls handling across the network layer",
        pairs=((NET_PY, "pkg/net.py", "net.py"), (OTHER_PY, "pkg/ssl_ctx.py", "ssl_ctx.py")),
    )
    reads = [m for m in markers if m["step"] == "tool" and m["tool"] == "read_file"]

    assert len(reads) == 2, [m["step_id"] for m in reads]
    assert [m["step_id"] for m in reads] == ["read_file#1", "read_file#2"]
    assert {m["args"]["source"] for m in reads} == {"pkg/net.py", "pkg/ssl_ctx.py"}


def test_a_finished_step_carries_duration_outcome_and_preview():
    markers, _ = _collect("fix the unsafe TLS call in net.py")
    done = next(m for m in markers if m["step"] == "tool_done" and m["tool"] == "retrieve_context")

    assert isinstance(done["elapsed_ms"], int) and done["elapsed_ms"] >= 0
    assert done["ok"] is True
    assert done["preview"] == ["net.py · py"]
    assert done["count"] == 1


def test_a_step_that_failed_is_reported_as_finished_not_missing():
    """A raising tool becomes a red card, not a spinner that never resolves.

    Patched through `DISPATCH` rather than the module attribute: the dispatch
    table captured the function objects at import, so an attribute patch would
    silently test the real tool instead of the failing one.
    """
    boom = AsyncMock(side_effect=RuntimeError("chroma down"))
    with patch.dict("app.api.agent.DISPATCH", {"retrieve_context": boom}):
        markers, report = _collect("map the retry logic", tools=["retrieve_context"])

    failure = next(m for m in markers if m["step"] == "tool_error")
    assert failure["ok"] is False
    assert "chroma down" in failure["message"]
    assert failure["elapsed_ms"] >= 0
    assert report  # the run still produced a report


# ── Arguments: what a card may show ───────────────────────────────────────────


def test_read_file_names_the_file_and_not_its_contents():
    markers, _ = _collect("map the retry logic in net.py")
    start = next(m for m in markers if m["step"] == "tool" and m["tool"] == "read_file")

    assert start["args"] == {"source": "pkg/net.py"}     # repo-relative, not the index id
    assert "requests.get" not in json.dumps(start)


def test_the_patch_card_never_carries_the_patch():
    """
    The diff is the report's job. A marker that carried it would be serialised and
    re-parsed on every streamed chunk to display text the reader already has.
    """
    markers, report = _collect("fix the unsafe TLS call in net.py and open a PR")
    patch_start = next(m for m in markers if m["step"] == "tool" and m["tool"] == "build_patch")
    patch_done = next(m for m in markers if m["step"] == "tool_done" and m["tool"] == "build_patch")

    assert "diff --git" in report                        # it is in the report…
    assert "diff --git" not in json.dumps(patch_start)   # …and nowhere near a marker
    assert patch_start["args"]["files"] == "1"
    assert patch_done["preview"] == ["pkg/net.py · modified +2/-2"]
    assert patch_done["additions"] == 2 and patch_done["deletions"] == 2


def test_the_confirmation_digest_needs_no_args_to_be_safe():
    """
    `create_pr` is the only step that can touch a real repository. Its card says
    whether a confirmation arrived without echoing the token that *is* one.
    """
    markers, _ = _collect("fix the TLS call and open a PR")
    pr_start = next(m for m in markers if m["step"] == "tool" and m["tool"] == "create_pr")

    assert pr_start["args"]["confirmed"] == "no"
    assert pr_start["args"]["repo"] == "octo/demo"
    assert pr_start["args"]["base"] == "main"
    assert "confirm_digest" not in json.dumps(pr_start)
    assert "digest" not in json.dumps(pr_start["args"])


def test_an_unknown_tool_still_gets_a_card():
    """An allowlist with no entry must degrade to a summary, not to an empty card."""
    summary = summarize_args("brand_new_tool", {"path": "a.py", "deep": {"x": 1}, "content": "z" * 4000})
    assert summary == {"path": "a.py", "deep": "{'x': 1}"}


def test_long_arguments_are_truncated_with_their_real_length():
    value = summarize_args("retrieve_context", {"query": "q" * 500})
    assert value["query"].endswith("(500 chars)")


# ── Plan, reasoning, budget ───────────────────────────────────────────────────


def test_the_opening_marker_holds_the_whole_plan():
    markers, _ = _collect("fix the TLS call and open a PR")
    opening = markers[0]

    assert opening["step"] == "starting" and opening["mode"] == "deterministic"
    assert len(opening["run_id"]) == 12
    assert [item["tool"] for item in opening["plan_steps"]] == build_plan("fix the TLS call and open a PR")
    assert {item["state"] for item in opening["plan_steps"]} == {"queued"}
    assert [item["order"] for item in opening["plan_steps"]] == list(range(len(opening["plan_steps"])))


def test_reasoning_lines_name_the_trigger_they_saw():
    """
    The deterministic agent has no chain of thought, and inventing one would be
    theatre. What it has is plan arithmetic, and these lines are that arithmetic —
    so a reader can check *why* a run that can edit files was allowed to.
    """
    markers_read, read_only = _collect("map the auth flow")
    reasons = [m["message"] for m in markers_read if m["step"] == "reasoning"]

    assert reasons and all(m["kind"] == "plan" for m in markers_read if m["step"] == "reasoning")
    assert any("read-only" in line for line in reasons)

    markers_fix, _ = _collect("harden the TLS call and open a PR")
    fix_reasons = [m["message"] for m in markers_fix if m["step"] == "reasoning"]
    assert any("`harden`" in line for line in fix_reasons)
    assert any("confirm" in line.lower() for line in fix_reasons)
    assert read_only  # the plan lines do not replace the report


def test_explicit_tools_are_reported_as_an_override():
    markers, _ = _collect("anything", tools=["retrieve_context"])
    reasons = [m["message"] for m in markers if m["step"] == "reasoning"]

    assert any("given explicitly" in line for line in reasons)


def test_running_out_of_budget_is_said_twice():
    """In the telemetry, and in the report the user might read alone."""
    markers, report = _collect("fix the TLS call and open a PR", max_steps=1)
    closing = next(m for m in markers if m["step"] == "complete")

    assert closing["steps_used"] == 1 and closing["max_steps"] == 1
    assert closing["truncated"] is True
    assert "step budget" in report


def test_a_planned_step_that_was_declined_reports_as_skipped():
    """
    "No auto-fixable findings" is a clean answer. Counting it as a failure would
    train the reader to ignore the red rows, which are the ones that matter.
    """
    clean = "def ok(x):\n    return x + 1\n"
    markers, _ = _collect("fix whatever is wrong here", pairs=((clean, "pkg/fine.py", "fine.py"),))
    skipped = [m for m in markers if m["step"] == "tool_skipped"]
    closing = next(m for m in markers if m["step"] == "complete")

    assert any(m["tool"] == "autofix" for m in skipped)
    assert all(m["plan_id"] == f"plan:{m['tool']}" for m in skipped)
    assert closing["skipped"] >= 1 and closing["failed"] == 0


# ── Cancellation ──────────────────────────────────────────────────────────────


def test_a_stop_between_steps_ends_the_run_and_names_the_open_step():
    """
    The Stop button aborts the fetch, which closes the response body; the backend
    learns via `Request.is_disconnected`. What is tested here is the contract that
    makes that honest: the run stops *working*, and says where it stopped.
    """
    calls = {"n": 0}

    async def stop_after_first() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    markers, report = _collect("map the retry logic in net.py", should_stop=stop_after_first)
    kinds = [m["step"] for m in markers]

    assert "cancelled" in kinds
    assert "complete" not in kinds
    assert not report.strip()
    cancelled = next(m for m in markers if m["step"] == "cancelled")
    assert cancelled["interrupted"] == []
    assert cancelled["steps_used"] >= 1


def test_an_interrupted_step_is_listed_as_interrupted():
    """
    If a step started and never finished, the only honest report is that it was
    cut short — otherwise the UI keeps a running card on screen forever.
    """
    recorder = RunRecorder("goal", ["read_file"], 4)
    recorder.begin("read_file")

    assert recorder.open_steps() == [{"step_id": "read_file#1", "plan_id": "plan:read_file",
                                      "tool": "read_file"}]
    assert recorder.open_ms("read_file#1") >= 0
    assert recorder.open_ms("nope") == 0


def test_later_tools_are_never_reached_once_the_reader_is_gone():
    seen: list[str] = []

    def record(name: str):
        async def _run(**_kwargs):
            seen.append(name)
            return ToolResult(name, True, "ran", data={})
        return _run

    stop = {"asked": 0}

    async def stopping() -> bool:
        # The runner checks between steps; the first check happens before the
        # first tool, so answering "yes" on the *second* check lets exactly one
        # step run and then stops — which is the behaviour under test.
        stop["asked"] += 1
        return stop["asked"] > 1

    tools = {name: record(name) for name in
             ("retrieve_context", "read_file", "dependency_graph", "blast_radius")}

    async def drive():
        with patch.dict("app.api.agent.DISPATCH", tools, clear=False), \
             patch("app.services.retrieval_service.retrieve_chunks", new=AsyncMock(return_value=[])):
            return [chunk async for chunk in _run_agent("map the flow", REPO, 8, should_stop=stopping)]

    text = "".join(asyncio.run(drive()))

    assert seen == ["retrieve_context"], seen
    assert '"step": "cancelled"' in text, text
    assert '"step": "complete"' not in text


# ── Size and failure surfaces ─────────────────────────────────────────────────


def test_no_marker_in_a_real_run_exceeds_the_byte_budget():
    """Even when a tool returns a 400 KB file: the bound is applied at emit time."""
    huge = "x = 'a' * 400000\n" + NET_PY
    markers, _ = _collect("map the retry logic in net.py", pairs=((huge, "pkg/big.py", "big.py"),))

    for marker in markers:
        assert len(json.dumps(marker).encode("utf-8")) <= MAX_STATUS_BYTES


def test_a_crash_inside_the_report_surfaces_as_an_error_marker():
    """
    An exception after the last step used to end the stream with status 200 and a
    half-rendered report. The route wraps the generator so the failure is visible.
    """
    from app.api import agent as agent_api

    with patch("app.api.agent._build_report", side_effect=RuntimeError("template exploded")), \
         patch("app.services.retrieval_service.retrieve_chunks", new=AsyncMock(return_value=[])), \
         patch("app.services.indexed_content.read_indexed_file", return_value=""):
        text = ""

        async def drive():
            nonlocal text
            async for chunk in agent_api._logged(
                agent_api._run_agent("map the flow", REPO, 8), goal="map the flow"
            ):
                text += chunk

        with pytest.raises(RuntimeError, match="template exploded"):
            asyncio.run(drive())

    assert "__ERROR__" in text and "template exploded" in text


def test_result_preview_drops_the_bulky_keys():
    """`status_data()` and `result_preview` share one rule: names yes, payloads no."""
    result = ToolResult("read_file", True, "read", data={"file": "a.py", "chars": 9, "content": "z" * 9000})
    preview = result_preview(result)

    assert preview["preview"] == ["a.py · 9 chars"]
    assert "z" * 100 not in json.dumps(preview)


def test_preview_overflow_is_counted():
    contexts = [{"file_name": f"f{i}.py", "source": f"src/f{i}.py", "language": "py"} for i in range(20)]
    preview = result_preview(ToolResult("retrieve_context", True, "found", data={"contexts": contexts}))

    assert len(preview["preview"]) == 6
    assert preview["preview_more"] == 14


def test_plan_reasoning_says_nothing_it_did_not_do():
    """
    One line per decision, and only for decisions taken. A run that was not asked
    for a PR must not print a sentence about confirmations — that is how a
    "reasoning" panel becomes decoration.
    """
    lines = plan_reasoning(plan=["retrieve_context"], read_limit=3)
    assert len(lines) == 2
    assert not any("pull request" in line.lower() for line in lines)

    with_pr = plan_reasoning(plan=["retrieve_context", "create_pr"], pr_hit="pr", write_path=False,
                             read_limit=3, repo_known=False)
    assert any("pull request" in line.lower() for line in with_pr)
    assert any("No repo_url" in line for line in with_pr)


def test_read_budget_caps_a_run_that_found_too_much():
    """
    Five retrieved files, three reads — and the run says so in the plan.

    Retrieval decides how many candidates a run sees, and it does not consult the
    agent's budget. Without the cap a 40-chunk search turns into 40 file reads on
    every request, which is the difference between a 200 ms run and a 4 s one on a
    laptop; the cap is also the only reason "how long did the agent take" has a
    stable answer for the same goal.
    """
    pairs = tuple((f"def f{i}():\n    return {i}\n", f"pkg/mod{i}.py", f"mod{i}.py") for i in range(5))
    markers, report = _collect("investigate how the review budget picks files", pairs=pairs)

    starts = [m for m in markers if m["step"] == "tool" and m["tool"] == "read_file"]
    finishes = [m for m in markers if m["step"] == "tool_done" and m["tool"] == "read_file"]
    assert len(starts) == MAX_READS_INVESTIGATE
    assert len(finishes) == MAX_READS_INVESTIGATE
    assert len({m["step_id"] for m in starts}) == MAX_READS_INVESTIGATE

    planned = next(m for m in markers if m["step"] == "starting")
    row = next(r for r in planned["plan_steps"] if r["tool"] == "read_file")
    # One plan row for a tool that ran three times: the repetition belongs to the
    # transcript, not to the plan. (`cards`/`runs` are counted client-side.)
    assert row["state"] == "queued" and [r["tool"] for r in planned["plan_steps"]].count("read_file") == 1
    # The report carries the number a reader cannot otherwise see: the run spent
    # three reads of its eight-step budget, and never hit the step ceiling.
    assert f"**Steps used:** {MAX_READS_INVESTIGATE + 3}/8" in report
    assert "Step budget" not in report


def test_write_path_reads_fewer_files_than_an_investigate_run():
    """
    The write path trades reach for certainty: it reads only what it is about to
    change, so the patch stays small enough to review. That is only true if the
    two limits really differ, which is what this pins.
    """
    pairs = tuple((f"def f{i}():\n    return {i}\n", f"pkg/mod{i}.py", f"mod{i}.py") for i in range(5))
    markers, _ = _collect("fix the unsafe verification in the network layer", pairs=pairs)

    starts = [m for m in markers if m["step"] == "tool" and m["tool"] == "read_file"]
    assert len(starts) == MAX_READS_WRITE < MAX_READS_INVESTIGATE


def test_every_planned_step_is_accounted_for_even_with_an_empty_index():
    """
    Nothing indexed is the common first run, and it is where a plan goes to lie: the
    agent planned four steps, two of them had no data to work on, and the transcript
    would show `read_file` and `blast_radius` as queued forever.

    A queued-forever row is worse than a red one, because a reader waits. So every
    planned step must end the run in one of three states — it ran, it failed, or it
    said why it did not run. This asserts the property over the whole stream rather
    than over one tool, so the next step anyone adds inherits the rule.
    """
    markers, report = _collect("map the review budget", pairs=())

    planned = next(m for m in markers if m["step"] == "starting")
    by_plan: dict[str, list[str]] = {}
    for marker in markers:
        if marker["step"] in ("tool_done", "tool_error", "tool_skipped") and marker.get("plan_id"):
            by_plan.setdefault(marker["plan_id"], []).append(marker["step"])

    assert planned["plan_steps"], "the run must announce its plan to check it against"
    for row in planned["plan_steps"]:
        assert by_plan.get(row["id"]), f"plan row {row['id']} never resolved: {by_plan}"

    skipped = [m for m in markers if m["step"] == "tool_skipped"]
    assert {m["tool"] for m in skipped} >= {"read_file", "blast_radius"}
    # A skip always carries the reason, and the reason is checked against the state
    # that produced it above (empty retrieval here, not a crash).
    assert all("skipped —" in m["message"] for m in skipped)
    assert "No indexed chunks matched" in report
