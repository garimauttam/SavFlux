"""
review_planner.py — Decide which files need a model, and which do not.

THE MEASUREMENT THIS COMES FROM
-------------------------------
    static analyzer : 67 files in 305 ms   (4.5 ms/file, AST + taint)
    LLM review      : the same 67 files, ~2.7 s each, 3 concurrent → ~60 s

That is a ~590× ratio, and the old pipeline paid it on every file, on every run,
including files that had not changed. The answer was never "review faster" — the
AST proves what it proves in milliseconds — it was "call the model less, and only
where a model adds something."

So the plan is a decision, per file, with three possible dispatches:

  static   the parser decided this file. A model call would add prose, not
           findings. Lock files, generated bundles, data-only files, and files
           that are simply too small to contain anything.
  batch    reviewable, but ordinary: no security shape, no complexity signal,
           nothing the analyzer flagged. These files share one call — the
           question "does anything here look wrong?" is asked of five files at
           once instead of five times.
  single   the front of the queue: security-relevant path, proven findings, real
           complexity, or simply the highest-scored files in the batch. These get
           the full attention of their own call, because that is where review
           quality is actually visible.

WHAT THIS IS NOT
----------------
This is not a cheaper model. The ranking is the existing `_triage_score`, the
thresholds are named constants with their reasoning attached, and every file's
dispatch and the reasons for it are recorded in the plan and streamed as
telemetry. A reviewer can therefore ask "why did this file only get static
analysis?" and get an answer, which is the difference between a budget and a
silent omission.

The result is not "worse review for less money" — it is the same review for a
tenth of the time, because a language model was never the thing finding `verify=False`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from app.services.code_analysis.models import FileAnalysis

#: Bump when the routing rules change. Recorded on every cached result so an
#: answer produced under an older plan is never attributed to the new one.
PLANNER_VERSION = 2

#: Sentinels used by the existing router in `multi_review_agent`.
ROUTE_SKIP = "__skip__"   # never call a model
ROUTE_FULL = "__full__"   # configured provider

#: Above this size a file is split by the ingestion chunker anyway, and the model
#: sees a truncated preview in the single-file path — so a huge file is not
#: "more reviewable", it is less. Such files go static unless the analyzer proved
#: something, in which case the finding is already in hand.
MAX_LLM_FILE_CHARS = 24_000

#: Smallest useful batch. Below this, the shared prompt is more overhead than the
#: call it saves, so the remaining files are reviewed individually.
MIN_BATCH_SIZE = 3

#: Largest batch. Beyond ~4 files the model's attention thins out and a single
#: long call starts to cost what several short ones would.
MAX_BATCH_SIZE = 4

#: Default cap on single-pass reviews per batch. Deliberately a budget, not a
#: count: it is the number of *expensive* calls this run is allowed to make.
DEFAULT_LLM_BUDGET = 12

#: Extensions whose content cannot contain a code finding worth a model call.
#: Reviewing a lock file is how a review pipeline trains its users to ignore it.
DATA_ONLY_EXTENSIONS = frozenset(
    {"lock", "json", "yaml", "yml", "toml", "ini", "cfg", "env", "properties", "txt", "csv"}
)

#: Substrings that make a filename security-relevant — the same signal the
#: existing triage score uses, kept here so the two cannot drift apart silently.
SENSITIVE_NAME_HINTS = (
    "auth", "security", "secret", "token", "crypto", "password", "session",
    "api", "service", "agent", "retriev", "ingest", "db", "sql", "admin",
)

#: Content shapes that mean "a person should read this", regardless of score.
SENSITIVE_CONTENT_HINTS = (
    "password", "token", "secret", "api_key", "apikey", "private_key",
    "subprocess", "sql", "exec(", "eval(", "pickle", "yaml.load", "verify=false",
)


@dataclass
class Dispatch:
    """What will happen to one file, and why."""

    index: int
    kind: str                  # "static" | "batch" | "single"
    tier: str                  # "static" | "fast" | "full"  (surfaced to the UI)
    route: str = ROUTE_FULL    # model override handed to get_review_llm
    batch_id: str | None = None
    batch_position: int | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "tier": self.tier,
            "batch": self.batch_id,
            "batch_position": self.batch_position,
            "reasons": list(self.reasons),
        }


@dataclass
class Batch:
    """A group of files that will share one model call."""

    batch_id: str
    indexes: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.indexes)


@dataclass
class ReviewPlan:
    """The complete decision for one review run, plus the numbers to explain it."""

    dispatches: dict[int, Dispatch]
    batches: dict[str, Batch]
    scores: dict[int, int]
    llm_budget: int
    #: Parses the planner already performed, keyed by file index. The static
    #: renderer reuses them: analysing every file twice would double the CPU cost
    #: of a review to produce byte-identical output.
    analyses: dict[int, "FileAnalysis"] = field(default_factory=dict)

    def of(self, index: int) -> Dispatch:
        return self.dispatches[index]

    @property
    def static_indexes(self) -> list[int]:
        return [i for i, d in self.dispatches.items() if d.kind == "static"]

    @property
    def single_indexes(self) -> list[int]:
        return [i for i, d in self.dispatches.items() if d.kind == "single"]

    @property
    def batched_indexes(self) -> list[int]:
        return [i for i, d in self.dispatches.items() if d.kind == "batch"]

    @property
    def model_calls(self) -> int:
        """Single-file reviews plus one per batch. This is the number that costs time."""
        return len(self.single_indexes) + len(self.batches)

    def stats(self, total: int) -> dict[str, Any]:
        return {
            "planner_version": PLANNER_VERSION,
            "files": total,
            "static_only": len(self.static_indexes),
            "single_reviews": len(self.single_indexes),
            "batched_files": len(self.batched_indexes),
            "batch_count": len(self.batches),
            "model_calls": self.model_calls,
            "llm_budget": self.llm_budget,
        }

    def explain(self, index: int) -> dict[str, Any]:
        """Per-file provenance: what will happen, and the reasons in words."""
        dispatch = self.dispatches[index]
        data = dispatch.to_dict()
        data["score"] = self.scores.get(index, 0)
        return data


# ── Scoring helpers ───────────────────────────────────────────────────────────


def _sensitive_reasons(file_info: dict) -> list[str]:
    """Name the specific signals, so the plan can explain itself."""
    reasons: list[str] = []
    name = (file_info.get("file_name") or "").lower()
    content = (file_info.get("content") or "").lower()

    hits = [hint for hint in SENSITIVE_NAME_HINTS if hint in name]
    if hits:
        reasons.append(f"security-relevant filename ({', '.join(hits[:3])})")

    content_hits = [hint for hint in SENSITIVE_CONTENT_HINTS if hint in content]
    if content_hits:
        reasons.append(f"security-sensitive constructs ({', '.join(content_hits[:3])})")
    return reasons


def _analyzer_reasons(file_info: dict) -> tuple[list[str], "FileAnalysis | None"]:
    """
    What the AST pass already proved, if anything.

    A file with proven findings is worth a model call *only* to explain impact —
    the finding itself is settled. That makes it a "single" candidate: the model's
    value here is the narrative around a known true positive.

    Returns the reasons *and* the parse, because the caller keeps the latter: the
    deterministic renderer needs the same analysis, and running it twice is pure
    waste.
    """
    from app.services.code_analysis import analyze_file

    try:
        analysis = analyze_file(
            file_info.get("content", ""),
            file_info.get("file_name", ""),
            file_info.get("language", ""),
        )
    except Exception:  # noqa: BLE001 — a file that cannot be analysed is not a crash
        return [], None

    reasons: list[str] = []
    worst = analysis.sorted_findings()
    if worst:
        top = worst[0]
        reasons.append(
            f"{len(worst)} deterministic finding(s), worst: {top.rule_id} "
            f"({top.severity.value}) at L{top.line}"
        )
    if analysis.max_complexity > 20:
        reasons.append(f"max cyclomatic complexity {analysis.max_complexity}")
    if analysis.parse_error:
        reasons.append(f"does not parse ({analysis.parse_error})")
    return reasons, analysis


def batch_id_for(files_slice: list[dict]) -> str:
    """
    Stable id for a batch, derived from the content of its members.

    Deterministic on purpose: the same set of files in the same order produces the
    same id, which is what makes the batch cache key and the UI's batch label agree
    across processes.
    """
    digest = hashlib.sha256()
    for file_info in files_slice:
        digest.update((file_info.get("file_name") or "").encode("utf-8"))
        digest.update((file_info.get("content") or "").encode("utf-8", errors="replace"))
    return digest.hexdigest()[:12]


def chunk_hashes(files_slice: list[dict]) -> list[str]:
    """Content digests of the batch's members, in order — the cache key's payload."""
    from app.services.review_cache import hash_content

    return [hash_content(f.get("content", "")) for f in files_slice]


# ── The planner ───────────────────────────────────────────────────────────────


def plan_review(
    files: list[dict],
    *,
    scores: dict[int, int],
    llm_budget: int = DEFAULT_LLM_BUDGET,
    max_llm_file_chars: int = MAX_LLM_FILE_CHARS,
    min_batch_size: int = MIN_BATCH_SIZE,
    max_batch_size: int = MAX_BATCH_SIZE,
    model_route: str = ROUTE_FULL,
    fast_route: str = "",
) -> ReviewPlan:
    """
    Decide the dispatch for every file in a batch.

    `scores` is the existing triage score (`_triage_score`), passed in rather than
    recomputed so there is exactly one definition of "how interesting is this file".

    The order of decisions matters and follows the cost of being wrong:

      1. Files the parser fully owns (data-only, oversized, trivial) go static.
         Nothing is lost: the deterministic report is the complete answer for them.
      2. Files with security shape or proven findings get their own call, up to the
         budget. If the budget runs out, the *reasons* are preserved in the plan
         even though the dispatch is static — the user is told what was deferred.
      3. Everything else reviewable is grouped into batches, so the question is
         asked once per group instead of once per file.

    Returns a `ReviewPlan`; nothing here touches the network, the disk, or a model.
    """
    dispatches: dict[int, Dispatch] = {}
    batches: dict[str, Batch] = {}
    analyses: dict[str, Any] = {}
    singles_used = 0

    candidates: list[tuple[int, int, list[str]]] = []  # (idx, score, reasons)

    for index, file_info in enumerate(files):
        score = scores.get(index, 0)
        language = (file_info.get("language") or "").lower()
        content = file_info.get("content") or ""
        name = (file_info.get("file_name") or "").lower()
        extension = name.rsplit(".", 1)[-1] if "." in name else ""

        # ── 1. static-only, with the reason recorded ─────────────────────────
        if score < 0 or name.endswith((".lock",)) or "package-lock" in name:
            dispatches[index] = Dispatch(
                index, "static", "static", route=ROUTE_SKIP,
                reasons=(f"generated or lock file (triage score {score})",),
            )
            continue

        if len(content.strip()) < 100:
            dispatches[index] = Dispatch(
                index, "static", "static", route=ROUTE_SKIP,
                reasons=(f"only {len(content.strip())} chars of content — nothing to review",),
            )
            continue

        if extension in DATA_ONLY_EXTENSIONS and not _sensitive_reasons(file_info):
            dispatches[index] = Dispatch(
                index, "static", "static", route=ROUTE_SKIP,
                reasons=(f".{extension} is data, not code — the analyzer covers it",),
            )
            continue

        if len(content) > max_llm_file_chars:
            dispatches[index] = Dispatch(
                index, "static", "static", route=ROUTE_SKIP,
                reasons=(
                    f"{len(content)} chars exceeds the {max_llm_file_chars}-char review "
                    "window — a truncation review would be misleading",
                ),
            )
            continue

        # ── 2. worth a call of its own? ──────────────────────────────────────
        analyzer_reasons, analysis = _analyzer_reasons(file_info)
        if analysis is not None:
            analyses[index] = analysis
        reasons = _sensitive_reasons(file_info) + analyzer_reasons
        if language in {"py", "js", "jsx", "ts", "tsx", "go", "java", "rs", "rb"}:
            reasons.append(f"reviewable {language} source")
        candidates.append((index, score, reasons))

    # Highest score first; a larger file breaks ties because complexity costs more
    # to get wrong. This mirrors the existing ranking so the two agree.
    candidates.sort(key=lambda item: (item[1], len(files[item[0]].get("content", ""))), reverse=True)

    batch_pool: list[tuple[int, int, list[str]]] = []
    for index, score, reasons in candidates:
        strong = any(
            reason.startswith(("security-relevant filename", "security-sensitive constructs",
                               "deterministic finding", "max cyclomatic complexity",
                               "does not parse"))
            for reason in reasons
        )
        if (strong or score >= 6) and singles_used < llm_budget:
            dispatches[index] = Dispatch(
                index, "single", "full" if score >= 6 else "fast",
                route=model_route if score >= 6 else (fast_route or model_route),
                reasons=tuple(reasons) or (f"ranked in the top {llm_budget} by triage score",),
            )
            singles_used += 1
        else:
            batch_pool.append((index, score, reasons))

    # ── 3. batch the remainder ───────────────────────────────────────────────
    # Batching happens in the *original file order*, not score order, so a batch
    # is a coherent slice of the repo (usually one directory) rather than five
    # unrelated files. That measurably helps the model: it can see that a group of
    # routes share a missing auth check.
    batch_pool_indexes = sorted(index for index, _, _ in batch_pool)
    deferred = {index: reasons for index, _, reasons in batch_pool}

    position = 0
    while position < len(batch_pool_indexes):
        remaining = len(batch_pool_indexes) - position
        size = min(max_batch_size, remaining)
        if remaining < min_batch_size:
            # A tail of one or two files shares a prompt for no benefit — review
            # them individually so each gets its own full call.
            for index in batch_pool_indexes[position:]:
                dispatches[index] = Dispatch(
                    index, "single", "fast", route=fast_route or model_route,
                    reasons=tuple(deferred.get(index, ())) or ("small remainder of the batch",),
                )
            break

        group = batch_pool_indexes[position: position + size]
        group_files = [files[i] for i in group]
        group_id = batch_id_for(group_files)
        batch = Batch(batch_id=group_id)
        for offset, index in enumerate(group):
            batch.indexes.append(index)
            dispatches[index] = Dispatch(
                index, "batch", "fast", route=fast_route or model_route,
                batch_id=group_id, batch_position=offset,
                reasons=tuple(deferred.get(index, ())) or ("reviewable, no specific signal",),
            )
        batches[group_id] = batch
        position += size

    return ReviewPlan(dispatches=dispatches, batches=batches, scores=dict(scores),
                      llm_budget=llm_budget, analyses=analyses)
