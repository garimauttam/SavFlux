"""
agent_tools.py — The tools the deterministic agent can actually call.

WHY A SEPARATE MODULE
---------------------
`app/api/agent.py` used to hold both the HTTP surface and the work: `_read_indexed_file`
lived inside the route file, and the three "write" tools below did not exist at
all. The result was a catalogue that advertised four read-only tools while three
finished endpoints — `autofix`, `build-patch`, `create-pr` — sat unreachable
because nothing in the product could call them.

Splitting the tools out fixes that and keeps the route file about HTTP. Each tool
here is a plain async function returning a `ToolResult`, so it can be tested
without a client, a stream, or a plan.

THE SAFETY RULE
---------------
`create_pr` writes to a real repository. It therefore never acts on the agent's
own judgement: it builds a *plan* (branch name, PR body, `gh` command, patch) and
only calls the GitHub API when the caller supplies `confirm_digest` equal to the
digest of the exact diff that was displayed. The digest is a property of the
diff, so confirming it is proof the caller saw that diff — and a stale or
swapped diff fails the check instead of being pushed.

Cost: every tool is stdlib or an existing in-repo service. No model call, no
network except `create_pr` when a token is configured and a digest is confirmed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Tools that can change something outside the process. The catalogue flags them
#: so a UI can warn before a user runs one, and so a test can assert the set is
#: exactly what we intend to be mutating.
MUTATING_TOOLS = frozenset({"create_pr"})


# ── Catalogue ─────────────────────────────────────────────────────────────────
#
# `args_schema` is the source of truth; `args` is derived from it and kept
# because it is the shape the agent UI already consumes.

TOOL_SPECS: list[dict] = [
    {
        "name": "retrieve_context",
        "description": "Hybrid BM25 + vector search over indexed chunks (RRF fused).",
        "args_schema": [
            {"name": "query", "type": "string", "required": True,
             "description": "Natural-language or symbol query."},
            {"name": "top_k", "type": "integer", "required": False,
             "description": "Chunks to return (default 8, max 50)."},
        ],
    },
    {
        "name": "read_file",
        "description": "Read an indexed file's full content from disk or the vector store.",
        "args_schema": [
            {"name": "source", "type": "string", "required": True,
             "description": "Indexed source id or repo-relative path."},
        ],
    },
    {
        "name": "dependency_graph",
        "description": "Import graph for the repo — nodes, edges, hub files.",
        "args_schema": [
            {"name": "repo_url", "type": "string", "required": False,
             "description": "Repo to scope the graph to; defaults to the whole index."},
        ],
    },
    {
        "name": "blast_radius",
        "description": "Transitive dependents of a file (what breaks if it changes).",
        "args_schema": [
            {"name": "file", "type": "string", "required": True,
             "description": "File to trace dependents for."},
            {"name": "repo_url", "type": "string", "required": False,
             "description": "Repo to scope the graph to; defaults to the whole index."},
        ],
    },
    {
        "name": "autofix",
        "description": (
            "Deterministic repairs for findings that have exactly one correct form, "
            "each re-parsed and re-analysed before it is kept. Python only."
        ),
        "args_schema": [
            {"name": "file", "type": "string", "required": True,
             "description": "Repo-relative path of the Python file to repair."},
            {"name": "source", "type": "string", "required": False,
             "description": "Exact indexed source id, when the caller has it."},
            {"name": "repo_url", "type": "string", "required": False,
             "description": "Repo the file belongs to, for index lookups."},
        ],
    },
    {
        "name": "build_patch",
        "description": (
            "Render proposed file contents as one git-applicable unified diff, "
            "with per-file counts, a digest and a ready PR body."
        ),
        "args_schema": [
            {"name": "changes", "type": "array", "required": True,
             "description": "Objects with `path` and `content` (plus optional `original`)."},
            {"name": "title", "type": "string", "required": False,
             "description": "PR title; also the source of the suggested branch name."},
            {"name": "summary", "type": "string", "required": False,
             "description": "One paragraph for the PR body: what changed and why."},
        ],
    },
    {
        "name": "create_pr",
        "description": (
            "Open a pull request for a built patch. Returns a reviewable plan by "
            "default; pushes only when confirm_digest matches the diff digest."
        ),
        "args_schema": [
            {"name": "repo", "type": "string", "required": True,
             "description": "'owner/name' or a github.com URL."},
            {"name": "head", "type": "string", "required": True,
             "description": "Branch that already contains the change."},
            {"name": "base", "type": "string", "required": False,
             "description": "Branch to merge into. Default: main."},
            {"name": "title", "type": "string", "required": False,
             "description": "PR title."},
            {"name": "body", "type": "string", "required": False,
             "description": "PR description, usually `build_patch`'s `pr_body`."},
            {"name": "confirm_digest", "type": "string", "required": False,
             "description": "Digest of the exact diff the user confirmed. Required to push."},
            {"name": "approval_token", "type": "string", "required": False,
             "description": (
                 "For changes the risk gate flags: the `risk.approval_token` from a "
                 "previous build_patch or create_pr plan. Bound to that exact change."
             )},
            {"name": "approval_reason", "type": "string", "required": False,
             "description": (
                 "One sentence, written by the approver, saying why this risky change "
                 "should go out. Required alongside approval_token; recorded in the ledger."
             )},
        ],
    },
]

#: Public catalogue: names + descriptions + typed schemas, `args` derived.
TOOL_CATALOGUE: list[dict] = [
    {
        "name": spec["name"],
        "description": spec["description"],
        "args": [arg["name"] for arg in spec["args_schema"]],
        "args_schema": spec["args_schema"],
        "mutating": spec["name"] in MUTATING_TOOLS,
    }
    for spec in TOOL_SPECS
]

TOOL_BY_NAME: dict[str, dict] = {tool["name"]: tool for tool in TOOL_CATALOGUE}

TOOL_NAMES: list[str] = [tool["name"] for tool in TOOL_CATALOGUE]


@dataclass
class ToolResult:
    """
    One tool invocation's outcome.

    `data` is free-form and meant to be spread into a `__STATUS__` marker, so it
    must stay JSON-serialisable — the UI reads these fields directly.
    """

    tool: str
    ok: bool
    message: str
    data: dict = field(default_factory=dict)
    #: Markdown appended to the run report. Empty for tools whose output is
    #: telemetry rather than prose.
    report: str = ""

    #: Payloads that belong in the report, not in a status line. A 20 KB diff
    #: inside `__STATUS__{...}__STATUS_END__` would sit in the UI's step list and
    #: be re-parsed on every chunk, for no reader benefit.
    BULKY_KEYS = ("content", "diff", "pr_body", "graph", "contexts", "patch", "original")

    def status_data(self) -> dict:
        """
        Scalar-only view of `data`, safe to inline in a status marker.

        Returning a dict (rather than a formatted marker) keeps this module free
        of the wire protocol: `app.api.agent` owns the `__STATUS__` framing.
        """
        return {
            key: value
            for key, value in self.data.items()
            if key not in self.BULKY_KEYS and isinstance(value, (str, int, float, bool, type(None)))
        }


# ── Read-only tools ───────────────────────────────────────────────────────────


async def retrieve_context(query: str, repo_url: str | None = None, top_k: int = 8) -> ToolResult:
    """
    Hybrid search; returns the top chunks as plain dicts for the report.

    Calls the retrieval-only entry point in `retrieval_service` — the same
    dense + BM25 + RRF + cross-encoder pipeline chat uses, minus the LLM. The
    name this used to import (`hybrid_search_with_sources`) never existed, so
    the step failed on every run while still emitting a "searched" status.
    """
    top_k = max(1, min(int(top_k or 8), 50))
    try:
        from app.services.retrieval_service import retrieve_chunks

        docs = await retrieve_chunks(query, repo_url=repo_url, top_k=top_k)
    except Exception as exc:  # noqa: BLE001 — a failed search is a step, not a crash
        return ToolResult("retrieve_context", False, f"retrieve_context failed: {str(exc)[:120]}")

    contexts: list[dict] = []
    for doc in docs or []:
        meta = getattr(doc, "metadata", {}) or {}
        contexts.append({
            "source": meta.get("source", ""),
            "file_name": meta.get("file_name", ""),
            "language": meta.get("language", ""),
            "snippet": (getattr(doc, "page_content", "") or "")[:600],
        })
    return ToolResult(
        "retrieve_context",
        True,
        f"retrieve_context: {len(contexts)} chunks",
        data={"count": len(contexts), "contexts": contexts},
    )


async def read_file(source: str, repo_url: str | None = None) -> ToolResult:
    """
    Full file text, from disk when it still exists and from the index otherwise.

    Temp clones are deleted after ingest, so the disk read usually misses — the
    index is the durable copy. This previously called a `reconstruct_file`
    helper that does not exist, so every read raised ImportError and the step
    reported an error while looking like it had run.
    """
    import asyncio

    from app.services.indexed_content import read_indexed_file

    try:
        with open(source, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except (FileNotFoundError, OSError):
        content = await asyncio.to_thread(read_indexed_file, source, repo_url=repo_url)

    if not content:
        return ToolResult("read_file", False, "read_file: empty or missing",
                          data={"file": source, "chars": 0})
    return ToolResult("read_file", True, f"read_file: {len(content)} chars",
                      data={"file": source, "chars": len(content), "content": content})


async def dependency_graph(repo_url: str | None = None) -> ToolResult:
    """Import graph for the repo."""
    import asyncio

    try:
        from app.services.dep_graph import build_dependency_graph

        graph = await asyncio.to_thread(build_dependency_graph, repo_url)
    except Exception as exc:  # noqa: BLE001
        return ToolResult("dependency_graph", False, f"dependency_graph failed: {str(exc)[:120]}")

    nodes = len((graph or {}).get("nodes", []))
    edges = len((graph or {}).get("edges", []))
    return ToolResult("dependency_graph", True, f"dependency_graph: {nodes} nodes / {edges} edges",
                      data={"nodes": nodes, "edges": edges, "graph": graph})


async def blast_radius(file: str, repo_url: str | None = None,
                       graph: dict | None = None) -> ToolResult:
    """Transitive dependents of one file, reusing a graph when one was built."""
    import asyncio

    try:
        from app.services.dep_graph import build_dependency_graph, get_blast_radius

        if graph is None:
            graph = await asyncio.to_thread(build_dependency_graph, repo_url)
        blast = await asyncio.to_thread(get_blast_radius, graph, file)
    except Exception as exc:  # noqa: BLE001
        return ToolResult("blast_radius", False, f"blast_radius failed: {str(exc)[:120]}",
                          data={"file": file})

    impacted = (blast or {}).get("impacted_files", [])
    return ToolResult(
        "blast_radius",
        True,
        f"blast_radius: {len(impacted)} dependents",
        # `impacted_files` is a list, so `ToolResult.status_data()` keeps it out of
        # the status line while `agent_run.result_preview` can still name the
        # dependents on the card. A count alone tells the user the tool ran; the
        # names are what make the answer checkable.
        data={"file": file, "count": len(impacted), "impacted_files": list(impacted)},
        report=blast_markdown(file, blast or {}),
    )


# ── Tools that change something ───────────────────────────────────────────────


async def autofix(file: str, source: str | None = None,
                  repo_url: str | None = None) -> ToolResult:
    """
    Apply every safe deterministic repair and return the verified patch.

    Thin on purpose: the pipeline (analyse → fix → re-verify → diff) lives in
    `fix_service`, which is also what `POST /review/autofix` calls. The agent
    therefore cannot ship a fix that the endpoint would have rejected.
    """
    from app.services.fix_service import FixError, apply_fixes

    try:
        outcome = await apply_fixes(file, source=source, repo_url=repo_url)
    except FixError as exc:
        return ToolResult("autofix", False, f"autofix: {str(exc)[:160]}",
                          data={"file": file, "fixed": False})

    if not outcome.changed:
        detail = outcome.skipped[0] if outcome.skipped else "no auto-fixable findings"
        return ToolResult(
            "autofix",
            True,
            f"autofix: nothing safe to fix ({detail[:80]})",
            data={"file": file, "fixed": False, "skipped": len(outcome.skipped)},
        )

    data = {
        "file": file,
        "fixed": True,
        "count": len(outcome.fixes),
        "score_before": outcome.score_before,
        "score_after": outcome.score_after,
        "digest": outcome.digest,
        # The repaired source is carried in `data` so `build_patch` can consume
        # it in the same run without a second read of the index.
        "content": outcome.content,
        "path": file,
    }
    return ToolResult(
        "autofix",
        True,
        f"autofix: {len(outcome.fixes)} fix(es), score {outcome.score_before} → {outcome.score_after}",
        data=data,
        report=fix_markdown(outcome),
    )


async def build_patch(changes: list[dict], title: str = "", summary: str = "",
                      findings: list[dict] | None = None,
                      context: int = 3) -> ToolResult:
    """
    Turn file contents into one reviewable diff.

    `changes` entries are `{path, content, original?}`. A missing `original` is
    resolved from the index, so a caller that only has the new text — which is
    all the LLM write path produces — still gets a correct diff rather than a
    whole-file addition.
    """
    import asyncio

    from app.services.patch_service import (
        FileChange,
        PatchError,
        build_patch as _build,
        build_pr_body,
        suggest_branch_name,
    )

    def _run() -> dict:
        resolved: list[FileChange] = []
        for change in changes:
            path = (change or {}).get("path", "")
            original = (change or {}).get("original")
            if original is None:
                # Not supplied — reconstruct from the index. A file that is not
                # indexed is treated as new rather than failing the request.
                original = _read_indexed(path) or None
            resolved.append(FileChange(path=path, original=original,
                                       modified=(change or {}).get("content", "")))

        result = _build(resolved, context=max(0, min(int(context or 3), 10)))
        pr_title = (title or "").strip() or "SavFlux: apply review fixes"
        return {
            **result.to_dict(),
            "title": pr_title,
            "suggested_branch": suggest_branch_name(pr_title),
            "pr_body": build_pr_body(summary, result, findings=findings or []),
        }

    if not changes:
        return ToolResult("build_patch", False, "build_patch: no changes supplied")

    try:
        data = await asyncio.to_thread(_run)
    except PatchError as exc:
        return ToolResult("build_patch", False, f"build_patch: {exc}")

    return ToolResult(
        "build_patch",
        True,
        f"build_patch: {data['files_changed']} file(s), "
        f"+{data['additions']}/-{data['deletions']}, digest {data['digest']}",
        data=data,
    )


def _read_indexed(path: str) -> str:
    """Index lookup for build_patch — one implementation, in indexed_content."""
    from app.services.indexed_content import read_indexed_file

    return read_indexed_file(path)


def _changed_file_contents(paths: list[str]) -> dict[str, str]:
    """
    Current content of the files a diff touches, for the verification scratch repo.

    `git apply` matches hunks against content and line numbers, so verifying a
    patch against an empty tree only proves the hunks are well-formed. Files that
    are not indexed (new files, or a paste) are simply absent, which is correct:
    their hunks are additions.
    """
    contents: dict[str, str] = {}
    for path in paths or []:
        content = _read_indexed(path)
        if content:
            contents[path] = content
    return contents


def _changed_file_contents(paths: list[str]) -> dict[str, str]:
    """
    Current content of the files a diff touches, for the verification scratch repo.

    `git apply` matches hunks against content and line numbers, so verifying a
    patch against an empty tree would only prove the hunks are well-formed. Files
    that are not indexed (new files, or a paste) are simply absent, which is
    correct: their hunks are additions.
    """
    contents: dict[str, str] = {}
    for path in paths or []:
        content = _read_indexed(path)
        if content:
            contents[path] = content
    return contents


async def create_pr(
    repo: str,
    head: str,
    base: str = "main",
    title: str = "",
    body: str = "",
    diff: str = "",
    confirm_digest: str | None = None,
    repo_url: str | None = None,
    approval_token: str | None = None,
    approval_reason: str | None = None,
) -> ToolResult:
    """
    Open a pull request — or explain exactly how to, without touching anything.

    Two independent gates, because they answer different questions:

      1. `confirm_digest` proves a human saw *this diff*. Without it the tool
         produces a plan and pushes nothing, so a model decision alone can never
         open a PR.
      2. The risk policy asks whether the change *should* go out: parsed findings,
         blast radius, sensitive paths, whether the patch was verified, whether the
         index is stale. Above the approval threshold a token bound to this exact
         change plus a written reason is required; above the block threshold the
         tool refuses and returns the manual command instead.

    The second gate exists because the first one can be satisfied by a model that
    was told the digest — "did you look at it?" is not "should this be pushed?".
    """
    from app.services.pr_service import (
        build_gh_command,
        create_pr_via_api,
        parse_repo_ref,
        validate_branches,
    )

    try:
        repo_slug = parse_repo_ref(repo or repo_url or "")
        head, base = validate_branches(head, base)
    except ValueError as exc:
        return ToolResult("create_pr", False, f"create_pr: {exc}", data={"status": "invalid"})

    from app.services.patch_service import digest_of

    patch_title = (title or "").strip() or "SavFlux: apply review fixes"
    digest = digest_of(diff) if diff else ""

    # ── Risk policy ──────────────────────────────────────────────────────────
    # Assessed before the digest comparison so the response can explain a refusal
    # on risk grounds without also implying the confirmation was the problem.
    plan = {
        "status": "manual",
        "repo": repo_slug,
        "head": head,
        "base": base,
        "title": patch_title,
        "digest": digest,
        "gh_command": build_gh_command(repo_slug, head, base, patch_title, body),
        "patch": diff or None,
    }

    # ── Risk policy ──────────────────────────────────────────────────────────
    # Assessed before the digest comparison so the response can explain a refusal
    # on risk grounds without also implying the confirmation was the problem.
    from app.services.risk_policy import (
        ACTION_CREATE_PR,
        apply_diff,
        gate_change,
        record_decision,
    )

    # Applied in a scratch repo, then parsed: the verification answer and the
    # evidence for the risk score come from the same run.
    risk_files: list[dict] = []
    verification = {"verified": None, "detail": "no diff supplied", "files": 0}
    if diff:
        import asyncio

        from app.services.impact_analyzer import analyze_diff

        touched = analyze_diff(diff).get("changed_files", [])
        originals = await asyncio.to_thread(_changed_file_contents, touched)
        applied = await asyncio.to_thread(apply_diff, diff, originals)
        verification = {
            "verified": applied["verified"],
            "detail": applied["detail"],
            "files": len(applied.get("files") or {}),
        }
        risk_files = [
            {"path": path, "content": content, "original": originals.get(path, "")}
            for path, content in (applied.get("files") or {}).items()
        ]

    risk, decision = gate_change(
        action=ACTION_CREATE_PR,
        diff=diff,
        files=risk_files or None,
        repo_url=repo_url,
        verified=verification.get("verified"),
        approve_token=(approval_token or "").strip(),
        approval_reason=(approval_reason or "").strip(),
        verification_detail=verification.get("detail", ""),
    )
    plan["risk"] = risk.to_dict()
    plan["verification"] = verification
    plan["policy"] = decision.to_dict(include_signals=False)

    if not diff:
        plan["reason"] = "No diff to push — build a patch first."
        return ToolResult("create_pr", False, "create_pr: no diff to push", data=plan)

    if decision.blocked:
        plan["reason"] = decision.reason
        record_decision(decision, outcome="blocked", repo=repo_slug, actor="agent")
        return ToolResult(
            "create_pr",
            False,
            f"create_pr: blocked by risk policy ({risk.score}/10) — plan returned, nothing pushed",
            data=plan,
            report=pr_markdown(plan),
        )

    if decision.requires_approval and not decision.approved:
        plan["reason"] = decision.reason
        record_decision(decision, outcome="refused_no_approval", repo=repo_slug, actor="agent")
        return ToolResult(
            "create_pr",
            False,
            f"create_pr: risk {risk.score}/10 needs approval — plan returned, nothing pushed",
            data=plan,
            report=pr_markdown(plan),
        )

    confirmed = bool(confirm_digest) and confirm_digest == digest
    if not confirmed:
        plan["reason"] = (
            "Confirmation required: show the diff, then re-run with "
            f"confirm_digest={digest!r} to open the PR. Nothing was pushed."
        )
        return ToolResult(
            "create_pr",
            True,
            f"create_pr: plan ready for {repo_slug} ({head} → {base}), nothing pushed",
            data=plan,
            report=pr_markdown(plan),
        )

    if not os.getenv("GITHUB_TOKEN", "").strip():
        plan["reason"] = "GITHUB_TOKEN not configured — run the command below (gh CLI) to open the PR."
        record_decision(decision, outcome="manual_plan", repo=repo_slug, actor="agent")
        return ToolResult("create_pr", True, "create_pr: no GITHUB_TOKEN, manual plan returned",
                          data=plan, report=pr_markdown(plan))

    try:
        created = await create_pr_via_api(repo_slug, head, base, patch_title, body)
    except RuntimeError as exc:
        plan["reason"] = f"GitHub API failed ({exc}); use the command below instead."
        return ToolResult("create_pr", False, f"create_pr: GitHub API failed ({str(exc)[:100]})",
                          data=plan, report=pr_markdown(plan))

    created_data = {**plan, "status": "created", **created}
    created_data.pop("patch", None)  # the PR itself is the artifact now
    record_decision(decision, outcome="created", repo=repo_slug, actor="agent",
                    detail=f"PR #{created.get('number')}")
    return ToolResult(
        "create_pr",
        True,
        f"create_pr: opened #{created.get('number')} on {repo_slug}",
        data=created_data,
        report=pr_markdown(created_data),
    )


# ── Report fragments ──────────────────────────────────────────────────────────


def fix_markdown(outcome) -> str:
    """Report section for one file's verified fixes."""
    lines = [
        f"### `{outcome.path}` — verified fixes",
        "",
        f"Risk score **{outcome.score_before} → {outcome.score_after}**, "
        f"findings {outcome.findings_before} → {outcome.findings_after}.",
        "",
    ]
    for fix in outcome.fixes:
        lines.append(f"- `L{fix.line}` **{fix.description}** (`{fix.rule_id}`)")
        lines.append(f"  - before: `{fix.before}`")
        lines.append(f"  - after:  `{fix.after}`")
    if outcome.skipped:
        lines += ["", "_Skipped (failed verification — fix these by hand):_"]
        lines += [f"- {reason}" for reason in outcome.skipped]
    lines.append("")
    return "\n".join(lines)


