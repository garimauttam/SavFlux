"""
multi_review_agent.py — Orchestrates cross-file-aware code review across multiple files.

ARCHITECTURE (v2 — cross-file context + full parallelism):

  Phase 0 — Repo context build  (instant, deterministic, no LLM)
    For every file in the batch, extract:
      • exported symbols (functions, classes, types)
      • import statements (which other files it depends on)
    Produces a compact per-file "repo context" block injected into each LLM prompt.
    This lets the LLM say "reviewPanel.tsx imports useMultiReview, which has this
    issue in multi_review_agent.py" without reviewing all files with the LLM.

  Phase 1 — Parallel static triage  (instant, CPU-only)
    Every file that falls outside the LLM review budget gets a full deterministic
    review (structure, security patterns, complexity, score) — NOT a placeholder.
    All triage runs simultaneously via asyncio.gather.
    Results are streamed to the frontend as soon as they complete.

  Phase 2 — Concurrent LLM reviews  (async, bounded by semaphore)
    Top-ranked files get a full LLM pass with repo_context injected.
    Reviews run concurrently up to review_concurrency (default 3).
    Each review streams tokens as it arrives — no waiting for all to finish.

  Phase 3 — Repo summary  (one final LLM call)
    Synthesises all per-file findings into an overall health assessment.
    Falls back to a rich deterministic summary if the LLM is unavailable.

SECTION WIRE FORMAT (unchanged — frontend already handles this):
  __SECTION_START__{json}__SECTION_END__   → open a new card
  __STATUS__message{json}__STATUS_END__    → progress update
  plain text                               → review content for current card
"""

import asyncio
import json
import logging
import re
from functools import lru_cache
from typing import AsyncGenerator

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.llm_factory import get_chat_llm
from app.services.review_agent import stream_code_review, stream_fast_code_review
from app.core.config import get_settings

logger = logging.getLogger(__name__)

FILE_SECTION_MARKER    = "---FILE_SECTION---"
SUMMARY_SECTION_MARKER = "---REPO_SUMMARY---"

# Per-file LLM review timeout.
# Enough headroom for deepseek-coder:33b on a CPU-only machine.
# On timeout, the file falls back to _static_triage rather than blocking the semaphore.
_LLM_TIMEOUT = 120  # seconds


# ── Section payload helper ─────────────────────────────────────────────────────

def _section_payload(file_info: dict) -> str:
    return json.dumps({
        "id":        file_info.get("file_path") or file_info["file_name"],
        "file_name": file_info["file_name"],
    })


# ── Phase 0: repo context build ───────────────────────────────────────────────

