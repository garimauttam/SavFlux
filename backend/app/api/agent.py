"""
agent.py — Deterministic code-task agent API ($0, no LLM required).

The agent decomposes a goal into inspection steps and executes them with
local tools only: hybrid retrieval, file reads, dependency graph, and
impact analysis. No model call is made, so runs are free, fast, and
fully deterministic — the same goal always produces the same report.

Endpoints:
  GET  /agent/tools  — tool catalogue the agent can use
  POST /agent/run    — stream a plan + findings report (SSE-style text)

Stream protocol (same markers as the review stream):
  __STATUS__{...}__STATUS_END__  → step telemetry for the Agent tab
  everything else                → markdown report text
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.deps import require_api_key
from app.limiter import limiter

router = APIRouter(prefix="/agent", tags=["agent"])

TOOL_CATALOGUE = [
    {
        "name": "retrieve_context",
        "description": "Hybrid BM25 + vector search over indexed chunks (RRF fused).",
        "args": ["query", "top_k"],
    },
    {
        "name": "read_file",
        "description": "Read an indexed file's full content from disk or vector store.",
        "args": ["source"],
    },
    {
        "name": "dependency_graph",
        "description": "Import graph for the repo — nodes, edges, hub files.",
        "args": ["repo_url"],
    },
    {
        "name": "blast_radius",
        "description": "Transitive dependents of a file (what breaks if it changes).",
        "args": ["file", "repo_url"],
    },
]


class AgentRunRequest(BaseModel):
    goal: str
    repo_url: str | None = None
    max_steps: int = 6

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


@router.get("/tools")
async def list_tools(_: None = Depends(require_api_key)):
    return {"tools": TOOL_CATALOGUE, "total": len(TOOL_CATALOGUE)}


def _status(step: str, message: str, **extra: object) -> str:
    payload = {"step": step, "message": message, **extra}
    return f"__STATUS__{json.dumps(payload)}__STATUS_END__\n"


def _read_indexed_file(source: str) -> str:
    """Read an indexed file from disk, falling back to ChromaDB chunks.

    Same strategy as POST /review/file: temp clone dirs are deleted after
    ingest, so disk reads often miss and chunk reconstruction saves us.
    """
    try:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        pass
    try:
        from app.services.ingestion_service import _get_vectorstore
        from app.services.chunk_reconstruction import reconstruct_chunks
        vs = _get_vectorstore()
        results = vs._collection.get(
            where={"source": source},
            include=["documents", "metadatas"],
        )
        docs = results.get("documents") or []
        metas = results.get("metadatas") or []
        if docs and metas:
            return reconstruct_chunks(zip(metas, docs))
    except Exception:
        pass
    return ""


async def _run_agent(goal: str, repo_url: str | None, max_steps: int):
    """Plan → inspect → report. Yields status markers + markdown."""
    steps_taken = 0

    def budget() -> bool:
        return steps_taken < max_steps

    yield _status("starting", f"Goal: {goal[:100]}", mode="deterministic")

    # ── Step 1: retrieve relevant context ──────────────────────────────────
    yield _status("tool", "retrieve_context: searching indexed code…",
                 tool="retrieve_context")
    contexts: list[dict] = []
    if budget():
        steps_taken += 1
        try:
            from app.services.retrieval_service import hybrid_search_with_sources
            docs = await asyncio.to_thread(
                hybrid_search_with_sources, goal, repo_url, None, 8
            )
            for d in (docs or [])[:8]:
                meta = getattr(d, "metadata", {}) or {}
                contexts.append({
                    "source": meta.get("source", ""),
                    "file_name": meta.get("file_name", ""),
                    "language": meta.get("language", ""),
                    "snippet": (getattr(d, "page_content", "") or "")[:600],
                })
            yield _status("tool_done", f"retrieve_context: {len(contexts)} chunks",
                         tool="retrieve_context", count=len(contexts))
        except Exception as e:
            yield _status("tool_error", f"retrieve_context failed: {str(e)[:120]}",
                         tool="retrieve_context")

    # ── Step 2: read the top files in full ────────────────────────────────
    file_contents: dict[str, str] = {}
    if budget() and contexts:
        seen: set[str] = set()
        for c in contexts:
            src = c.get("source", "")
            if not src or src in seen or not budget():
                continue
            seen.add(src)
            steps_taken += 1
            yield _status("tool", f"read_file: {c.get('file_name') or src}",
                         tool="read_file", file=src)
            try:
                from app.services.chunk_reconstruction import reconstruct_file
                content = await asyncio.to_thread(reconstruct_file, src)
                if content:
                    file_contents[src] = content[:20000]
                    yield _status("tool_done",
                                 f"read_file: {len(content)} chars",
                                 tool="read_file", file=src)
                else:
                    yield _status("tool_error", "read_file: empty or missing",
                                 tool="read_file", file=src)
            except Exception as e:
                yield _status("tool_error", f"read_file failed: {str(e)[:120]}",
                             tool="read_file", file=src)

    # ── Step 3: dependency context for the most relevant file ─────────────
    blast: dict | None = None
    top_source = contexts[0].get("source") if contexts else None
    if budget() and top_source:
        steps_taken += 1
        yield _status("tool", "blast_radius: mapping dependents…",
                     tool="blast_radius", file=top_source)
        try:
            from app.services.dep_graph import build_dependency_graph, get_blast_radius
            graph = await asyncio.to_thread(build_dependency_graph, repo_url)
            blast = await asyncio.to_thread(get_blast_radius, graph, top_source)
            n = len((blast or {}).get("impacted_files", []))
            yield _status("tool_done", f"blast_radius: {n} dependents",
                         tool="blast_radius", count=n)
        except Exception as e:
            yield _status("tool_error", f"blast_radius failed: {str(e)[:120]}",
                         tool="blast_radius")

    # ── Report ────────────────────────────────────────────────────────────
    yield _status("writing", "Assembling findings report…")
    lines = [
        f"# Agent Report",
        "",
        f"**Goal:** {goal}",
        "",
        f"**Steps used:** {steps_taken}/{max_steps} · **Mode:** deterministic ($0, no LLM)",
        "",
        "## Relevant code",
        "",
    ]
    if contexts:
        for c in contexts[:8]:
            name = c.get("file_name") or c.get("source")
            lines.append(f"- `{name}` ({c.get('language', '?')})")
    else:
        lines.append("_No indexed chunks matched. Index a repo first._")
    lines += ["", "## Key excerpts", ""]
    if file_contents:
        for src, content in list(file_contents.items())[:3]:
            lines.append(f"### `{src.split('/')[-1]}`")
            lines.append("```")
            lines.append(content[:3000])
            lines.append("```")
            lines.append("")
    else:
        lines.append("_No full files could be reconstructed._")
        lines.append("")
    if blast:
        impacted = blast.get("impacted_files", [])
        lines.append("## Change risk (most relevant file)")
        lines.append("")
        lines.append(f"- Risk level: **{blast.get('risk_level', 'unknown')}** "
                     f"(score {blast.get('risk_score', 0)}/10)")
        lines.append(f"- Direct + transitive dependents: **{len(impacted)}**")
        for f in impacted[:10]:
            lines.append(f"  - `{f}`")
        lines.append("")
    lines.append("---")
    lines.append("_Generated by the deterministic agent — verify before acting._")
    yield "\n".join(lines)
    yield _status("complete", "Agent run finished")


@router.post("/run")
@limiter.limit("10/minute")
async def run_agent(request: Request, body: AgentRunRequest,
                    _: None = Depends(require_api_key)):
    if body.repo_url is not None and not body.repo_url.strip():
        raise HTTPException(status_code=400, detail="repo_url cannot be blank")
    return StreamingResponse(
        _run_agent(body.goal, body.repo_url, body.max_steps),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
