"""
multi_review_agent.py — Orchestrates cross-file-aware code review across multiple files.

ARCHITECTURE (v3 — planned, batched, cached):

  Phase -1 — Plan  (instant, CPU-only, deterministic)
    `review_planner.plan_review` decides each file's dispatch and records why:
      • static — the parser already owns this file (data, generated, oversized,
        trivial). No model call: the deterministic report is the complete answer.
      • batch  — reviewable but unremarkable. Several such files share ONE call.
      • single — security shape, proven findings, or real complexity. Its own
        call, because that is where review quality is visible.
    This is the change that took a 67-file review from ~60s to low seconds: at
    4.5ms/file the analyzer had already proven most findings, so the model was
    being asked to re-describe finished work.

  Phase 0 — Repo context build  (instant, deterministic, no LLM)
    For every file in the batch, extract:
      • exported symbols (functions, classes, types)
      • import statements (which other files it depends on)
    Produces a compact per-file "repo context" block injected into each LLM prompt.
    This lets the LLM say "reviewPanel.tsx imports useMultiReview, which has this
    issue in multi_review_agent.py" without reviewing all files with the LLM.

  Phase 1 — Deterministic results  (instant, CPU-only)
    Every static file gets a full deterministic review (structure, security
    patterns, complexity, score) — NOT a placeholder. Results are cached against
    the file's content hash, so an unchanged file is answered from disk in
    microseconds instead of being re-analysed and re-explained.

  Phase 2 — Concurrent model calls  (bounded by semaphore)
    Singles get their own call with repo_context injected; batches share one call
    and are split back into per-file sections. Both are cached by content hash.
    Cache hits are labelled with the digest and date they were reviewed.

  Phase 3 — Repo summary  (one final LLM call)
    Synthesises all per-file findings into an overall health assessment.
    Falls back to a rich deterministic summary if the LLM is unavailable.

WIRE FORMAT — defined in `app.services.stream_protocol`, not here:
  __SECTION_START__{"id": …, "file_name": …}__SECTION_END__  → open a card
  __STATUS__{"step": …, "message": …}__STATUS_END__          → progress + timings
  plain text                                                  → review content

Every marker this generator emits is built by `status_event()`, which is also what
the code agent, the writer and the chat retrieval path use. That is deliberate:
one encoder means the client needs one decoder, and a field added for one surface
(a tool duration, a plan step id) is available to all of them. The stage timings
below (`plan_ms`, `context_ms`, per-file `llm_ms`/`wait_ms`, `elapsed_ms`) are the
measurement the latency work has to start from — until now the only number a
review reported was "it finished".
"""

import asyncio
import logging
import re
from typing import AsyncGenerator, Awaitable, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.llm_factory import get_chat_llm
from app.services.review_agent import (
    split_batch_review,
    stream_batch_code_review,
    stream_code_review,
    stream_fast_code_review,
)
from app.services import review_cache, review_planner
from app.services.review_planner import (
    DEFAULT_LLM_BUDGET,
    ROUTE_FULL,
    plan_review,
)
from app.services.stream_protocol import is_protocol_token, section_event, status_event
from app.services.code_analysis import analyze_file
from app.services.code_analysis.analyzer import render_findings_markdown
from app.services.code_analysis.models import FileAnalysis
from app.core.config import get_settings

logger = logging.getLogger(__name__)

FILE_SECTION_MARKER    = "---FILE_SECTION---"
SUMMARY_SECTION_MARKER = "---REPO_SUMMARY---"

# Per-file LLM review timeout.
# Enough headroom for deepseek-coder:33b on a CPU-only machine.
# On timeout, the file falls back to _static_triage rather than blocking the semaphore.
_LLM_TIMEOUT = 120  # seconds

# A batch asks one question about several files, so it earns more headroom than a
# single file — but not unlimited, or a stalled model holds the batch's files
# hostage. On timeout every file in the batch falls back to its static report.
_BATCH_TIMEOUT = 180  # seconds


# ── Section payload helper ─────────────────────────────────────────────────────

def _section_payload(file_info: dict) -> str:
    """
    The marker that opens one file's section, framed by the shared protocol.

    `id` stays the caller's `file_path` when there is one, because the UI keys its
    section cards on it; a section that re-keys mid-stream duplicates a card.
    """
    return section_event(
        id=file_info.get("file_path") or file_info["file_name"],
        file_name=file_info["file_name"],
    )


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
# TTL-based cache: re-query Ollama at most once every 5 minutes.
# lru_cache(forever) meant a newly-pulled model never appeared until restart.
# 5 minutes is short enough that `ollama pull qwen2.5-coder:7b` takes effect
# in the next batch review without hammering the Ollama API on every file.

import time as _time

_ollama_model_cache: tuple[frozenset[str], float] | None = None
_OLLAMA_MODEL_TTL = 300  # seconds