def _build_repo_context(files: list[dict]) -> dict[str, str]:
    """
    Fast deterministic pass over all files.

    Returns a mapping  file_name → context_block  where context_block is a
    compact paragraph injected into each file's LLM prompt.  It lists:
      • what this file exports (functions, classes, types)
      • which other files in the batch import from this file
      • which files in the batch this file imports from

    Pure regex — no LLM call, runs in <50ms even for 100 files.
    """
    # Patterns for Python / JS / TS
    EXPORT_PY  = re.compile(r"^\s*(async\s+def|def|class)\s+(\w+)", re.MULTILINE)
    EXPORT_JS  = re.compile(
        r"export\s+(?:default\s+)?(?:(?:async\s+)?function\s+(\w+)|class\s+(\w+)|const\s+(\w+)|type\s+(\w+)|interface\s+(\w+))",
        re.MULTILINE,
    )
    IMPORT_PY  = re.compile(r"^(?:from\s+([\w./]+)\s+import|import\s+([\w./]+))", re.MULTILINE)
    IMPORT_JS  = re.compile(r"""(?:import|from)\s+['"]([^'"]+)['"]""", re.MULTILINE)

    # ── collect exports per file ───────────────────────────────────────────────
    file_exports: dict[str, list[str]] = {}  # file_name → [exported symbol, ...]
    for f in files:
        name    = f["file_name"]
        content = f.get("content", "")
        lang    = f.get("language", "")
        exports: list[str] = []
        if lang in ("py",):
            exports = [m.group(2) for m in EXPORT_PY.finditer(content)]
        elif lang in ("js", "jsx", "ts", "tsx"):
            for m in EXPORT_JS.finditer(content):
                sym = next((g for g in m.groups() if g), None)
                if sym:
                    exports.append(sym)
        # truncate to keep context compact
        file_exports[name] = exports[:20]

    # ── collect imports per file and resolve to file names in the batch ────────
    batch_names = {f["file_name"] for f in files}

    def _resolve_imports(content: str, lang: str) -> list[str]:
        """Return file names in the batch that this file imports from."""
        raw: list[str] = []
        if lang == "py":
            for m in IMPORT_PY.finditer(content):
                raw.append((m.group(1) or m.group(2) or "").split(".")[-1])
        elif lang in ("js", "jsx", "ts", "tsx"):
            for m in IMPORT_JS.finditer(content):
                path = m.group(1)
                # keep only relative imports; strip extension
                if path.startswith("."):
                    raw.append(re.split(r"[/.]", path)[-1])
        # resolve to actual file names present in the batch
        matched: list[str] = []
        for stem in raw:
            for bname in batch_names:
                if stem and bname.startswith(stem):
                    matched.append(bname)
        return list(dict.fromkeys(matched))[:10]  # deduplicate, cap

    file_imports: dict[str, list[str]] = {
        f["file_name"]: _resolve_imports(f.get("content", ""), f.get("language", ""))
        for f in files
    }

    # ── build reverse map: who imports this file ───────────────────────────────
    imported_by: dict[str, list[str]] = {f["file_name"]: [] for f in files}
    for importer, deps in file_imports.items():
        for dep in deps:
            if dep in imported_by:
                imported_by[dep].append(importer)

    # ── assemble one context block per file ────────────────────────────────────
    context: dict[str, str] = {}
    for f in files:
        name    = f["file_name"]
        exports = file_exports.get(name, [])
        deps    = file_imports.get(name, [])
        users   = imported_by.get(name, [])

        lines: list[str] = [f"File: {name}"]
        if exports:
            lines.append(f"Exports: {', '.join(exports)}")
        if deps:
            lines.append(f"Imports from (in batch): {', '.join(deps)}")
        if users:
            lines.append(f"Used by (in batch): {', '.join(users)}")
        if len(lines) == 1:
            # no cross-file signal — skip injecting context for this file
            context[name] = ""
        else:
            context[name] = "\n".join(lines)

    return context


# ── Triage scoring (determines who gets an LLM review) ────────────────────────

def _triage_score(file_info: dict) -> int:
    """Higher score → higher priority for the limited LLM review budget."""
    name     = file_info["file_name"].lower()
    language = file_info.get("language", "").lower()
    content  = file_info.get("content", "")
    score    = 1

    # PERF 4: tiny files (<100 chars) with no positive language signal get
    # a heavy penalty — they are empty stubs, blank configs, or lock-file
    # fragments that produce no useful LLM signal.  Files with high-value
    # keywords (auth, security, etc.) still go through even if short.
    if len(content) < 100 and language not in {"py", "js", "jsx", "ts", "tsx", "go", "java", "rs", "rb"}:
        score -= 8

    if language in {"py", "js", "jsx", "ts", "tsx", "go", "java", "rs", "rb"}:
        score += 4
    if any(t in name for t in ("auth", "security", "api", "service", "agent", "retriev", "ingest")):
        score += 3
    if any(t in content.lower() for t in ("password", "token", "secret", "subprocess", "sql", "exec(")):
        score += 3
    if name.endswith(("lock", ".lock")) or "package-lock" in name:
        score -= 5
    if language in {"json", "yaml", "yml", "md", "txt"}:
        score -= 2
    return score


# ── Available Ollama models cache ─────────────────────────────────────────────

@lru_cache(maxsize=1)
def _available_ollama_models() -> frozenset[str]:
    """
    Return the set of model names currently available in the local Ollama instance.

    Cached with lru_cache — called once per server process so we don't hammer
    the Ollama API on every file in a batch review.

    Returns an empty frozenset if Ollama is unreachable (safe fallback —
    callers treat an absent model as "use the full provider instead").
    """
    try:
        import httpx
        settings = get_settings()
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0)
        resp.raise_for_status()
        models = {m["name"] for m in resp.json().get("models", [])}
        # Also add base names without the tag (e.g. "qwen2.5-coder" matches "qwen2.5-coder:7b")
        base_names = {m.split(":")[0] for m in models}
        return frozenset(models | base_names)
    except Exception as exc:
        logger.debug("Could not fetch Ollama model list (non-fatal): %s", exc)
        return frozenset()


def _model_available(model_name: str) -> bool:
    """True if `model_name` (or its base name) is pulled in the local Ollama instance."""
    if not model_name:
        return False
    available = _available_ollama_models()
    return model_name in available or model_name.split(":")[0] in available


