"""
agent.py — Deterministic code-task agent API ($0, no LLM required).

The agent decomposes a goal into inspection steps and executes them with
local tools only: hybrid retrieval, file reads, dependency graph, impact
analysis, verified autofix, patch construction, and — only with an explicit
confirmation — pull-request creation. No model call is made, so runs are free,
fast, and fully deterministic: the same goal against the same index always
produces the same report.

The tools themselves live in `app.services.agent_tools`; this module is the
HTTP surface and the plan that stitches them together.

Endpoints:
  GET  /agent/tools  — tool catalogue the agent can use
  POST /agent/run    — stream a plan + findings report (SSE-style text)

Stream protocol (same markers as the review stream):
  __STATUS__{...}__STATUS_END__  → step telemetry for the Agent tab
  everything else                → markdown report text

PLANNING
--------
The goal decides how far the run may go:

  * always            — retrieve_context, read_file, dependency_graph, blast_radius
  * "fix / repair /   — adds autofix (verified repairs) and build_patch
    patch / vuln …"
  * "open a PR …"     — adds create_pr, which returns a reviewable plan and
                        pushes only if the caller confirmed the exact diff

A caller can override the inference with an explicit `tools` list. Editing files
because someone said "look at the auth flow" is the behaviour that makes agents
untrustworthy, so the write path is opt-in in both directions.
"""

import json
import re
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter
from app.services.agent_tools import (
    DISPATCH,
    TOOL_CATALOGUE,
    TOOL_NAMES,
    ToolResult,
)

# Re-exported: `TOOL_CATALOGUE` has lived here since the first version, and the
# UI reads it from `GET /agent/tools` rather than from Python.
__all__ = ["TOOL_CATALOGUE", "AgentRunRequest", "router"]

router = APIRouter(prefix="/agent", tags=["agent"])

# ── Plan inference ────────────────────────────────────────────────────────────
#
# Word-boundary matching, not substring: `"pr" in goal` fires on "improve" and
# "sprint", which would silently hand a read-only question the write path.

_FIX_INTENT = re.compile(
    r"\b(fix|fixes|repair|autofix|auto-fix|patch|remediate|remediation|"
    r"vulnerab\w*|hardening|harden|resolve|safer|secure)\b",
    re.IGNORECASE,
)
_PR_INTENT = re.compile(r"\b(pr|prs|pull request|pull-request|merge request|mr)\b", re.IGNORECASE)

#: Inspection steps every plan starts with, in execution order.
_BASE_PLAN = ["retrieve_context", "read_file", "dependency_graph", "blast_radius"]

#: A goal that only wants to understand the code reads fewer files than one that
#: is about to rewrite them.
MAX_READS_INVESTIGATE = 3
MAX_READS_WRITE = 2


def wants_fix(goal: str) -> bool:
    return bool(_FIX_INTENT.search(goal or ""))


def wants_pr(goal: str) -> bool:
    return bool(_PR_INTENT.search(goal or ""))


def build_plan(goal: str, tools: list[str] | None = None) -> list[str]:
    """
    Tool names to run, in canonical order.

    Canonical order (the order in `TOOL_NAMES`) rather than the caller's, because
    `autofix` is meaningless before `retrieve_context` has found something to fix.
    """
    if tools:
        requested = set(tools)
        return [name for name in TOOL_NAMES if name in requested]

    plan = list(_BASE_PLAN)
    if wants_fix(goal) or wants_pr(goal):
        plan += ["autofix", "build_patch"]
    if wants_pr(goal):
        plan.append("create_pr")
    return plan


def _github_ref(repo_url: str | None) -> str:
    """The `owner/name` a PR could target, or "" when the repo is not on GitHub."""
    from app.services.pr_service import parse_repo_ref

    try:
        return parse_repo_ref(repo_url or "")
    except ValueError:
        return ""


# ── Request / response models ─────────────────────────────────────────────────