def _available_ollama_models() -> frozenset[str]:
    """
    Return the set of model names currently available in the local Ollama instance.

    Results are cached for _OLLAMA_MODEL_TTL seconds (default 5 min).
    Returns an empty frozenset if Ollama is unreachable (safe fallback —
    callers treat an absent model as "use the full provider instead").

    WHY TTL INSTEAD OF lru_cache?
    lru_cache lives forever — a model pulled mid-session is invisible until
    the process restarts. A 5-minute TTL makes newly-pulled models available
    in the next batch review without any extra Ollama API pressure.
    """
    global _ollama_model_cache
    now = _time.monotonic()
    if _ollama_model_cache is not None and now - _ollama_model_cache[1] < _OLLAMA_MODEL_TTL:
        return _ollama_model_cache[0]

    try:
        import httpx
        settings = get_settings()
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=3.0)
        resp.raise_for_status()
        models = {m["name"] for m in resp.json().get("models", [])}
        # Also add base names without the tag (e.g. "qwen2.5-coder" matches "qwen2.5-coder:7b")
        base_names = {m.split(":")[0] for m in models}
        result = frozenset(models | base_names)
    except Exception as exc:
        logger.debug("Could not fetch Ollama model list (non-fatal): %s", exc)
        result = frozenset()

    _ollama_model_cache = (result, now)
    return result


def _model_available(model_name: str) -> bool:
    """True if `model_name` (or its base name) is pulled in the local Ollama instance."""
    if not model_name:
        return False
    available = _available_ollama_models()
    return model_name in available or model_name.split(":")[0] in available


# ── Model routing ─────────────────────────────────────────────────────────────
#
# `review_planner.plan_review` owns the per-file decision (static / batch /
# single). What remains here is the one thing it should not know: which model
# names actually exist on this machine. A fast local model is used for batched and
# low-score files only when it is genuinely pulled — routing to a model that is
# not there makes the LLM call throw, and the file silently degrades to static,
# which is worse than never having routed it in the first place.


