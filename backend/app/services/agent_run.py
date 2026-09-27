"""
agent_run.py — the bookkeeping an agent run needs, so the route can stay a route.

A run of the code agent is not a list of log lines: it is a *plan* with a state,
several tool invocations with arguments and results, and a budget. Before this
module, all of that was implicit in `app/api/agent.py`'s loop — a counter, a
string join, and a UI that reconstructed the pairing by matching on tool name.

That reconstruction is where the agent surface lost its usefulness. `read_file`
runs up to three times per run, so three "read a file ✓" lines in a row say
nothing about which file, what came back, or how long each took. The fix is to
make the pairing server-side and authoritative:

  * every invocation gets a `step_id` (`read_file#2`) and a `plan_id`
    (`plan:read_file`) on both its start and its finish marker, so the client can
    bind args → result → duration without guessing;
  * every finish carries `elapsed_ms`, measured here with one clock;
  * the budget is reported as a number (`steps_used` / `max_steps`) and
    `truncated` when a planned step never got a slot — a run that silently ran
    4 of 7 steps reads as a complete run otherwise;
  * arguments are summarised, never dumped: `summarize_args` has a per-tool
    allowlist, so `read_file` reports the path and not the file.

`plan_reasoning` returns the *why* of a plan as data. It is not chain-of-thought —
this agent is deterministic, so the reasoning is the branch arithmetic itself,
which is exactly what a reviewer wants to check before trusting a run that may
push a patch. Presenting invented "thinking" prose would make the surface look
more agentic and be less trustworthy, so the panel labels it "Plan reasoning".
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app.services.agent_tools import ToolResult

#: How many bytes of a string argument survive into a status marker. Long enough
#: for a path or a PR title, short enough that a prompt cannot ride the wire.
MAX_ARG_CHARS = 160
#: How many names a result preview lists before "+N more".
MAX_PREVIEW_ITEMS = 6

#: Keys never summarised into a marker, whatever the tool. Values under these
#: names are payloads (file text, diffs, PR bodies, whole graphs), which belong in
#: the report. `ToolResult.BULKY_KEYS` guards the result side; this guards the
#: argument side, where a caller passes the payload in directly.
OPAQUE_ARG_KEYS = frozenset(
    {
        "content",
        "diff",
        "patch",
        "pr_body",
        "original",
        "graph",
        "contexts",
        "findings",
        "changes",
        "confirm_digest",
        "body",
    }
)


# ── Step identity ─────────────────────────────────────────────────────────────


@dataclass
class StepHandle:
    """One invocation of one tool, from start to finish."""

    tool: str
    step_id: str
    plan_id: str
    started_at: float
    #: Free-form, so a caller can hang whatever it needs off its own step (the
    #: route keeps the file it is working on so a loop can name it in the message).
    note: str = ""


@dataclass
class PlanItem:
    """A planned tool, with the states its invocations ended in."""

    id: str
    tool: str
    order: int
    started: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def state(self) -> str:
        """
        A single state for the whole plan row, for the rail.

        Order matters: a row that started is never "queued", and a row with a
        failure is reported as failed even when a later attempt succeeded — a
        reviewer looking for "did anything go wrong" must not have to read the
        counts to find out.
        """
        if self.failed:
            return "failed"
        if self.started > self.done + self.failed:
            return "running"
        if self.started:
            return "done" if self.done else "skipped"
        return "queued"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "order": self.order,
            "state": self.state,
            "started": self.started,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
        }


# ── The recorder ──────────────────────────────────────────────────────────────


class RunRecorder:
    """
    Identities, timing and budget for one agent run.

    Deliberately free of any knowledge of the wire format: it returns dicts that
    the caller spreads into `status_event()`, so the protocol stays in one module
    and this one stays testable without a stream.
    """

    def __init__(self, goal: str, plan: list[str], max_steps: int, *, mode: str = "deterministic"):
        self.goal = goal
        self.plan = list(plan)
        self.max_steps = int(max_steps)
        self.mode = mode
        self.run_id = uuid.uuid4().hex[:12]
        self._clock: Callable[[], float] = time.monotonic
        self._t0 = self._clock()
        self.steps_used = 0
        self._counts: dict[str, int] = {}
        self._by_step: dict[str, StepHandle] = {}
        self._items = {
            tool: PlanItem(id=f"plan:{tool}", tool=tool, order=index)
            for index, tool in enumerate(self.plan)
        }

    # ── budget ────────────────────────────────────────────────────────────────

    @property
    def budget_left(self) -> int:
        return max(0, self.max_steps - self.steps_taken)

    @property
    def steps_taken(self) -> int:
        """Slots consumed, including the step currently running."""
        return self.steps_used

    @property
    def truncated(self) -> bool:
        """A planned step never got a slot because the budget ran out."""
        planned = len(self._items)
        finished = sum(
            item.done + item.failed + item.skipped for item in self._items.values()
        )
        return self.steps_taken >= self.max_steps and finished < planned

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._t0) * 1000)

    # ── steps ─────────────────────────────────────────────────────────────────

    def begin(self, tool: str, *, note: str = "") -> StepHandle:
        """Claim a slot and open a step. Raises nothing: a full budget is the
        caller's problem to check with `budget_left` first."""
        self.steps_used += 1
        count = self._counts.get(tool, 0) + 1
        self._counts[tool] = count
        step_id = f"{tool}#{count}"
        item = self._items.setdefault(
            tool,
            PlanItem(id=f"plan:{tool}", tool=tool, order=len(self._items)),
        )
        item.started += 1
        handle = StepHandle(tool=tool, step_id=step_id, plan_id=item.id, started_at=self._clock(), note=note)
        self._by_step[step_id] = handle
        return handle

    def finish(self, handle: StepHandle, ok: bool, *, result: ToolResult | None = None) -> dict:
        """
        Close a step: duration, outcome, and the bounded preview of what came back.

        The returned dict is spread straight into a status marker, which is why it
        carries `step_id`/`plan_id`/`tool` again: a client may render the finish
        line on its own (a reconnect, a replayed log) and must not need the start
        marker to know which step this is.
        """
        item = self._items.get(handle.tool)
        if item is not None:
            if ok:
                item.done += 1
            else:
                item.failed += 1
        self._by_step.pop(handle.step_id, None)
        payload = {
            "step_id": handle.step_id,
            "plan_id": item.id if item else f"plan:{handle.tool}",
            "tool": handle.tool,
            "ok": bool(ok),
            "elapsed_ms": max(0, int((self._clock() - handle.started_at) * 1000)),
        }
        if result is not None:
            payload.update(result_preview(result))
        return payload

    def skip(self, tool: str) -> dict:
        """
        Record a planned step that did not run by choice.

        Distinct from a failure on purpose: "no auto-fixable findings" is a
        clean answer, and a UI that colours it red teaches the reader to ignore
        the difference.
        """
        item = self._items.get(tool)
        if item is not None:
            item.skipped += 1
        else:
            item = self._items.setdefault(tool, PlanItem(id=f"plan:{tool}", tool=tool, order=len(self._items)))
            item.skipped += 1
            item.started = 1  # so the state resolves to "skipped", not "queued"
        return {"plan_id": item.id, "tool": tool}

    def open_ms(self, step_id: str) -> int:
        """How long an unclosed step had been running, for the cancel marker."""
        handle = self._by_step.get(step_id)
        return max(0, int((self._clock() - handle.started_at) * 1000)) if handle else 0

    def open_steps(self) -> list[dict]:
        """Steps begun but never finished — what to mark as interrupted on cancel."""
        return [
            {"step_id": handle.step_id, "plan_id": handle.plan_id, "tool": handle.tool}
            for handle in self._by_step.values()
        ]

    # ── snapshots ─────────────────────────────────────────────────────────────

    def plan_items(self) -> list[dict]:
        return [self._items[tool].to_dict() for tool in self.plan if tool in self._items]

    def snapshot(self) -> dict:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "steps_used": self.steps_taken,
            "max_steps": self.max_steps,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "done": sum(item.done for item in self._items.values()),
            "failed": sum(item.failed for item in self._items.values()),
            "skipped": sum(item.skipped for item in self._items.values()),
        }

    def started_event(self, *, extra: dict | None = None) -> dict:
        """Fields for the run's opening marker."""
        payload = {
            "step": "starting",
            "run_id": self.run_id,
            "mode": self.mode,
            "goal": self.goal,
            "plan": " → ".join(self.plan),
            "plan_steps": [
                {"id": item.id, "tool": item.tool, "order": item.order, "state": "queued"}
                for item in (self._items[tool] for tool in self.plan if tool in self._items)
            ],
            "max_steps": self.max_steps,
        }
        if extra:
            payload.update(extra)
        return payload

    def finish_event(self, ok: bool = True, *, reason: str = "") -> dict:
        """Fields for the run's closing marker, paired with `started_event`."""
        payload = self.snapshot()
        payload["step"] = "complete" if ok else "failed"
        payload["ok"] = bool(ok)
        if reason:
            payload["reason"] = reason
        return payload