# ── LLM Router ────────────────────────────────────────────────────────────────

def _route_file(file_info: dict, score: int) -> str:
    """
    Return the model name to use for this file's review.

    Three tiers based on _triage_score():

      Tier 0  score < 0    lock / generated files → "__skip__" (static only)
      Tier 1  score 0–5    config, yaml, small utils
                           → fast cheap model (ollama_fast_model) IF it is actually
                             pulled in the local Ollama instance.
                           → falls back to "__full__" if the fast model is not
                             configured or not available (avoids silent LLM errors
                             when e.g. qwen2.5-coder:7b is set but not pulled).
      Tier 2  score 6+     service files, agents, API routes, auth
                           → full reasoning model (configured provider)
                           → "__full__" → get_review_llm uses configured provider

    Returns:
      "__skip__"  → Tier 0 sentinel — caller must NOT make an LLM call
      "__full__"  → use the configured provider as-is (Tier 1 fallback or Tier 2)
      "<name>"    → explicit Ollama model name to pass to get_review_llm()
    """
    settings = get_settings()
    if score < 0:
        return "__skip__"  # Tier 0: static only

    if score <= 5:
        # Tier 1: use the fast cheap model only if it's actually available.
        # If ollama_fast_model is set to a model that hasn't been pulled,
        # the LLM call will throw and fall back to _static_triage — negating
        # the point of having a Tier 1 at all. Validate first.
        fast_model = getattr(settings, "ollama_fast_model", "") or ""
        if fast_model and _model_available(fast_model):
            return fast_model
        # Fast model not configured or not pulled — use the full provider
        return "__full__"

    # Tier 2: full reasoning model
    return "__full__"


# ── Phase 1: deterministic static triage ──────────────────────────────────────