class AgentRunRequest(BaseModel):
    goal: str
    repo_url: str | None = None
    max_steps: int = 8
    #: Explicit tool allowlist. None lets the goal decide.
    tools: list[str] | None = None
    #: Digest of the exact diff the user has already seen. `create_pr` will not
    #: push without it — this is the confirmation, and it is bound to the diff.
    confirm_digest: str | None = None

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("goal cannot be empty")
        if len(v) > 1000:
            raise ValueError("goal too long (max 1000 chars)")
        return v

    @field_validator("max_steps")
    @classmethod
    def validate_steps(cls, v: int) -> int:
        return max(1, min(v, 12))

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("tools cannot be empty — omit it to let the goal decide")
        unknown = sorted(set(v) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"unknown tool(s): {', '.join(unknown)}")
        return sorted(set(v))

    @field_validator("confirm_digest")
    @classmethod
    def validate_digest(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not re.fullmatch(r"[0-9a-f]{8,64}", v):
            raise ValueError("confirm_digest must be a hex digest from build_patch")
        return v


@router.get("/tools")
async def list_tools(_: None = Depends(require_api_key)):
    return {"tools": TOOL_CATALOGUE, "total": len(TOOL_CATALOGUE)}


def _status(step: str, message: str, **extra: object) -> str:
    payload = {"step": step, "message": message, **extra}
    return f"__STATUS__{json.dumps(payload)}__STATUS_END__\n"


# ── The run ───────────────────────────────────────────────────────────────────


async def _run_agent(
    goal: str,
    repo_url: str | None,
    max_steps: int,
    tools: list[str] | None = None,
    confirm_digest: str | None = None,
) -> AsyncGenerator[str, None]:
    """Plan → inspect → (fix → patch → PR) → report. Yields status markers + markdown."""
    plan = build_plan(goal, tools)
    steps_taken = 0
    reports: list[str] = []

    #: Files this run repaired, in the shape `build_patch` consumes.
    fixed: list[dict] = []

    def budget() -> bool:
        return steps_taken < max_steps

    async def run_tool(name: str, message: str, **kwargs) -> tuple[list[str], ToolResult | None]:
        """
        Execute one step of the plan inside the budget.

        Returns the status markers to emit and the result. Markers are returned
        rather than yielded so this can stay an ordinary coroutine — an async
        generator cannot be delegated to with `yield from`.
        """
        nonlocal steps_taken
        if name not in plan or not budget():
            return [], None
        steps_taken += 1
        markers = [_status("tool", message, tool=name)]
        try:
            result = await DISPATCH[name](**kwargs)
        except Exception as exc:  # noqa: BLE001 — one bad tool must not kill the run
            result = ToolResult(name, False, f"{name} failed: {str(exc)[:120]}")
        markers.append(_status(
            "tool_done" if result.ok else "tool_error",
            result.message,
            tool=name,
            **result.status_data(),
        ))
        return markers, result

    yield _status("starting", f"Goal: {goal[:100]}", mode="deterministic",
                  plan=" → ".join(plan))

    # ── Step 1: retrieve relevant context ─────────────────────────────────────
    markers, retrieval = await run_tool(
        "retrieve_context", "retrieve_context: searching indexed code…", query=goal, repo_url=repo_url
    )
    for marker in markers:
        yield marker
    contexts: list[dict] = (retrieval.data.get("contexts") if retrieval else None) or []

    # ── Step 2: read the top files in full ────────────────────────────────────
    write_path = "autofix" in plan or "build_patch" in plan
    read_limit = MAX_READS_WRITE if write_path else MAX_READS_INVESTIGATE
    file_contents: dict[str, str] = {}

    seen: set[str] = set()
    for context in contexts:
        source = context.get("source", "")
        if not source or source in seen or len(file_contents) >= read_limit:
            continue
        seen.add(source)
        markers, result = await run_tool(
            "read_file", f"read_file: {context.get('file_name') or source}",
            source=source, repo_url=repo_url,
        )
        for marker in markers:
            yield marker
        if result and result.ok:
            file_contents[source] = result.data.get("content", "")

    # ── Step 3: dependency context ────────────────────────────────────────────
    top_source = contexts[0].get("source") if contexts else None
    graph: dict | None = None

    markers, graph_result = await run_tool(
        "dependency_graph", "dependency_graph: mapping imports…", repo_url=repo_url
    )
    for marker in markers:
        yield marker
    if graph_result and graph_result.ok:
        graph = graph_result.data.get("graph")

    if top_source:
        markers, blast = await run_tool(
            "blast_radius", "blast_radius: mapping dependents…",
            file=top_source, repo_url=repo_url, graph=graph,
        )
        for marker in markers:
            yield marker
        if blast and blast.report:
            reports.append(blast.report)

    # ── Step 4: verified repairs, worst file first ────────────────────────────
    # Only files the retrieval step actually surfaced are eligible. Rewriting
    # something the user did not ask about is not autonomy, it is a surprise.
    if "autofix" in plan:
        for context in contexts:
            if not budget():
                break
            path = _relative_path(context.get("source", ""), repo_url)
            if not path.lower().endswith((".py", ".pyi", ".pyw")):
                continue
            if any(change["path"] == path for change in fixed):
                continue
            markers, fixed_result = await run_tool(
                "autofix", f"autofix: repairing {context.get('file_name') or path}",
                file=path, source=context.get("source"), repo_url=repo_url,
            )
            for marker in markers:
                yield marker
            if fixed_result and fixed_result.ok and fixed_result.data.get("fixed"):
                fixed.append({
                    "path": path,
                    "content": fixed_result.data.get("content", ""),
                    "original": file_contents.get(context.get("source", ""), ""),
                })
                if fixed_result.report:
                    reports.append(fixed_result.report)

        if not fixed:
            yield _status("tool_skipped",
                          "autofix: no auto-fixable Python findings in the retrieved files",
                          tool="autofix")

    # ── Step 5: one reviewable patch for everything that was fixed ────────────
    patch: dict | None = None
    if "build_patch" in plan and fixed:
        title = f"Apply verified fixes to {len(fixed)} file(s)"
        markers, patch_result = await run_tool(
            "build_patch", "build_patch: rendering the diff…",
            changes=fixed, title=title, summary=goal,
            findings=[{"title": "verified autofix"}],
        )
        for marker in markers:
            yield marker
        if patch_result and patch_result.ok:
            patch = patch_result.data
            # Rendered here rather than returned by the tool: a patch is only
            # worth a report section when a run asked for one, and the runner is
            # what knows that.
            reports.append(_patch_section(patch_result))

    # ── Step 6: open a PR — only from a diff, only with confirmation ──────────
    if "create_pr" in plan:
        if not patch:
            yield _status("tool_skipped",
                          "create_pr: skipped — no verified patch to open a PR for",
                          tool="create_pr")
        elif not _github_ref(repo_url):
            yield _status("tool_skipped",
                          "create_pr: skipped — set repo_url to a github.com repo to open a PR",
                          tool="create_pr")
        else:
            markers, pr = await run_tool(
                "create_pr", "create_pr: preparing the pull request…",
                repo=_github_ref(repo_url), head=patch["suggested_branch"], base="main",
                title=patch["title"], body=patch["pr_body"], diff=patch["diff"],
                confirm_digest=confirm_digest, repo_url=repo_url,
            )
            for marker in markers:
                yield marker
            if pr and pr.report:
                reports.append(pr.report)

    # ── Report ────────────────────────────────────────────────────────────────
    yield _status("writing", "Assembling findings report…")
    yield _build_report(goal, plan, steps_taken, max_steps, contexts, file_contents, reports)
    yield _status("complete", "Agent run finished")


def _relative_path(source: str, repo_url: str | None) -> str:
    """
    Repo-relative path for `source`, which is either an index id or a disk path.

    Patches name files relative to the repository root; a diff header of
    `a/https://github.com/o/r::src/app.py` is not a patch anyone can apply.
    """
    if not source:
        return ""
    if "::" in source:
        return source.split("::", 1)[1]
    if repo_url and source.startswith(repo_url):
        return source[len(repo_url):].lstrip("/")
    return source if not source.startswith("/") else source.rsplit("/", 1)[-1]


def _patch_section(result: ToolResult) -> str:
    from app.services.agent_tools import patch_markdown

    return patch_markdown(result.data)


def _build_report(
    goal: str,
    plan: list[str],
    steps_taken: int,
    max_steps: int,
    contexts: list[dict],
    file_contents: dict[str, str],
    reports: list[str],
) -> str:
    """Assemble the markdown report. Deterministic: same inputs, same bytes."""
    lines = [
        "# Agent Report",
        "",
        f"**Goal:** {goal}",
        "",
        f"**Steps used:** {steps_taken}/{max_steps} · **Mode:** deterministic ($0, no LLM)",
        "",
        f"**Plan:** {' → '.join(f'`{name}`' for name in plan)}",
        "",
        "## Relevant code",
        "",
    ]
    if contexts:
        for context in contexts[:8]:
            name = context.get("file_name") or context.get("source")
            lines.append(f"- `{name}` ({context.get('language', '?')})")
    else:
        lines.append("_No indexed chunks matched. Index a repo first._")

    lines += ["", "## Key excerpts", ""]
    if file_contents:
        for source, content in list(file_contents.items())[:2]:
            lines.append(f"### `{source.split('/')[-1]}`")
            lines.append("```")
            lines.append(content[:2000])
            lines.append("```")
            lines.append("")
    else:
        lines.append("_No full files could be reconstructed._")
        lines.append("")

    if reports:
        lines.append("## Findings")
        lines.append("")
        lines += [section.rstrip("\n") + "\n" for section in reports if section.strip()]

    lines.append("---")
    lines.append(f"_Generated by the deterministic agent — verify before acting. "
                 f"Plan: {', '.join(plan)}._")
    return "\n".join(lines)


@router.post("/run")
@limiter.limit("10/minute")
async def run_agent(request: Request, body: AgentRunRequest,
                    _: None = Depends(require_api_key)):
    if body.repo_url is not None and not body.repo_url.strip():
        raise HTTPException(status_code=400, detail="repo_url cannot be blank")
    if body.confirm_digest and "create_pr" not in build_plan(body.goal, body.tools):
        raise HTTPException(
            status_code=400,
            detail="confirm_digest was supplied but the goal does not open a pull request. "
                   "Ask for a PR in the goal, or pass tools=[\"create_pr\", …].",
        )
    # The confirmation token is per-request and never logged: it is the only
    # thing standing between a diff preview and a push.
    stream = _run_agent(body.goal, body.repo_url, body.max_steps, body.tools, body.confirm_digest)
    return StreamingResponse(
        stream,
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