def blast_markdown(file: str, blast: dict) -> str:
    impacted = blast.get("impacted_files", [])
    lines = [
        "## Change risk",
        "",
        f"- Target: `{file}`",
        f"- Risk level: **{blast.get('risk_level', 'unknown')}** "
        f"(score {blast.get('risk_score', 0)}/10)",
        f"- Direct + transitive dependents: **{len(impacted)}**",
    ]
    lines += [f"  - `{name}`" for name in impacted[:10]]
    lines.append("")
    return "\n".join(lines)


def patch_markdown(data: dict, max_chars: int = 20_000) -> str:
    """Report section for a built patch. Truncated, because reports are read."""
    diff = data.get("diff", "")
    truncated = len(diff) > max_chars
    lines = [
        "## Patch",
        "",
        f"**{data.get('files_changed', 0)} file(s)**, "
        f"+{data.get('additions', 0)}/−{data.get('deletions', 0)} · "
        f"digest `{data.get('digest', '')}`",
        "",
        "| File | Status | +/- |",
        "| --- | --- | --- |",
    ]
    for entry in data.get("files", []):
        lines.append(
            f"| `{entry.get('path')}` | {entry.get('status')} | "
            f"+{entry.get('additions', 0)}/−{entry.get('deletions', 0)} |"
        )
    lines += ["", f"- Suggested branch: `{data.get('suggested_branch', '')}`", "",
              "```diff", diff[:max_chars].rstrip("\n"), "```"]
    if truncated:
        lines.append(f"_Diff truncated at {max_chars} characters — use the patch download for the full file._")
    lines.append("")
    return "\n".join(lines)


def pr_markdown(data: dict) -> str:
    """Report section for a PR plan or a created PR."""
    if data.get("status") == "created":
        return "\n".join([
            "## Pull request",
            "",
            f"Opened **#{data.get('number')}** on `{data.get('repo')}` — {data.get('url', '')}",
            "",
        ])
    return "\n".join([
        "## Pull request — not opened",
        "",
        f"_{data.get('reason', 'Review and confirm before any push.')}_",
        "",
        "```bash",
        data.get("gh_command", ""),
        "```",
        "",
    ])


# ── Dispatch table ────────────────────────────────────────────────────────────

#: name → callable. Kept explicit rather than introspected so the set of things
#: an agent can be asked to do is greppable.
DISPATCH: dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "retrieve_context": retrieve_context,
    "read_file": read_file,
    "dependency_graph": dependency_graph,
    "blast_radius": blast_radius,
    "autofix": autofix,
    "build_patch": build_patch,
    "create_pr": create_pr,
}