def _static_triage(file_info: dict) -> str:
    """
    Full deterministic review — not a placeholder.

    Runs the same structural + security analysis as the LLM tools, formats the
    results as a structured review with sections and a 1–10 score.
    """
    content    = file_info.get("content", "")
    file_name  = file_info.get("file_name", "")
    language   = file_info.get("language", "")
    lines      = content.splitlines()
    total_lines = len(lines)

    def _is_comment(line: str) -> bool:
        return line.strip().startswith(("#", "//", "*", "/*", "<!--"))

    def _is_placeholder(value: str) -> bool:
        return bool(re.search(
            r"(?i)(placeholder|dummy|example|sample|your[-_ ]|ci-placeholder|sk-ci"
            r"|os\.getenv|getenv|settings\.|import\.meta\.env)",
            value,
        ))

    def _hits(pattern: str) -> list[tuple[int, str]]:
        compiled = re.compile(pattern, re.IGNORECASE)
        return [
            (i, line.rstrip())
            for i, line in enumerate(lines, 1)
            if not _is_comment(line) and compiled.search(line)
        ]

    # ── Structure ──────────────────────────────────────────────────────────────
    DEF_PATS = [
        r"^\s*(async\s+def|def|class)\s+\w",
        r"^\s*(export\s+)?(async\s+)?function\s+\w",
        r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?\(",
        r"^\s*(export\s+)?(default\s+)?class\s+\w",
        r"^\s*func\s+(\(\w+\s+\*?\w+\)\s+)?\w+\s*\(",
        r"^\s*(public|private|protected|static|override|abstract).*\s+\w+\s*\(",
        r"^\s*(pub(\(.*\))?\s+)?(async\s+)?fn\s+\w",
    ]
    def_compiled = [re.compile(p) for p in DEF_PATS]
    definitions  = [
        f"  L{i}: {line.strip()[:80]}"
        for i, line in enumerate(lines, 1)
        if any(p.search(line) for p in def_compiled)
    ]

    # ── Complexity ─────────────────────────────────────────────────────────────
    bare_excepts  = sum(1 for l in lines if l.strip() == "except:")
    long_lines    = sum(1 for l in lines if len(l) > 120)
    todos         = sum(1 for l in lines if re.search(r"\b(TODO|FIXME|HACK)\b", l, re.I))
    magic_numbers = sum(1 for l in lines if re.search(r"(?<![=\w])\b[0-9]{2,}\b(?!\s*[=\w])", l.strip()))
    nested_loops  = 0
    loop_stack: list[int] = []
    for line in lines:
        if not line.strip():
            continue
        indent  = len(line) - len(line.lstrip())
        stripped = line.strip()
        while loop_stack and indent <= loop_stack[-1]:
            loop_stack.pop()
        if re.match(r"^(for|while)\b", stripped):
            if loop_stack:
                nested_loops += 1
            loop_stack.append(indent)

    # ── Security ───────────────────────────────────────────────────────────────
    security: list[str] = []
    secret_hits = [
        (i, line) for i, line in _hits(
            r"(?i)\b(api[_-]?key|password|secret|token)\b\s*[:=]\s*['\"]?([^'\"\s,}]+)"
        )
        if (m := re.search(
            r"(?i)\b(api[_-]?key|password|secret|token)\b\s*[:=]\s*['\"]?([^'\"\s,}]+)", line
        )) and not _is_placeholder(m.group(2))
    ]
    if secret_hits:
        lnos = ", ".join(str(i) for i, _ in secret_hits[:6])
        security.append(f"🔑 **Hardcoded credential** at line(s) {lnos} — move to env vars.")
    shell_hits = _hits(
        r"(?i)\b(os\.system|child_process\.exec)\s*\(|subprocess\.(run|call|popen)\s*\([^)]*shell\s*=\s*True"
    )
    if shell_hits:
        lnos = ", ".join(str(i) for i, _ in shell_hits[:6])
        security.append(f"💉 **Shell injection risk** at line(s) {lnos} — use shell=False + arg list.")
    sql_hits = _hits(
        r"(?i)(\b(execute|executemany)\s*\(\s*f?['\"].*?\b(select|insert|update|delete)\b"
        r"|(f['\"]|['\"])[^'\"]*\b(select|insert|update|delete)\b[^'\"]*['\"]\s*([\+%]|\.format\s*\())"
    )
    if sql_hits:
        lnos = ", ".join(str(i) for i, _ in sql_hits[:6])
        security.append(f"🗄️ **Dynamic SQL** at line(s) {lnos} — use parameterised queries.")
    eval_hits = _hits(r"\beval\s*\(|\bexec\s*\(")
    if eval_hits:
        lnos = ", ".join(str(i) for i, _ in eval_hits[:6])
        security.append(f"⚠️ **eval()/exec()** at line(s) {lnos} — code injection risk.")
    xss_hits = _hits(r"dangerouslySetInnerHTML")
    if xss_hits:
        lnos = ", ".join(str(i) for i, _ in xss_hits[:4])
        security.append(f"🌐 **dangerouslySetInnerHTML** at line(s) {lnos} — sanitise first.")

    # ── Quality ────────────────────────────────────────────────────────────────
    quality: list[str] = []
    if bare_excepts:
        quality.append(f"🪤 **Bare `except:`** ({bare_excepts}×) — catch specific types.")
    if nested_loops:
        quality.append(f"🔄 **Nested loops** ({nested_loops}×) — extract inner logic.")
    if long_lines:
        quality.append(f"📏 **Lines >120 chars** ({long_lines}) — wrap or extract variables.")
    if todos:
        quality.append(f"📌 **TODO/FIXME/HACK** ({todos}) — track in issue tracker.")
    if magic_numbers:
        quality.append(f"🔢 **Magic numbers** (~{magic_numbers}) — use named constants.")
    async_hits = _hits(r"\bawait\b")
    try_hits   = _hits(r"^\s*try\s*[:{]")
    if len(async_hits) > 3 and not try_hits:
        quality.append("🛡️ **No try/except around async calls** — can crash the caller.")

    # ── Score ──────────────────────────────────────────────────────────────────
    penalty   = len(security) * 2 + len(quality)
    raw_score = max(1, min(10, 10 - penalty))
    score_note = (
        "(small file — limited signal)" if total_lines < 30
        else "(large file — complexity risk higher)" if total_lines > 600
        else ""
    )

    # ── Assemble ───────────────────────────────────────────────────────────────
    def_block = (
        "\n".join(definitions[:20])
        + (f"\n  … and {len(definitions) - 20} more" if len(definitions) > 20 else "")
        if definitions else "  No definitions found."
    )
    sec_block = (
        "\n".join(f"- {s}" for s in security)
        if security else "- ✅ No high-signal security patterns."
    )
    qual_block = (
        "\n".join(f"- {q}" for q in quality)
        if quality else "- ✅ No significant quality issues."
    )

    return (
        f"## 📁 File Overview\n"
        f"`{file_name}` · {language.upper() or 'unknown'} · {total_lines} lines\n\n"
        f"### Structure\n{def_block}\n\n"
        f"### Complexity\n"
        f"  {total_lines} lines | {len(definitions)} definitions | "
        f"nested loops: {nested_loops} | bare excepts: {bare_excepts} | "
        f"long lines: {long_lines} | TODOs: {todos}\n\n"
        f"## 🔒 Security\n{sec_block}\n\n"
        f"## ⚠️ Code Quality\n{qual_block}\n\n"
        f"## 📊 Score\n**{raw_score}/10** {score_note}\n\n"
        f"> ℹ️ Static analysis (outside LLM review budget for this batch). "
        f"Increase `REVIEW_MAX_FULL_FILES` in your `.env` to include this file in LLM review."
    )


# ── Phase 3: deterministic fallback summary ───────────────────────────────────

def _deterministic_repo_summary(
    files: list[dict],
    per_file_summaries: list[str],
    reason: str | None = None,
    llm_succeeded_indexes: set[int] | None = None,
) -> str:
    """Rich deterministic summary — used when the LLM provider is unavailable."""
    import re as _re

    total = len(files)  # total number of files reviewed in this batch

    languages: dict[str, int] = {}
    risk_hits          = 0
    scored_files: list[tuple[str, int]] = []

    for i, (file_info, summary) in enumerate(zip(files, per_file_summaries)):
        lang = (file_info.get("language") or "unknown").lower()
        languages[lang] = languages.get(lang, 0) + 1
        text = summary.lower()
        if any(t in text for t in ("hardcoded", "shell injection", "sql", "eval()", "xss", "error")):
            risk_hits += 1
        m = _re.search(r"\*\*(\d+)/10\*\*", summary)
        if m:
            scored_files.append((file_info.get("file_name", ""), int(m.group(1))))

    # Use explicit routing counts rather than scanning summary text,
    # which is unreliable when fallback text also contains "static analysis".
    llm_reviewed = len(llm_succeeded_indexes) if llm_succeeded_indexes is not None else 0
    deterministic_only = total - llm_reviewed

    llm_pct = round((llm_reviewed / total) * 100) if total else 0
    lang_text        = ", ".join(f"{l}: {c}" for l, c in sorted(languages.items()))
    scored_files.sort(key=lambda x: x[1])
    priority_files   = [f"`{n}` ({s}/10)" for n, s in scored_files[:5] if s < 8]

    provider_note = ""
    if reason:
        if "402" in reason or "insufficient balance" in reason.lower():
            provider_note = (
                "\n\n> ⚠️ **LLM unavailable** — Insufficient Balance (402). "
                "Recharge your DeepSeek key or run `ollama pull deepseek-r1:14b` for local reviews."
            )
        elif "connection" in reason.lower() or "timeout" in reason.lower():
            provider_note = "\n\n> ⚠️ **LLM unreachable** — check Ollama is running (`ollama serve`)."
        else:
            provider_note = f"\n\n> ⚠️ **LLM error** — {reason[:120]}"

    coverage_note = (
        "\n\n> ℹ️ **Coverage: deterministic only** — all files analysed with static analysis; "
        "no LLM provider was available for semantic review."
        if deterministic_only == total
        else (
            f"\n\n> ℹ️ **Coverage: {llm_pct}% LLM + {deterministic_only} static** — "
            f"{llm_reviewed} LLM reviews; {deterministic_only} static analysis."
        ) if deterministic_only > 0
        else ""
    )

    priority_block = (
        "- Priority files (lowest scores):\n  " + "\n  ".join(priority_files)
        if priority_files
        else "- No files scored below 8/10."
    )

    return (
        "## 🏥 Overall Repo Health\n\n"
        f"**Files:** {total} | **LLM:** {llm_reviewed} | **Static:** {deterministic_only}\n\n"
        f"**Languages:** {lang_text or 'unknown'}\n\n"
        "## 🔁 Cross-file Patterns\n\n"
        f"- Security risk markers in **{risk_hits}** file(s).\n"
        "- Lock files and generated files were deprioritised.\n\n"
        "## 🎯 Top Priority Files\n\n"
        f"{priority_block}\n\n"
        "## ✅ Strengths\n\n"
        "- Full repository coverage: every file has a structural + security review.\n"
        "- Cross-file dependency context was injected into every LLM review."
        f"{coverage_note}"
        f"{provider_note}"
    )


# ── Main entry point ───────────────────────────────────────────────────────────

async def stream_multi_review(
    files: list[dict],
) -> AsyncGenerator[str, None]:
    """
    Stream a cross-file-aware review of multiple files.

    Phase 0: Build repo context (deterministic, instant) for all files.
    Phase 1: Run static triage for all non-LLM files in parallel.
    Phase 2: Run LLM reviews concurrently (bounded semaphore) with repo context.
    Phase 3: Stream per-file results as they complete, then the repo summary.
    """
    settings     = get_settings()
    n            = len(files)
    review_mode  = getattr(settings, "review_mode", "fast")
    review_fn    = stream_fast_code_review if review_mode == "fast" else stream_code_review
    concurrency  = max(1, settings.review_concurrency)
    semaphore    = asyncio.Semaphore(concurrency)
    per_summaries: list[str] = [""] * n
    # Track which indexes produced a real LLM review (not a static fallback).
    # Used by _deterministic_repo_summary for accurate coverage counts.
    llm_succeeded: set[int] = set()

    # ── Phase 0: build repo context ───────────────────────────────────────────
    repo_context_map = _build_repo_context(files)

    # ── LLM Router: assign each file to a tier ────────────────────────────────
    # Route results: idx → route string
    #   "__skip__"  → Tier 0 (lock/generated) — static only, never LLM
    #   "__full__"  → Tier 2 (agents, auth, API) — full reasoning model
    #   "<name>"    → Tier 1 (config, yaml, utils) — fast cheap Ollama model
    #
    # The LLM budget (review_max_full_files) still applies: only the top-N
    # scored files get any LLM call. Files beyond the budget fall back to static.
    scores  = {idx: _triage_score(f) for idx, f in enumerate(files)}
    ranked  = sorted(scores.items(), key=lambda kv: (kv[1], len(files[kv[0]].get("content", ""))), reverse=True)
    llm_limit = settings.review_max_full_files

    # Build route map: files inside budget get a route, outside budget are static.
    llm_route: dict[int, str] = {}
    for idx, score in ranked[:llm_limit]:
        route = _route_file(files[idx], score)
        if route != "__skip__":
            llm_route[idx] = route  # "__full__" or "<model_name>"

    # ── Yield phase-0 status ──────────────────────────────────────────────────
    tier_counts = {0: 0, 1: 0, 2: 0}
    for idx, score in scores.items():
        if idx not in llm_route:
            tier_counts[0] += 1
        elif llm_route[idx] == "__full__":
            tier_counts[2] += 1
        else:
            tier_counts[1] += 1
    ctx_meta = json.dumps({"step": "context_built", "total": n,
                            "tier0": tier_counts[0], "tier1": tier_counts[1], "tier2": tier_counts[2]})
    yield (
        f"__STATUS__Built cross-file context for {n} files "
        f"(T0 static:{tier_counts[0]} T1 fast:{tier_counts[1]} T2 full:{tier_counts[2]})"
        f"...{ctx_meta}__STATUS_END__\n"
    )

    # ── Phase 1: parallel static triage for non-LLM files ─────────────────────
    # Run all static triages simultaneously — pure CPU, no LLM, no rate limit.
    static_indexes = [idx for idx in range(n) if idx not in llm_route]

    async def run_static(idx: int) -> tuple[int, str]:
        result = await asyncio.to_thread(_static_triage, files[idx])
        return idx, result

    static_tasks = [asyncio.create_task(run_static(idx)) for idx in static_indexes]

    # ── Phase 2: concurrent LLM reviews ───────────────────────────────────────
    # Each review gets the repo_context for its own file and its routed model.
    # PERF 3: each call is wrapped in asyncio.wait_for(timeout=120) so a hung
    # model cannot hold a semaphore slot indefinitely — it falls back to static.

    async def run_llm_review(idx: int) -> tuple[int, list[str]]:
        async with semaphore:
            tokens: list[str] = []
            route = llm_route.get(idx, "__full__")
            # "__full__" → pass model_override="" so get_review_llm uses configured provider
            model_override = "" if route == "__full__" else route

            async def _collect() -> list[str]:
                collected: list[str] = []
                ctx = repo_context_map.get(files[idx]["file_name"], "")
                async for token in review_fn(
                    files[idx]["file_name"],
                    files[idx]["content"],
                    files[idx].get("language", ""),
                    repo_context=ctx,
                    model_override=model_override,
                ):
                    collected.append(token)
                return collected

            try:
                tokens = await asyncio.wait_for(_collect(), timeout=_LLM_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "LLM review timed out after %ds for file %s — falling back to static",
                    _LLM_TIMEOUT,
                    files[idx].get("file_name", ""),
                )
                tokens.append(
                    f"__ERROR__LLM timed out after {_LLM_TIMEOUT}s__ERROR_END__\n"
                )
            except Exception as exc:
                tokens.append(f"__ERROR__{str(exc)[:200]}__ERROR_END__\n")
            return idx, tokens

    llm_tasks = [
        asyncio.create_task(run_llm_review(idx))
        for idx in sorted(llm_route)
    ]

    # ── Phase 3: stream results as they complete ───────────────────────────────
    # Static results appear immediately; LLM results stream as they arrive.
    # asyncio.as_completed → frontend sees output in completion order, not submit order.
    all_tasks: list[asyncio.Task] = static_tasks + llm_tasks

    for coro in asyncio.as_completed(all_tasks):
        result = await coro
        idx    = result[0]
        file_info = files[idx]
        file_name = file_info["file_name"]
        file_id   = file_info.get("file_path") or file_name

        yield f"__SECTION_START__{_section_payload(file_info)}__SECTION_END__\n"

        if idx in llm_route:
            # LLM result: tuple[int, list[str]]
            _, review_tokens = result
            route      = llm_route[idx]
            tier_label = "fast" if route != "__full__" else "full"
            model_tag  = f" [{route}]" if route != "__full__" else ""
            status_meta = json.dumps({"step": "file", "id": file_id, "file": file_name,
                                       "index": idx + 1, "total": n, "tier": tier_label})
            yield f"__STATUS__Reviewing{model_tag} `{file_name}`...{status_meta}__STATUS_END__\n"
            has_error = any(t.startswith("__ERROR__") for t in review_tokens)
            for token in review_tokens:
                if not token.startswith("__ERROR__"):
                    yield token
            review_text = "".join(
                t for t in review_tokens
                if not t.startswith("__STATUS__") and not t.startswith("__ERROR__")
            ).strip()
            if has_error:
                fallback = _static_triage(file_info)
                yield f"\n\n{fallback}\n\n*LLM unavailable — static analysis shown.*"
                per_summaries[idx] = f"**{file_name}**: {fallback[:400]}"
                complete_meta = json.dumps({"step": "complete", "id": file_id, "file": file_name,
                                             "index": idx + 1, "total": n})
                yield f"__STATUS__Static fallback: `{file_name}`...{complete_meta}__STATUS_END__\n"
            else:
                per_summaries[idx] = (
                    f"**{file_name}**: {review_text[:400]}"
                    + ("..." if len(review_text) > 400 else "")
                )
                llm_succeeded.add(idx)  # real LLM review completed
                complete_meta = json.dumps({"step": "complete", "id": file_id, "file": file_name,
                                             "index": idx + 1, "total": n, "tier": tier_label})
                yield f"__STATUS__Review done: `{file_name}`...{complete_meta}__STATUS_END__\n"
        else:
            # Static result: tuple[int, str]
            _, triage_text = result
            status_meta = json.dumps({"step": "file", "id": file_id, "file": file_name,
                                       "index": idx + 1, "total": n, "tier": "static"})
            yield f"__STATUS__Scanned `{file_name}`...{status_meta}__STATUS_END__\n"
            yield triage_text
            per_summaries[idx] = f"**{file_name}**: {triage_text[:400]}"
            complete_meta = json.dumps({"step": "complete", "id": file_id, "file": file_name,
                                         "index": idx + 1, "total": n, "tier": "static"})
            yield f"__STATUS__Static done: `{file_name}`...{complete_meta}__STATUS_END__\n"

    # ── Repo summary (Idea 6: Mixture-of-Agents) ──────────────────────────────
    if n > 1:
        summary_info = {"file_path": "__repo_summary__", "file_name": "📊 Overall Repo Summary"}
        yield f"__SECTION_START__{_section_payload(summary_info)}__SECTION_END__\n"

        # Emit a structured coverage token so the frontend can display accurate
        # LLM vs static counts independently of text scanning.
        llm_count  = len(llm_succeeded)
        static_count = n - llm_count
        coverage_meta = json.dumps({
            "step":    "coverage",
            "id":      "__repo_summary__",
            "total":   n,
            "llm":     llm_count,
            "static":  static_count,
            "pct":     round((llm_count / n) * 100) if n else 0,
        })
        yield f"__STATUS__Coverage: {llm_count}/{n} LLM reviews...{coverage_meta}__STATUS_END__\n"

        summary_meta = json.dumps({"step": "summary", "id": "__repo_summary__",
                                    "file": "📊 Overall Repo Summary"})

        # Parse mixture model list from config (comma-separated)
        mixture_models: list[str] = [
            m.strip() for m in (settings.summary_mixture_models or "").split(",")
            if m.strip()
        ]

        # Build coverage context to inject into the LLM summary prompt so the model
        # knows how many files had a full review vs. deterministic-only.
        coverage_context = (
            f"\n\n**Review coverage:** {llm_count}/{n} files had a full LLM review; "
            f"{static_count} used deterministic static analysis only."
        ) if static_count > 0 else ""

        try:
            summaries_text = "\n\n".join(s for s in per_summaries if s)
            summary_prompt = (
                f"Per-file findings:\n\n{summaries_text}{coverage_context}\n\n"
                "Write **Overall Repo Health Assessment** with:\n"
                "- 🏥 Health score (1–10) with reasoning\n"
                "- 🔁 Cross-file patterns (repeated issues, shared dependencies)\n"
                "- 🎯 Top 3 fixes ordered by impact\n"
                "- ✅ Strengths\n"
                "- 📊 Coverage note (how many files had full LLM review vs static analysis)"
            )
            summary_system = (
                "You are CodeSage. You have reviewed multiple files in a repository "
                "with full cross-file dependency context. Write a concise overall "
                "health assessment based on the per-file findings. "
                "If some files only had static analysis, note this honestly in the Coverage section."
            )

            if len(mixture_models) >= 2:
                # ── MoA path: fan out to N cheap models, aggregate ─────────────
                yield f"__STATUS__MoA summary ({len(mixture_models)} models)...{summary_meta}__STATUS_END__\n"

                from app.services.llm_factory import get_review_llm

                async def _draft(model_name: str) -> str:
                    """Run one non-streaming draft call on a specific model."""
                    llm = get_review_llm(model_name, streaming=False)
                    msgs = [
                        SystemMessage(content=summary_system),
                        HumanMessage(content=summary_prompt),
                    ]
                    try:
                        resp = await llm.ainvoke(msgs)
                        return resp.content or ""
                    except Exception:
                        return ""

                # All draft calls run in parallel
                drafts: list[str] = list(await asyncio.gather(
                    *(_draft(m) for m in mixture_models)
                ))
                valid_drafts = [d for d in drafts if d.strip()]

                if valid_drafts:
                    # Aggregator: synthesise drafts into one final streaming answer
                    aggregator_llm = get_chat_llm(streaming=True, review=True)
                    agg_content = "\n\n---\n\n".join(
                        f"[Draft {i+1} from {mixture_models[i]}]\n{d}"
                        for i, d in enumerate(valid_drafts)
                    )
                    agg_msgs = [
                        SystemMessage(
                            content=(
                                "You are CodeSage aggregating multiple draft repo health assessments. "
                                "Synthesise them into a single, best-of-all final assessment. "
                                "Resolve contradictions by taking the more conservative/security-conscious view. "
                                "Do not say 'draft 1 said' — just write the final answer directly."
                            )
                        ),
                        HumanMessage(content=agg_content),
                    ]
                    async for chunk in aggregator_llm.astream(agg_msgs):
                        if chunk.content:
                            yield chunk.content
                else:
                    yield _deterministic_repo_summary(
                        files, per_summaries,
                        "All MoA models returned empty drafts.",
                        llm_succeeded_indexes=llm_succeeded,
                    )

            else:
                # ── Single-model path (default — no config change needed) ───────
                yield f"__STATUS__Generating repo summary...{summary_meta}__STATUS_END__\n"
                llm = get_chat_llm(streaming=True, review=True)
                messages = [
                    SystemMessage(content=summary_system),
                    HumanMessage(content=summary_prompt),
                ]
                async for chunk in llm.astream(messages):
                    if chunk.content:
                        yield chunk.content

        except Exception as e:
            yield _deterministic_repo_summary(
                files, per_summaries, str(e)[:200],
                llm_succeeded_indexes=llm_succeeded,
            )
        finally:
            done_meta = json.dumps({"step": "summary_complete", "id": "__repo_summary__",
                                     "file": "📊 Overall Repo Summary"})
            yield f"\n__STATUS__Repo summary ready...{done_meta}__STATUS_END__\n"