# ── Argument summaries ────────────────────────────────────────────────────────

#: Which arguments a card shows, per tool. An allowlist rather than "everything
#: scalar" because the agent's own kwargs include the payload it is about to
#: write — `build_patch` receives whole files in `changes`, and a status marker
#: that carried them would put the diff in the step list as well as the report.
ARG_FIELDS: dict[str, tuple[str, ...]] = {
    "retrieve_context": ("query", "top_k"),
    "read_file": ("source",),
    "dependency_graph": ("repo_url",),
    "blast_radius": ("file",),
    "autofix": ("file",),
    "build_patch": ("title", "summary"),
    "create_pr": ("repo", "head", "base", "title"),
}


def summarize_args(tool: str, kwargs: dict[str, Any], *, extra: dict | None = None) -> dict:
    """
    A card-sized view of the arguments one tool was called with.

    Values are strings so the UI can print them without type-guessing. Long ones
    are truncated with an ellipsis and the original length, because a truncated
    path and a truncated query are different problems to read about.
    """
    summary: dict[str, str] = {}
    # A tool with no allowlist entry still gets a card: fall back to its scalar
    # kwargs, minus anything the payload rule says is a document rather than an
    # argument. Silent-empty is the failure mode that makes a new tool invisible.
    keys = ARG_FIELDS.get(tool) or [key for key in kwargs if key not in OPAQUE_ARG_KEYS]
    for key in keys:
        value = kwargs.get(key)
        if value is None or value == "":
            continue
        summary[key] = _display(key, value)

    if tool == "build_patch":
        changes = kwargs.get("changes") or []
        summary["files"] = str(len(changes))
        summary.pop("changes", None)
    if tool == "create_pr":
        # The digest is the confirmation. A card should say whether it was
        # supplied, never carry it: it is the one value that authorises a push.
        digest = kwargs.get("confirm_digest")
        summary["confirmed"] = "yes" if digest else "no"
    if tool == "blast_radius" and kwargs.get("graph") is not None:
        summary["graph"] = "reused from this run"

    if extra:
        summary.update({key: str(value) for key, value in extra.items()})
    return summary