class _ProviderCircuit:
    """
    Stop asking a provider that is demonstrably not answering.

    A review of 60 files makes up to 26 model calls. When Ollama is not running —
    or a paid key has expired — every one of those calls pays a full connection
    timeout before falling back to static analysis, and the user watches a review
    take 30 seconds to produce results the analyzer had ready in 300ms.

    So failures are counted, and after a few in a row the pipeline stops calling
    the provider for a cooldown window, then allows a single probe to see whether
    it came back. Every file still gets its deterministic review; only the
    pointless waiting is removed.

    Not thread-safe by design: the review pipeline runs on one event loop, and
    adding a lock here would buy nothing but the appearance of rigour.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at = 0.0
        self._probe_allowed = False
        self._last_reason = ""

    @property
    def is_open(self) -> bool:
        """True when the provider is known-bad and calls should be skipped."""
        if self._failures < self.failure_threshold:
            return False
        if _time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return False  # cooldown elapsed — let a probe through
        return True

    def allows_call(self) -> bool:
        """
        Whether this call may be attempted.

        Below the failure threshold: always. Within a cooldown: never. Past a
        cooldown: exactly once, because a recovered provider should be found again
        without every queued file discovering it simultaneously — a hundred
        probes at once is the thundering herd the circuit exists to prevent.
        """
        if self._failures < self.failure_threshold:
            return True
        if _time.monotonic() - self._opened_at >= self.cooldown_seconds and not self._probe_allowed:
            self._probe_allowed = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = 0.0
        self._probe_allowed = False
        self._last_reason = ""

    def record_failure(self, reason: str = "") -> None:
        """
        Count a failed call.

        `reason` must be a short category ("review timed out"), never the
        provider's own message: this string is reported to the user in the
        coverage token, and a review that quotes a billing error reads as broken
        software. The raw exception is logged by the caller instead.
        """
        self._failures += 1
        self._last_reason = reason or self._last_reason
        if self._failures >= self.failure_threshold:
            # Refresh the window on every failure so a failed probe waits a full
            # cooldown before the next one.
            self._opened_at = _time.monotonic()
            self._probe_allowed = False

    def snapshot(self) -> dict:
        return {
            "open": self.is_open,
            "consecutive_failures": self._failures,
            "last_reason": self._last_reason,
        }


#: Process-wide circuit shared by every review. One user's broken provider is the
#: same provider for the next request, so the knowledge is kept across requests.
_PROVIDER_CIRCUIT = _ProviderCircuit()


def _fast_route() -> str:
    """Ollama fast model if it is pulled, else "" (meaning: use the provider)."""
    settings = get_settings()
    fast_model = getattr(settings, "ollama_fast_model", "") or ""
    return fast_model if fast_model and _model_available(fast_model) else ""


# ── Phase 1: deterministic static triage ──────────────────────────────────────

def _static_triage(file_info: dict, analysis: FileAnalysis | None = None) -> str:
    """
    Full deterministic review — not a placeholder.

    Delegates to the AST analyzer in `app.services.code_analysis`, which parses
    the file rather than matching regexes against raw lines. The previous
    implementation reported `execute("... WHERE id = ?", (uid,))` as dynamic SQL
    while missing an f-string injection split across two lines; on a 17-case
    labelled corpus it scored F1 0.67 against the analyzer's 1.00.

    The output contract is unchanged — same section headings, same score line,
    same footer sentinel — because the PR-comment renderer and the review UI
    both parse these sections.
    """
    content   = file_info.get("content", "")
    file_name = file_info.get("file_name", "")
    language  = file_info.get("language", "")

    # The planner parses every file it plans for, and hands the parse over here.
    # Re-analysing would double the CPU cost of a review for no new information.
    if analysis is None:
        analysis = analyze_file(content, file_name, language)

    security_findings = [f for f in analysis.sorted_findings() if f.cwe]
    quality_findings  = [f for f in analysis.sorted_findings() if not f.cwe]

    def _render(findings: list, empty_message: str) -> str:
        if not findings:
            return f"- ✅ {empty_message}"
        scoped = FileAnalysis(
            file_name=analysis.file_name,
            language=analysis.language,
            total_lines=analysis.total_lines,
            findings=findings,
        )
        return render_findings_markdown(scoped, max_findings=10)

    # Structure: the analyzer already located every definition with its real
    # extent, so this no longer depends on per-language regex guesswork.
    if analysis.functions:
        ranked = sorted(analysis.functions, key=lambda f: (-f.complexity, f.line))[:20]
        def_lines = [
            f"  L{fn.line}: {fn.name}  ({fn.length} lines, complexity {fn.complexity})"
            for fn in ranked
        ]
        if len(analysis.functions) > 20:
            def_lines.append(f"  … and {len(analysis.functions) - 20} more")
        def_block = "\n".join(def_lines)
    else:
        def_block = "  No definitions found."

    score = analysis.risk_score()
    score_note = (
        "(small file — limited signal)" if analysis.total_lines < 30
        else "(large file — complexity risk higher)" if analysis.total_lines > 600
        else ""
    )
    parse_note = f"\n\n> ⚠️ Parse failed ({analysis.parse_error}) — fell back to pattern scanning." if analysis.parse_error else ""

    return (
        f"## 📁 File Overview\n"
        f"`{file_name}` · {(analysis.language or 'unknown').upper()} · {analysis.total_lines} lines "
        f"({analysis.code_lines} code, {analysis.comment_lines} comment)\n\n"
        f"### Structure\n{def_block}\n\n"
        f"### Complexity\n"
        f"  {len(analysis.functions)} definitions | max complexity: {analysis.max_complexity} "
        f"| avg: {analysis.avg_complexity:.1f}\n\n"
        f"## 🔒 Security\n{_render(security_findings, 'No security issues found by static analysis.')}\n\n"
        f"## ⚠️ Code Quality\n{_render(quality_findings, 'No significant quality issues.')}\n\n"
        f"## 📊 Score\n**{score}/10** {score_note}{parse_note}\n\n"
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
        if re.search(r"\b402\b", reason) or "insufficient balance" in reason.lower() or "payment required" in reason.lower():
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

#: How often a running repo review asks whether the reader is still there, while the
#: model calls are the thing holding the run open. A poll is a queue lookup; the calls
#: it can cancel are seconds of a local model.
_STOP_POLL_SECONDS = 0.2


async def _reap(tasks: list[asyncio.Task]) -> None:
    """
    Cancel what is still running and wait for it, rather than abandoning it.

    A generator that returns leaves its tasks pending, and asyncio then either runs
    the very model calls the Stop was meant to avoid or logs "Task was destroyed but
    it is pending" — so cancellation has to be awaited to count as done. The
    exceptions are swallowed on purpose: a `CancelledError` from a worker a reader
    stopped is not a failure to report anywhere.
    """
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _cancelled_marker(files: list[dict], streamed: set[int], started_at: float) -> str:
    """The closing marker for a review a reader stopped."""
    missing = [files[idx].get("file_name", "?") for idx in range(len(files)) if idx not in streamed]
    return status_event(
        f"Stopped — {len(missing)} of {len(files)} file(s) were not reviewed",
        step="cancelled",
        elapsed_ms=int((_time.monotonic() - started_at) * 1000),
        published=len(streamed),
        not_reviewed=len(missing),
        files_skipped=missing,
    )


async def stream_multi_review(
    files: list[dict],
    should_stop: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncGenerator[str, None]:
    """
    Stream a planned, batched, cached review of multiple files.

    Phase -1: plan each file's dispatch (static / batch / single) with reasons.
    Phase 0:  build cross-file repo context.
    Phase 1:  run deterministic reviews for static files, in parallel.
    Phase 2:  run model reviews — one call per single, one per batch.
    Phase 3:  stream each file's section as it is ready, then the repo summary.

    Every model result is cached under a key derived from the file content, the
    model and the planner version, so re-reviewing an unchanged file costs a
    dictionary lookup instead of a call. Cache hits are labelled in the output.

    `should_stop` is an async predicate checked before every model call and between
    streamed sections; the route hands it `Request.is_disconnected`, so closing the
    tab ends the run instead of paying for it. The saving is the point — a 60-file
    repo review is minutes of a local model's time, and the browser has already gone.
    Files that were never started are named in a closing `cancelled` marker rather
    than left as blank sections, which is what separates "stopped here" from
    "reviewed nothing and said nothing".
    """
    async def _never_stopped() -> bool:
        return False

    stop = should_stop or _never_stopped
    # Set once the reader is known to be gone. The workers check this flag as well
    # as the predicate: after a Stop, `is_disconnected` stays true, but a task that
    # is already past its check must not start a *second* model call, and a cached
    # lookup should not be paid for either.
    abandoned = False
    #: Indexes whose section has been yielded — the "what did you actually review"
    #: set the closing marker counts against the plan.
    streamed: set[int] = set()

    async def stopping() -> bool:
        return abandoned or await stop()

    #: Set by the watcher as well as the driver, so a Stop noticed mid-model-call can
    #: reach the files still waiting for a semaphore slot.
    running = True

    settings     = get_settings()
    n            = len(files)
    review_mode  = getattr(settings, "review_mode", "fast")
    review_fn    = stream_fast_code_review if review_mode == "fast" else stream_code_review
    concurrency  = max(1, int(getattr(settings, "review_concurrency", 3)))
    semaphore    = asyncio.Semaphore(concurrency)
    per_summaries: list[str] = [""] * n
    #: Indexes that received a model review this run — including cache hits,
    #: because they were reviewed; the run simply did not have to pay for it twice.
    llm_succeeded: set[int] = set()
    cache_hits: set[int] = set()
    #: Files that should have had a model review but did not get one (provider
    #: error, timeout, or a batch that omitted them). Counted separately from the
    #: files the planner deliberately left to static analysis, because "the parser
    #: covered this" and "the model failed on this" are different statements.
    fallback_only: set[int] = set()
    started_at = _time.monotonic()

    cache_enabled = bool(getattr(settings, "review_cache_enabled", True))
    provider      = getattr(settings, "llm_provider", "") or ""
    cache_mode    = "fast" if review_mode == "fast" else "agentic"

    # ── Phase -1: score, then plan ────────────────────────────────────────────
    scores = {idx: _triage_score(f) for idx, f in enumerate(files)}
    planned_at = _time.monotonic()
    plan = plan_review(
        files,
        scores=scores,
        llm_budget=int(getattr(settings, "review_llm_budget", DEFAULT_LLM_BUDGET)
                       or getattr(settings, "review_max_full_files", DEFAULT_LLM_BUDGET)),
        model_route=ROUTE_FULL,
        fast_route=_fast_route(),
    )

    plan_ms = int((_time.monotonic() - planned_at) * 1000)

    # ── Phase 0: build repo context ───────────────────────────────────────────
    context_at = _time.monotonic()
    repo_context_map = _build_repo_context(files)
    context_ms = int((_time.monotonic() - context_at) * 1000)

    plan_stats = plan.stats(n)
    yield status_event(
        f"Planned {n} files: {plan_stats['single_reviews']} full review(s), "
        f"{plan_stats['batched_files']} batched into {plan_stats['batch_count']} call(s), "
        f"{plan_stats['static_only']} static-only "
        f"({plan_stats['model_calls']} model call(s) instead of {n})",
        step="planned", total=n, **plan_stats,
        # Where the run's time went before any model was called. `plan_ms` covers
        # the parse+triage of every file, `context_ms` the cross-file map — the
        # two deterministic phases, which a user experiences as "it is just
        # sitting there" and nobody could previously separate from the model.
        plan_ms=plan_ms, context_ms=context_ms,
    )

    # ── Cache helpers ─────────────────────────────────────────────────────────
    def _file_key(idx: int, kind: str) -> str:
        info = files[idx]
        return review_cache.file_key(
            info.get("content", ""),
            info.get("file_name", ""),
            info.get("language", ""),
            provider=provider if kind != "static" else "static",
            model="" if kind != "static" else "analyzer",
            mode=cache_mode if kind != "static" else "static",
        )

    def _cache_read(idx: int, kind: str) -> str | None:
        if not cache_enabled:
            return None
        entry = review_cache.get(_file_key(idx, kind))
        return entry["value"] if entry else None

    def _cache_write(idx: int, kind: str, value: str) -> None:
        if not cache_enabled or not value.strip():
            return
        review_cache.put(
            _file_key(idx, kind), value, kind=kind,
            meta={"file": files[idx].get("file_name", ""), "tier": kind},
        )

    def _provenance(idx: int, kind: str) -> str:
        """The note that turns "no work happened" into evidence a reader can check."""
        entry = review_cache.get(_file_key(idx, kind)) if cache_enabled else None
        digest = review_cache.hash_content(files[idx].get("content", ""))[:12]
        when = ""
        if entry and entry.get("created"):
            when = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime(entry["created"]))
        return (
            f"\n\n> ♻️ **Cached review** — this file is byte-identical to the copy "
            f"reviewed{f' on {when}' if when else ''} (sha256 `{digest}…`). "
            f"No model call was made for it in this run.\n"
        )

    # Per-file timings, filled by the workers and read by the markers below.
    #
    # A side table rather than a fifth element of the result tuple, because four
    # places build those tuples and none of them cares about timing — widening the
    # tuple would touch every one of them to move one number.
    #
    # `wait_ms` is the time the file spent queued behind the concurrency
    # semaphore, `llm_ms` the model call itself. Separating them is the point: a
    # review where every file waited 900 ms and answered in 200 ms has a
    # concurrency problem, and one where it waited 10 ms and answered in 4 s has a
    # model problem. The two want opposite fixes.
    stage_ms: dict[int, dict] = {}

    # ── Phase 1+2: static work and model work ─────────────────────────────────
    async def run_static(idx: int) -> list[tuple[int, str, bool, str | None]]:
        if await stopping():
            return []
        cached = _cache_read(idx, "static")
        if cached is not None:
            return [(idx, cached + _provenance(idx, "static"), True, None)]
        text = await asyncio.to_thread(_static_triage, files[idx], plan.analyses.get(idx))
        _cache_write(idx, "static", text)
        return [(idx, text, False, None)]

    async def run_single(idx: int) -> list[tuple[int, str, bool, str | None]]:
        if await stopping():
            return []
        cached = _cache_read(idx, "single")
        if cached is not None:
            return [(idx, cached + _provenance(idx, "single"), True, None)]

        if not _PROVIDER_CIRCUIT.allows_call():
            logger.warning(
                "provider circuit open (%s) — skipping the model call for %s",
                _PROVIDER_CIRCUIT._last_reason or "repeated failures",
                files[idx].get("file_name", ""),
            )
            return _static_fallback(idx, "model provider is failing; call skipped")

        queued_at = _time.monotonic()
        async with semaphore:
            # The check belongs here, not only at entry: a file that queued behind
            # the semaphore for two minutes has had two chances to be cancelled, and
            # the call it is about to make is the expensive one.
            if await stopping():
                return []
            waited_ms = int((_time.monotonic() - queued_at) * 1000)
            route = plan.of(idx).route
            model_override = "" if route == ROUTE_FULL else route
            tokens: list[str] = []

            async def _collect() -> None:
                ctx = repo_context_map.get(files[idx]["file_name"], "")
                async for token in review_fn(
                    files[idx]["file_name"],
                    files[idx]["content"],
                    files[idx].get("language", ""),
                    repo_context=ctx,
                    model_override=model_override,
                ):
                    tokens.append(token)

            called_at = _time.monotonic()
            try:
                await asyncio.wait_for(_collect(), timeout=_LLM_TIMEOUT)
                stage_ms[idx] = {
                    "llm_ms": int((_time.monotonic() - called_at) * 1000),
                    "wait_ms": waited_ms,
                }
            except asyncio.TimeoutError:
                logger.warning(
                    "LLM review timed out after %ds for file %s — falling back to static",
                    _LLM_TIMEOUT, files[idx].get("file_name", ""),
                )
                _PROVIDER_CIRCUIT.record_failure("review timed out")
                return _static_fallback(idx, "model call timed out")
            except Exception as exc:  # noqa: BLE001
                # The provider's message goes to the log, not into the user's
                # review: "402 Insufficient Balance" is an operator's problem, and
                # a review that contains billing text reads as broken software.
                logger.warning("LLM review failed for %s: %s", files[idx].get("file_name"), exc)
                _PROVIDER_CIRCUIT.record_failure("provider call failed")
                return _static_fallback(idx, "model call failed")

        error = next((t for t in tokens if t.startswith("__ERROR__")), None)
        if error:
            logger.warning("review stream reported an error for %s: %s",
                           files[idx].get("file_name", ""), error[:200])
            _PROVIDER_CIRCUIT.record_failure("provider reported an error")
            return _static_fallback(idx, "model call failed")

        _PROVIDER_CIRCUIT.record_success()

        review_text = "".join(
            t for t in tokens
            if not is_protocol_token(t)
        ).strip()
        _cache_write(idx, "single", review_text)
        return [(idx, review_text, False, None)]

    def _static_fallback(idx: int, reason: str) -> list[tuple[int, str, bool, str | None]]:
        """A model that failed is a fact to report, not a file to drop."""
        fallback_only.add(idx)
        text = _static_triage(files[idx], plan.analyses.get(idx))
        return [(idx, text, False, reason or "model unavailable")]

    async def run_batch(batch_id: str) -> list[tuple[int, str, bool, str | None]]:
        if await stopping():
            return []
        batch = plan.batches[batch_id]
        members = [files[i] for i in batch.indexes]
        names = [m["file_name"] for m in members]

        key = review_cache.batch_key(
            review_planner.chunk_hashes(members),
            provider=provider, model="", mode=cache_mode,
        )
        entry = review_cache.get(key) if cache_enabled else None
        if entry:
            split = split_batch_review(entry["value"], names)
            if len(split) == len(names):
                return [
                    (idx, split[files[idx]["file_name"]] + _provenance(idx, "single"), True, None)
                    for idx in batch.indexes
                ]

        if not _PROVIDER_CIRCUIT.allows_call():
            return [
                _static_fallback(i, "model provider is failing; call skipped")[0]
                for i in batch.indexes
            ]

        queued_at = _time.monotonic()
        async with semaphore:
            if await stopping():
                return []
            waited_ms = int((_time.monotonic() - queued_at) * 1000)
            tokens: list[str] = []

            async def _collect() -> None:
                async for token in stream_batch_code_review(
                    members, repo_context_map, model_override=plan.of(batch.indexes[0]).route
                ):
                    tokens.append(token)

            started_call = _time.monotonic()
            try:
                await asyncio.wait_for(_collect(), timeout=_BATCH_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("batched review timed out after %ds (%s)", _BATCH_TIMEOUT, names)
                _PROVIDER_CIRCUIT.record_failure("batched review timed out")
                return [_static_fallback(i, "batched review timed out")[0] for i in batch.indexes]
            except Exception as exc:  # noqa: BLE001
                logger.warning("batched review failed (%s): %s", names, exc)
                _PROVIDER_CIRCUIT.record_failure("batch call failed")
                return [_static_fallback(i, "model call failed")[0] for i in batch.indexes]

        _PROVIDER_CIRCUIT.record_success()
        # One call, several files: every member of the batch records the same
        # model duration, which is the measurement that shows batching working.
        for member in batch.indexes:
            stage_ms[member] = {
                "llm_ms": int((_time.monotonic() - started_call) * 1000),
                "wait_ms": waited_ms,
                "batch": batch_id,
            }

        combined = "".join(
            t for t in tokens
            if not is_protocol_token(t)
        ).strip()
        split = split_batch_review(combined, names)
        if combined:
            review_cache.put(key, combined, kind="batch_llm",
                             meta={"files": names, "batch_id": batch_id})

        results: list[tuple[int, str, bool, str | None]] = []
        for idx in batch.indexes:
            name = files[idx]["file_name"]
            body = split.get(name)
            if body:
                # Cache the per-file slice too: the same file may be batched with
                # different neighbours next time, and a batch hit would miss.
                _cache_write(idx, "single", body)
                results.append((idx, body, False, None))
            else:
                # The model answered about its neighbours but not this file. Say
                # so, and give the deterministic report rather than a wrong one.
                results.append(_static_fallback(idx, "no section returned in the batched review")[0])
        return results

    tasks: list[asyncio.Task] = []
    for idx in range(n):
        kind = plan.of(idx).kind
        if kind == "static":
            tasks.append(asyncio.create_task(run_static(idx)))
        elif kind == "single":
            tasks.append(asyncio.create_task(run_single(idx)))
    for batch_id in plan.batches:
        tasks.append(asyncio.create_task(run_batch(batch_id)))

    async def _watch_for_departure() -> None:
        nonlocal abandoned
        while running:
            await asyncio.sleep(_STOP_POLL_SECONDS)
            if not running:
                return
            if await stop():
                # A Stop that arrives while every in-flight review is mid-model-call
                # would otherwise go unnoticed until one of them finishes, which on a
                # 40-second local call is precisely the wait the button exists to end.
                abandoned = True
                await _reap(tasks)
                return

    watch = asyncio.create_task(_watch_for_departure())

    # The reviews are detached tasks, which is what lets six of them share three
    # semaphore slots — and what makes them survive their own generator. When a client
    # hangs up, Starlette may abandon the frame instead of unwinding it, and then no
    # `finally` of mine runs at all; the generator is left suspended and its children
    # keep calling the model. So the consumer of this stream is asked to cancel them
    # when *it* finishes, whatever it was doing at the time. A done callback needs no
    # await, so it fires on a cancelled task, on a closed generator, and during loop
    # teardown alike.
    consumer = asyncio.current_task()

    def _cancel_outstanding(_result=None):
        # One function, reached from both ways this run can end: the consumer task's
        # done-callback (which fires even when the frame below is abandoned) and the
        # generator's own teardown. `Task.cancel()` needs no await, so it lands while a
        # frame is unwinding and after one is gone.
        nonlocal running
        running = False
        if not watch.done():
            watch.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()

    if consumer is not None:
        consumer.add_done_callback(_cancel_outstanding)

    async def _await_result(coro):
        try:
            return await coro
        except asyncio.CancelledError:
            if abandoned:
                # The watcher tore this down on the reader's behalf: an ending, not a
                # failure. Anything else must keep propagating, or a real teardown
                # would be swallowed here.
                return None
            raise

    # ── Phase 3: stream results as they complete ──────────────────────────────
    # Static results appear immediately; model results stream as they arrive.
    # Batches resolve into several file sections from one completed task.
    # The loop and its teardown share a frame, so that the `finally` below also
    # covers a run that is cancelled from underneath rather than one that notices
    # the disconnect between two files.
    try:
        for coro in asyncio.as_completed(tasks):
            results = await _await_result(coro)
            if results is None:
                # The watcher already cancelled the work; there is nothing left to
                # hand the reader, and the `finally` reaps what is still winding down.
                return
            if await stopping():
                abandoned = True
                await _reap(tasks)
                yield _cancelled_marker(files, streamed, started_at)
                return
            for idx, review_text, was_cached, fallback_reason in results:
                file_info = files[idx]
                file_name = file_info["file_name"]
                file_id   = file_info.get("file_path") or file_name
                dispatch  = plan.of(idx)

                yield _section_payload(file_info)
                streamed.add(idx)

                if was_cached:
                    cache_hits.add(idx)
                    # A cache hit is only an LLM review when a model produced it. A
                    # static report served from cache is still static analysis, and
                    # counting it as LLM coverage would overstate how much of the repo
                    # a model actually saw.
                    if dispatch.kind != "static":
                        llm_succeeded.add(idx)
                    tier_label = dispatch.tier
                    yield status_event(
                        f"Cache hit: `{file_name}` (no model call)",
                        step="file", id=file_id, file=file_name,
                        index=idx + 1, total=n, tier=tier_label, cached=True,
                    )
                    yield review_text
                    per_summaries[idx] = f"**{file_name}**: {review_text[:400]}"
                    yield status_event(
                        f"Review done: `{file_name}`",
                        step="complete", id=file_id, file=file_name,
                        index=idx + 1, total=n, tier=tier_label, cached=True,
                    )
                    continue

                if dispatch.kind == "static":
                    yield status_event(
                        f"Scanned `{file_name}`",
                        step="file", id=file_id, file=file_name,
                        index=idx + 1, total=n, tier="static",
                    )
                    yield review_text
                    per_summaries[idx] = f"**{file_name}**: {review_text[:400]}"
                    yield status_event(
                        f"Static done: `{file_name}`",
                        step="complete", id=file_id, file=file_name,
                        index=idx + 1, total=n, tier="static",
                    )
                    continue

                # A model review — single or one file of a batch.
                tier_label = dispatch.tier
                yield status_event(
                    f"Reviewing `{file_name}`",
                    step="file", id=file_id, file=file_name,
                    index=idx + 1, total=n, tier=tier_label,
                    **({"batch": dispatch.batch_id} if dispatch.batch_id else {}),
                )
                yield review_text

                if fallback_reason:
                    # The static report is already in review_text; name the failure
                    # without quoting the provider.
                    yield f"\n\n*Static analysis shown — {fallback_reason}.*"
                    per_summaries[idx] = f"**{file_name}**: {review_text[:400]}"
                else:
                    llm_succeeded.add(idx)
                    per_summaries[idx] = (
                        f"**{file_name}**: {review_text[:400]}"
                        + ("..." if len(review_text) > 400 else "")
                    )
                yield status_event(
                    f"Review done: `{file_name}`",
                    step="complete", id=file_id, file=file_name,
                    index=idx + 1, total=n, tier=tier_label,
                    **stage_ms.get(idx, {}),
                )

    finally:
        # Reached when this generator is unwound rather than abandoned — a consumer that
        # stops reading mid-yield, or a test. The done-callback above covers the case
        # where nobody unwinds the frame at all.
        _cancel_outstanding()

    # ── Run telemetry ─────────────────────────────────────────────────────────
    # The numbers that make the plan auditable: how many calls this run actually
    # made, how many of the planned ones it skipped because content was unchanged,
    # and how long the whole file phase took.
    elapsed_ms = int((_time.monotonic() - started_at) * 1000)

    # The slowest file, by model time. `max` over the *measured* subset: a file
    # answered from cache or by the parser has no `llm_ms` at all, and counting
    # those as zero would report "the slowest model call took 0 ms" on a run where
    # no model ran. Key presence rather than truthiness for the same reason — an
    # instant local answer genuinely measures 0 ms, and a 0 ms sample is still the
    # sample a benchmark should average.
    slowest: tuple[str, dict] | None = None
    _by_llm = {idx: timings for idx, timings in stage_ms.items() if "llm_ms" in timings}
    if _by_llm:
        _worst = max(_by_llm, key=lambda idx: _by_llm[idx]["llm_ms"])
        slowest = (files[_worst]["file_name"], _by_llm[_worst])
    yield status_event(
        f"{plan_stats['model_calls']} model call(s), {len(cache_hits)} cache hit(s) in {elapsed_ms} ms",
        step="timing", stage="files",
        elapsed_ms=elapsed_ms,
        model_calls=plan_stats["model_calls"],
        cache_hits=len(cache_hits),
        static_only=plan_stats["static_only"],
        batched_files=plan_stats["batched_files"],
        batch_count=plan_stats["batch_count"],
        # Slowest file in the run, with the phase that made it slow. One number is
        # a statistic; the split says whether the fix is concurrency or model.
        **({"slowest_file": slowest[0], **slowest[1]} if slowest else {}),
    )

# ── Repo summary (Idea 6: Mixture-of-Agents) ──────────────────────────────
    if n > 1:
        summary_info = {"file_path": "__repo_summary__", "file_name": "📊 Overall Repo Summary"}
        yield _section_payload(summary_info)

        # Emit a structured coverage token so the frontend can display accurate
        # LLM vs static counts independently of text scanning.
        llm_count  = len(llm_succeeded)
        static_count = n - llm_count
        planned_static = len(set(plan.static_indexes) - fallback_only)
        coverage_meta = {
            "step":    "coverage",
            "id":      "__repo_summary__",
            "total":   n,
            "llm":     llm_count,
            "static":  static_count,
            "pct":     round((llm_count / n) * 100) if n else 0,
            # Extra detail for the "why" of a coverage number: a planner decision
            # is not the same as a provider failure, and a cache hit is not a
            # missed review.
            "cache_hits": len(cache_hits),
            "cached_llm": len([i for i in cache_hits if plan.of(i).kind != "static"]),
            "planned_static": planned_static,
            "provider_circuit": _PROVIDER_CIRCUIT.snapshot(),
            "fallback_static": len(fallback_only),
            "model_calls": plan_stats["model_calls"],
        }
        yield status_event(
            f"Coverage: {llm_count}/{n} LLM reviews", **{
                key: value for key, value in coverage_meta.items()},
        )

        summary_meta = {"step": "summary", "id": "__repo_summary__",
                        "file": "📊 Overall Repo Summary"}
        summary_at = _time.monotonic()

        # Parse mixture model list from config (comma-separated)
        mixture_models: list[str] = [
            m.strip() for m in (settings.summary_mixture_models or "").split(",")
            if m.strip()
        ]

        # Build coverage context to inject into the LLM summary prompt so the model
        # knows how many files had a full review vs. deterministic-only.
        if static_count > 0:
            fallback_note = (
                f" Of those, {len(fallback_only)} were meant to be model-reviewed but "
                f"the provider call failed or timed out; the rest are files the parser "
                f"fully determined (the model would only be restating the analyzer)."
                if fallback_only else
                " These are files the parser fully determined — a model review of them "
                "would restate the analyzer rather than add to it."
            )
            cache_note = (
                f" {len(cache_hits)} file(s) were served from the content-hash cache "
                f"(byte-identical to a previous review), so this run made "
                f"{plan_stats['model_calls']} model call(s) for {n} files."
                if cache_hits else ""
            )
            coverage_context = (
                f"\n\n**Review coverage:** {llm_count}/{n} files had a model review; "
                f"{static_count} used deterministic static analysis only.{fallback_note}{cache_note}"
            )
        else:
            coverage_context = (
                f"\n\n**Review coverage:** all {n} files had a model review "
                f"({len(cache_hits)} served from cache)."
            )

        # The summary is another model call, and it is subject to the same
        # circuit: if the provider has been failing all review long, asking it
        # once more only adds a connection timeout to the end of an otherwise
        # instant run. The deterministic summary below is what the LLM path
        # falls back to anyway, so it is used directly and announced honestly.
        summary_uses_model = _PROVIDER_CIRCUIT.allows_call()
        if not summary_uses_model:
            # No model call, and no pretense of one: the deterministic summary is
            # what a failed LLM summary produces anyway, so produce it directly
            # and say that the provider never answered.
            circuit = _PROVIDER_CIRCUIT.snapshot()
            yield status_event(
                "Writing the deterministic summary (provider not answering)",
                step="summary", id="__repo_summary__", file="📊 Overall Repo Summary",
            )
            yield _deterministic_repo_summary(
                files, per_summaries,
                f"provider unavailable ({circuit['last_reason'] or 'no response'})",
                llm_succeeded_indexes=llm_succeeded,
            )
            yield status_event(
                "Repo summary ready",
                step="summary_complete", id="__repo_summary__", file="📊 Overall Repo Summary",
                elapsed_ms=int((_time.monotonic() - summary_at) * 1000), model=False,
            )
            return

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
                "You are SavFlux. You have reviewed multiple files in a repository "
                "with full cross-file dependency context. Write a concise overall "
                "health assessment based on the per-file findings. "
                "If some files only had static analysis, note this honestly in the Coverage section."
            )

            if len(mixture_models) >= 2:
                # ── MoA path: fan out to N cheap models, aggregate ─────────────
                yield status_event(f"MoA summary ({len(mixture_models)} models)", **summary_meta)

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
                                "You are SavFlux aggregating multiple draft repo health assessments. "
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
                yield status_event("Generating repo summary", **summary_meta)
                llm = get_chat_llm(streaming=True, review=True)
                messages = [
                    SystemMessage(content=summary_system),
                    HumanMessage(content=summary_prompt),
                ]
                async for chunk in llm.astream(messages):
                    if chunk.content:
                        yield chunk.content

        except Exception as e:
            _PROVIDER_CIRCUIT.record_failure("summary call failed")
            yield _deterministic_repo_summary(
                files, per_summaries, str(e)[:200],
                llm_succeeded_indexes=llm_succeeded,
            )
        finally:
            yield "\n" + status_event(
                "Repo summary ready",
                step="summary_complete", id="__repo_summary__", file="📊 Overall Repo Summary",
                elapsed_ms=int((_time.monotonic() - summary_at) * 1000),
                model=summary_uses_model,
            )