def _display(key: str, value: Any) -> str:
    """An index id is `repo::path`; a card only needs the path."""
    if isinstance(value, str) and "::" in value and key in ("source", "file", "path"):
        value = value.split("::", 1)[1]
    return _stringify(key, value)


def _stringify(key: str, value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return f"{len(value)} item(s)"
    text = value if isinstance(value, str) else str(value)
    if len(text) > MAX_ARG_CHARS:
        return f"{text[:MAX_ARG_CHARS]}… ({len(text)} chars)"
    return text


# ── Result previews ───────────────────────────────────────────────────────────


def result_preview(result: ToolResult) -> dict:
    """
    What a tool found, in the handful of lines a reader scans.

    `status_data()` already carries the scalars (counts, digests, urls). This
    adds the *names*, which are the difference between "3 chunks" and "it looked
    at net.py, ssl.py and client.py" — without it, a card cannot be checked
    against the question it was supposed to answer.
    """
    data = result.data or {}
    names: list[str] = []

    if result.tool == "retrieve_context":
        names = [
            _label(context.get("file_name") or context.get("source"), context.get("language"))
            for context in (data.get("contexts") or [])
        ]
    elif result.tool == "blast_radius":
        names = list(data.get("impacted_files") or [])
    elif result.tool == "read_file":
        names = [_label(data.get("file"), f"{data.get('chars', 0)} chars")]
    elif result.tool == "autofix":
        names = [_label(data.get("file"), f"score {data.get('score_before')} → {data.get('score_after')}")]
    elif result.tool == "build_patch":
        # `files` is the per-file summary the patch service builds for exactly
        # this purpose, so the card and the diff can never disagree about which
        # files a patch touched.
        names = [
            _label(
                (entry or {}).get("path"),
                " ".join(
                    part
                    for part in (
                        (entry or {}).get("status"),
                        f"+{(entry or {}).get('additions', 0)}/-{(entry or {}).get('deletions', 0)}",
                    )
                    if part
                ),
            )
            for entry in (data.get("files") or [])
        ]
    elif result.tool == "create_pr":
        # The gh command and the PR body go in the report, where they are useful.
        # On the card the only two things worth reading are whether it opened and
        # where to look.
        status = data.get("status")
        if status == "created":
            names = [_label(data.get("url"), f"#{data.get('number')}" if data.get("number") else "")]
        else:
            names = [_label(data.get("reason") or "awaiting confirmation", status)]

    names = [name for name in names if name]
    preview = {
        "preview": names[:MAX_PREVIEW_ITEMS],
        "preview_more": max(0, len(names) - MAX_PREVIEW_ITEMS),
    }
    return preview


def _label(primary: Any, secondary: Any = None) -> str:
    left = str(primary).strip() if primary not in (None, "") else ""
    right = str(secondary).strip() if secondary not in (None, "") else ""
    if left and right:
        return f"{left} · {right}"
    return left or right


def plan_reasoning(
    *,
    plan: list[str],
    fix_hit: str = "",
    pr_hit: str = "",
    explicit_tools: bool = False,
    write_path: bool = False,
    read_limit: int = 0,
    repo_known: bool = True,
    confirmed: bool = False,
) -> list[str]:
    """
    Explain the plan in the run's own terms. One line per decision actually taken.

    Every sentence here is a fact about the branch the code just went through, so
    the panel can print it verbatim and a test can assert on it. Nothing is
    inferred about intent beyond the two regexes the planner itself uses.
    """
    lines: list[str] = []
    if explicit_tools:
        lines.append(
            "Plan was given explicitly, so the goal text did not choose the tools. "
            "Canonical order is still applied: retrieval before anything that reads or writes."
        )
    elif fix_hit or pr_hit:
        hits = ", ".join(f"`{hit}`" for hit in (fix_hit, pr_hit) if hit)
        lines.append(
            f"Goal contains {hits}, so this run may change files: `autofix` repairs, "
            "`build_patch` renders one reviewable diff."
        )
    else:
        lines.append(
            "No change was asked for, so the plan stays read-only: retrieval, file reads, "
            "dependency context, blast radius. Nothing here can touch a working tree."
        )
    if pr_hit:
        lines.append(
            "A pull request was asked for, so `create_pr` is last in the plan. Nothing will be "
            "pushed without your confirmation: the first run only returns the branch, the body and "
            "a digest of the diff, and re-running with that digest is the confirmation."
            if not confirmed
            else "A confirmed diff digest came with this request, so `create_pr` may push. "
                 "The digest is compared with the patch built in *this* run — a changed diff "
                 "fails the confirmation check instead of being pushed."
        )
    lines.append(
        f"{'Write path' if write_path else 'Read-only run'}: the top {read_limit} "
        f"retrieved file(s) are read in full"
        + (
            " — every read file is a repair candidate, so the loop stops early rather "
            "than pulling sources that would only widen the patch."
            if write_path
            else " so the report can quote real lines instead of chunk fragments."
        )
    )
    if not repo_known:
        lines.append(
            "No repo_url, so the dependency graph and blast radius are scoped to the whole "
            "index rather than one repository, and a pull request cannot be opened."
        )
    if not plan:
        lines.append("Empty plan — nothing to run.")
    return lines


def outcome_line(payload: dict) -> str:
    """
    One sentence summarising a finished step, built from what the recorder knows.

    Lives on the backend because the same sentence is the `message` a client shows
    in a collapsed row and the text a log line needs; formatting it twice, in two
    languages, is how the two drift.
    """
    tool = payload.get("tool", "step")
    elapsed = payload.get("elapsed_ms")
    if not payload.get("ok", True):
        return f"{tool} failed"
    bits = [f"{tool} ok"]
    if payload.get("preview_more"):
        bits.append(f"{payload['preview_more']} more")
    if elapsed is not None:
        bits.append(f"{elapsed} ms")
    return " · ".join(bits)


__all__ = [
    "RunRecorder",
    "StepHandle",
    "outcome_line",
    "plan_reasoning",
    "result_preview",
    "summarize_args",
]
