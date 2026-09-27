"""
eval_rag.py — Automated RAG Evaluation & Benchmarking Suite.

Runs on free local code only: no paid API tier is needed for any metric here, and the
default CI invocation (`--embedder offline --no-rerank`) needs no model weights at all.

WHAT IS MEASURED, and at what granularity
-----------------------------------------
Retrieval, per leg (`bm25_only`, `dense_only`, `fused`, `fused_reranked`) and for the
headline pipeline, over the top-K slice of each ranking:

1. Hit Rate @ K — *file level*: any unit of the ground-truth file appears in positions
   1..K. Answer-key matching is exact (basename, or a directory-qualified path), not a
   substring, because `tests/test_batch_review.py` is not `app/api/review.py`.
2. Span Hit Rate @ K / Span MRR / Span Precision — *unit level*: the retrieved unit's own
   text contains an expected symbol. The stricter pair exists because file-level scoring
   cannot tell a good retrieval from a lucky one on a chunk corpus.
3. MRR @ K — mean 1/rank of the first hit at each of those two granularities.
4. Precision @ K / Span Precision @ K — of the K units, how many were relevant.
5. Symbol Recall — do the expected names appear in the text the model would have been
   shown (the retrieved chunks widened to their parents), regardless of position.
6. Latency, per stage, with the harness's own scoring measured and reported separately so
   it can be excluded honestly rather than quietly (`harness_ms`).

Answer groundedness / faithfulness is NOT computed here: it needs a generated answer to
grade, and this harness stops at retrieval. Claiming it in a benchmark that never called
a model was a category error, and the number that would have been printed for it would
have described the retrieved text, not the answer.

Usage:
  python eval_rag.py --embedder offline --no-rerank   # what CI runs; no weights, no network, $0
  python eval_rag.py --embedding-model sentence-transformers/all-MiniLM-L6-v2 --no-rerank
      # a real local embedder baseline, from cached weights only
  python eval_rag.py --eval-file file.json  # load external JSON benchmark dataset
  python eval_rag.py --top-k 10 --quiet     # machine-readable JSON output

Every way to run it is free: with `LLM_PROVIDER=openai` the embedder the product uses IS a
paid API, and a corpus is ~1,800 calls not one, so `--embedder model` refuses a paid
embedder instead of inheriting it. `--embedding-model NAME` (local weights) or
`LLM_PROVIDER=ollama` are the ways to get a real model's numbers.

WHY 40+ QUERIES?
  - 5 queries isn't statistically meaningful — a lucky cache hit inflates the numbers.
  - 40+ queries across different query types (semantic, lexical, file-scoped, cross-file)
    gives a credible Hit Rate and MRR that you can quote in a README or interview.
  - Industry standard RAG benchmarks (BEIR, HotpotQA) use hundreds of queries; 40+ is
    the minimum that makes variance manageable.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path, PurePosixPath

# Add backend directory to sys.path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from typing import List, Dict, Any, Optional, Sequence
from langchain_core.documents import Document
from app.services.hybrid_retriever import (
    BM25Index,
    reciprocal_rank_fusion,
    two_branch_rrf,
)
from app.services.reranker import RERANK_SCORE_KEY, rerank
from app.services.query_enhancer import extract_file_scope, local_query_variants
from app.services.offline_embedder import DenseIndex
from app.services.parent_child import children_of, dedupe_to_parents, parent_context
from app.services.ast_chunker import MAX_CHUNK_CHARS


# ── Benchmark Dataset ─────────────────────────────────────────────────────────
# 42 benchmark queries covering: service logic, API endpoints, data models,
# configuration, security, testing, and infrastructure.
# Each query has a ground-truth filename and expected symbols that should appear
# in the retrieved chunks.
BENCHMARK_DATASET: List[Dict[str, Any]] = [
    # ── LLM Factory & Models ─────────────────────────────────────────────────
    {
        "query": "Where is the LLM factory configured and how does it pick between providers?",
        "ground_truth_file": "llm_factory.py",
        "expected_symbols": ["get_chat_llm", "get_embedding_fn", "llm_provider"],
    },
    {
        "query": "How are LLM clients cached to avoid rebuilding on every request?",
        "ground_truth_file": "llm_factory.py",
        "expected_symbols": ["lru_cache", "get_chat_llm"],
    },
    {
        "query": "What embedding model is used for local providers and why?",
        "ground_truth_file": "llm_factory.py",
        "expected_symbols": ["HuggingFaceEmbeddings", "all-MiniLM-L6-v2"],
    },
    {
        "query": "How does the system switch between OpenAI, DeepSeek, and Ollama without changing service code?",
        "ground_truth_file": "llm_factory.py",
        "expected_symbols": ["llm_provider", "get_chat_llm", "ollama"],
    },

    # ── Retrieval & RAG Pipeline ──────────────────────────────────────────────
    {
        "query": "How is BM25 hybrid search implemented with Reciprocal Rank Fusion?",
        "ground_truth_file": "hybrid_retriever.py",
        "expected_symbols": ["reciprocal_rank_fusion", "BM25Index", "BM25Okapi"],
    },
    {
        "query": "What is the RRF formula used to combine dense and lexical search results?",
        "ground_truth_file": "hybrid_retriever.py",
        "expected_symbols": ["reciprocal_rank_fusion", "rrf_k", "rank"],
    },
    {
        "query": "How does the BM25 index filter results to a specific repo or file?",
        "ground_truth_file": "hybrid_retriever.py",
        "expected_symbols": ["repo_url", "file_filter", "BM25Index"],
    },
    {
        "query": "What cross-encoder model is used for reranking and how is it warmed up?",
        "ground_truth_file": "reranker.py",
        "expected_symbols": ["_get_cross_encoder", "cross-encoder"],
    },
    {
        "query": "How does reranking improve retrieval precision over pure vector search?",
        "ground_truth_file": "reranker.py",
        "expected_symbols": ["rerank", "cross_encoder", "predict"],
    },
    {
        "query": "How is query expansion and file scope (@filename) extraction handled?",
        "ground_truth_file": "query_enhancer.py",
        "expected_symbols": ["extract_file_scope", "local_query_variants"],
    },
    {
        "query": "How is chat history compacted before being added to the prompt?",
        "ground_truth_file": "query_enhancer.py",
        "expected_symbols": ["compact_chat_history", "max_turns"],
    },

    # ── RAG Stream & Retrieval Service ────────────────────────────────────────
    {
        "query": "How are sources cited in the chat response stream?",
        "ground_truth_file": "retrieval_service.py",
        "expected_symbols": ["__SOURCES__", "json.dumps", "sources"],
    },
    {
        "query": "How does active_repo_url filter retrieval to a single repository?",
        "ground_truth_file": "retrieval_service.py",
        "expected_symbols": ["active_repo_url", "filter", "repo_url"],
    },
    {
        "query": "How is the BM25 index rebuilt when new files are indexed?",
        "ground_truth_file": "retrieval_service.py",
        "expected_symbols": ["_bm25_doc_count", "_bm25_index_cache", "current_count"],
    },
    {
        "query": "What is the SYSTEM_PREFIX prompt and why is context passed as a separate message?",
        "ground_truth_file": "retrieval_service.py",
        "expected_symbols": ["SYSTEM_PREFIX", "HumanMessage", "SystemMessage"],
    },

    # ── Review Agent ──────────────────────────────────────────────────────────
    {
        "query": "@review_agent.py how does the ReAct code review loop work?",
        "ground_truth_file": "review_agent.py",
        "expected_symbols": ["stream_code_review", "max_iterations", "_make_tools"],
    },
    {
        "query": "How are closure-bound tools used to avoid passing file content on every tool call?",
        "ground_truth_file": "review_agent.py",
        "expected_symbols": ["_make_tools", "file_content", "closure"],
    },
    {
        "query": "How does the fast review mode differ from the agentic ReAct mode?",
        "ground_truth_file": "review_agent.py",
        "expected_symbols": ["stream_fast_code_review", "FAST_REVIEW_SYSTEM_PROMPT", "asyncio"],
    },
    {
        "query": "What STATUS stream markers are used to show agent progress in the UI?",
        "ground_truth_file": "review_agent.py",
        "expected_symbols": ["__STATUS__", "__STATUS_END__", "step"],
    },
    {
        "query": "How is a review timeout implemented in the agent loop?",
        "ground_truth_file": "review_agent.py",
        "expected_symbols": ["max_seconds", "loop_start", "monotonic"],
    },

    # ── Ingestion Service ─────────────────────────────────────────────────────
    {
        "query": "How does the ingestion pipeline clone a GitHub repo and split it into chunks?",
        "ground_truth_file": "ingestion_service.py",
        "expected_symbols": ["ingest_github_repo", "RecursiveCharacterTextSplitter"],
    },
    {
        "query": "What file types are excluded from indexing?",
        "ground_truth_file": "ingestion_service.py",
        "expected_symbols": ["ALLOWED_EXTENSIONS", "SKIP_DIRS"],
    },
    {
        "query": "How are indexed repos tracked so the UI can show repo-level stats?",
        "ground_truth_file": "ingestion_service.py",
        "expected_symbols": ["get_indexed_repos", "repo_url", "chunk_count"],
    },
    {
        "query": "How does the clear_index function remove a specific repo's chunks?",
        "ground_truth_file": "ingestion_service.py",
        "expected_symbols": ["clear_index", "delete", "repo_url"],
    },

    # ── Write Agent ───────────────────────────────────────────────────────────
    {
        "query": "How does the write agent support generate, edit, and test modes?",
        "ground_truth_file": "write_agent.py",
        "expected_symbols": ["WriteMode", "generate", "edit", "tests"],
    },
    {
        "query": "How is the target file content reconstructed from ChromaDB chunks for editing?",
        "ground_truth_file": "write_agent.py",
        "expected_symbols": ["_reconstruct_file", "chunk_index", "sorted_chunks"],
    },

    # ── Configuration ─────────────────────────────────────────────────────────
    {
        "query": "How are environment settings validated using Pydantic BaseSettings?",
        "ground_truth_file": "config.py",
        "expected_symbols": ["BaseSettings", "get_settings", "Settings"],
    },
    {
        "query": "How does configs.json override the default model settings?",
        "ground_truth_file": "config.py",
        "expected_symbols": ["configs.json", "ollama_chat_model", "top_k_results"],
    },
    {
        "query": "What CORS origins are whitelisted and why is allow_origins=['*'] avoided?",
        "ground_truth_file": "config.py",
        "expected_symbols": ["cors_origins", "CORS_ORIGINS"],
    },

    # ── API Endpoints ─────────────────────────────────────────────────────────
    {
        "query": "Where is rate limiting configured for the chat and review endpoints?",
        "ground_truth_file": "limiter.py",
        "expected_symbols": ["Limiter", "get_remote_address"],
    },
    {
        "query": "How does the review/file endpoint guard against path traversal attacks?",
        "ground_truth_file": "review.py",
        "expected_symbols": ["validate_file_path", "resolve", "allowed_roots"],
    },
    {
        "query": "How does the ingest/github endpoint stream progress events via SSE?",
        "ground_truth_file": "ingest.py",
        "expected_symbols": ["StreamingResponse", "progress_callback", "data:"],
    },
    {
        "query": "How does the PR webhook endpoint consume git diffs for automated review?",
        "ground_truth_file": "review.py",
        "expected_symbols": ["PRWebhookRequest", "diff", "pr_number"],
    },
    {
        "query": "What does the /health endpoint check and why does it return 503 on failure?",
        "ground_truth_file": "main.py",
        "expected_symbols": ["health_check", "503", "chromadb"],
    },

    # ── Metrics & Observability ────────────────────────────────────────────────
    {
        "query": "How are LLM token counts tracked across chat, review, and write requests?",
        "ground_truth_file": "token_counter.py",
        "expected_symbols": ["TokenUsageCallback", "on_llm_end", "token_usage"],
    },
    {
        "query": "How is per-provider token field name normalisation handled in the callback?",
        "ground_truth_file": "token_counter.py",
        "expected_symbols": ["prompt_token_count", "candidates_token_count", "prompt_tokens"],
    },

    # ── Dependency Graph ──────────────────────────────────────────────────────
    {
        "query": "How are Python import statements parsed to build the dependency graph?",
        "ground_truth_file": "dep_graph.py",
        "expected_symbols": ["extract_imports", "import", "from"],
    },
    {
        "query": "How are graph edges resolved when imports use relative paths?",
        "ground_truth_file": "dep_graph.py",
        "expected_symbols": ["build_dependency_graph", "edge", "nodes"],
    },

    # ── Main App ──────────────────────────────────────────────────────────────
    {
        "query": "Why is the ChromaDB telemetry env var set before any chromadb import in main.py?",
        "ground_truth_file": "main.py",
        "expected_symbols": ["ANONYMIZED_TELEMETRY", "setdefault"],
    },
    {
        "query": "How does the lifespan context manager warm up the reranker at startup?",
        "ground_truth_file": "main.py",
        "expected_symbols": ["lifespan", "_get_cross_encoder", "asyncio.to_thread"],
    },
    {
        "query": "What request size limit middleware is applied and why?",
        "ground_truth_file": "main.py",
        "expected_symbols": ["limit_request_size", "10 * 1024 * 1024", "413"],
    },

    # ── Testing ───────────────────────────────────────────────────────────────
    {
        "query": "How is the FastAPI test client set up in conftest.py?",
        "ground_truth_file": "conftest.py",
        "expected_symbols": ["TestClient", "client", "pytest.fixture"],
    },
    {
        "query": "How does the test for path traversal verify the security validator works?",
        "ground_truth_file": "test_review.py",
        "expected_symbols": ["path_traversal", "422", "/etc/passwd"],
    },
    {
        "query": "How are LLM calls mocked in tests to avoid real API calls?",
        "ground_truth_file": "conftest.py",
        "expected_symbols": ["patch", "mock", "MagicMock"],
    },
]


# ── Evaluation Engine ─────────────────────────────────────────────────────────

def _build_embedder(mode: str, model_pin: Optional[str] = None):
    """
    Return the embedder the dense leg will use — local weights or nothing.

    `offline` is the deterministic hashing embedder: no download, no network, and
    therefore runnable in CI. Its similarities are lexical, so numbers from it prove
    the dense path *works* and say nothing about retrieval quality.

    `model` is what production uses, and `--embedding-model NAME` pins a candidate
    model by name (index and query must then be understood as using that one model).
    Both need the weights available locally. There is deliberately NO silent fallback
    to the offline embedder: a quality number that quietly came from a lexical stand-in
    is worse than a failure, because it looks like evidence.

    The refusal below is not a style rule. `--embedder model` is the CLI default,
    `python eval_rag.py` is the documented way to run it, and the configured provider
    for the paid path is a setting a laptop already has — so "use whatever the product
    uses" would embed ~1,800 corpus units through a paid embedding API. SavFlux is a
    $0 product; the benchmark may not be the thing that spends money. Both checks are
    kept, in this order: the provider gate before anything is imported or constructed,
    and the type check on the object actually returned, because the gate describes the
    factory's branching and the type is the factory's behaviour.
    """
    if mode == "offline" and model_pin:
        raise ValueError(
            "--embedding-model names a real model; --embedder offline is the lexical "
            "stand-in. Pick one — a run cannot claim a model it never loaded."
        )

    if mode == "offline":
        from app.services.offline_embedder import OfflineEmbedder

        return OfflineEmbedder()

    if mode != "model":
        raise ValueError(f"unknown embedder mode {mode!r}; expected 'offline' or 'model'")

    from app.services.llm_factory import (
        LOCAL_EMBEDDING_PROVIDERS,
        build_local_embeddings,
        get_embedding_fn,
    )

    if model_pin:
        try:
            return build_local_embeddings(model_pin)
        except Exception as exc:  # noqa: BLE001 - any load failure is actionable here
            raise RuntimeError(_embedder_load_error(model_pin, exc)) from exc

    from app.core.config import get_settings

    try:
        provider = get_settings().llm_provider
    except Exception as exc:  # pragma: no cover - unreadable config is still not permission to pay
        raise RuntimeError(
            f"Cannot read LLM_PROVIDER to decide whether the configured embedder is "
            f"local ({type(exc).__name__}: {exc}); refusing to build one that might "
            "not be. Pass --embedding-model NAME to pin a local model by name."
        ) from exc
    if provider not in LOCAL_EMBEDDING_PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER={provider!r} embeds through a paid API, and this benchmark "
            f"will not spend money on 1,800+ corpus units. Run with "
            f"--embedding-model <name> for any local model, or LLM_PROVIDER=ollama to "
            f"use the configured one, or --embedder offline for the plumbing-only "
            f"check that needs no weights at all."
        )

    try:
        embedder = get_embedding_fn()
    except Exception as exc:  # noqa: BLE001 - any load failure is actionable here
        raise RuntimeError(_embedder_load_error(None, exc)) from exc

    module = type(embedder).__module__.lower()
    if "openai" in module or "azure" in module or "cohere" in module or "vertexai" in module:
        raise RuntimeError(
            f"The configured embedder is {type(embedder).__name__} from "
            f"{type(embedder).__module__}, which is a paid API. The benchmark refuses "
            "it whatever LLM_PROVIDER says — pass --embedding-model <name> for a local "
            "model, or --embedder offline for the no-weights plumbing check."
        )
    return embedder


def _embedder_load_error(model_pin: Optional[str], exc: BaseException) -> str:
    """The one error text for 'the weights are not here', naming every free way out."""
    named = f"--embedding-model {model_pin}" if model_pin else "the configured EMBEDDING_MODEL"
    return (
        f"Could not load {named}: {type(exc).__name__}: {exc}.\n"
        "The weights have to be available locally: fetch them once with access to "
        "huggingface.co (or an Ollama model), or point HF_HOME at a cache that already "
        "has them. Run with --embedder offline for a plumbing-only check that needs no "
        "weights — but its numbers are lexical, not a quality result, and the artefact "
        "says so."
    )


#: Identifies the matching rule, so a run scored under a different rule cannot be
#: read as a regression or an improvement. Recorded in every artefact.
GROUND_TRUTH_MATCHING = "exact-basename-v2"

#: Which part of a ranking an `@K` metric is computed over. `top-k-slice-v1` means
#: positions 1..K and nothing else. Before this, the fused leg was scored over the whole
#: `top_k * 3`-deep candidate list it hands the reranker, so `hit_rate_at_k` on a
#: `--no-rerank` run — every CI and offline run — was really hit@3K, and a baseline from
#: that era is not a trend for a run from this one. Two definitions change together here,
#: and both are recorded, because "which number moved" is only useful if the artefact
#: says why.
METRIC_DEPTH_RULE = "top-k-slice-v1"

#: The definitions that make a quality number mean what its name says. A run's pair is
#: compared as a unit; see `_measurement_rules_of`.
MEASUREMENT_RULES = ("ground_truth", "metric_depth")


def _unit_matches_file(doc: Document, target_file: str) -> bool:
    """
    Whether a retrieval unit belongs to `target_file`.

    ONE definition, because four places ask this question: the rank of the first hit,
    the precision numerator, the span rank below, and the units-per-file figure the
    report prints beside the hit rate. Copies of a predicate drift, and this
    particular drift is invisible — the metric and the number printed next to it
    would disagree while both looked plausible.

    EXACT basename, not substring. It used to be `needle in name`, and with
    `backend/tests/` inside the corpus (which is right — the tests are indexable
    source, and two queries ask about them) that made four of the dataset's seventeen
    ground truths match several different files at once. Measured over the corpus at
    this commit: `review.py` credited `tests/test_batch_review.py` and
    `tests/test_review.py`, `config.py` credited `tests/test_provider_config.py` and
    `tests/test_embedding_config.py`, `ingest.py` credited `tests/test_delta_ingest.py`,
    and `review_agent.py` credited `app/services/multi_review_agent.py` — a genuinely
    different module. 11 of the 44 queries could be scored a hit by a document that
    answers nothing about them, which inflates hit rate and MRR and cannot be seen in
    either number.

    A ground truth that needs to name one of two same-named files carries a directory
    (`api/review.py`) and is matched as a path suffix; that is the only way to be
    unambiguous about a corpus that contains `app/__init__.py` and five more of those.
    """
    return _ground_truth_matches(*_unit_paths(doc), target_file)


def _ground_truth_matches(name: str, source: str, target_file: str) -> bool:
    """The rule itself, on already-extracted `(basename, source)` strings.

    Split out so the dataset audit can ask the same question of a file path without
    wrapping it in a fake Document first: one rule, two callers, no fixture-shaped
    version of it to drift.
    """
    needle = str(target_file).replace("\\", "/").strip("/").lower()
    if "/" in needle:
        # Qualified: match the path itself, so `api/review.py` is not `app/review.py`.
        return source == needle or source.endswith("/" + needle)
    return name == needle or source.endswith("/" + needle)


def _unit_paths(doc: Document) -> "tuple[str, str]":
    """
    `(basename, source)` for a unit, posix-separated and lowercased.

    Both are needed: `file_name` is what ingestion stamps on every unit, while
    `source` is the only place a directory lives — and the two corpora this harness
    can build put different amounts of path in it (absolute for `files`, repo-relative
    for `chunks`), which is why a qualified ground truth is a SUFFIX rather than a
    prefix. The metadata fields exist on every unit — `_assert_unit_shape` raises if a
    chunk corpus ships one without them, precisely because file-level ground truth has
    nothing else to match on.
    """
    return (
        str(doc.metadata.get("file_name", "")).lower(),
        str(doc.metadata.get("source", "")).replace("\\", "/").lower(),
    )


def _unit_localizes_answer(doc: Document, target_file: str, symbols: Sequence[str]) -> bool:
    """
    Whether this ONE unit is the answer, rather than being from the right file.

    The file-level question above cannot tell a good retrieval from a lucky one: on a
    chunk corpus, a hit is any one of a file's chunks, so a system that surfaces the
    file's import block scores identically to one that surfaces the function the user
    asked about. Requiring an expected symbol inside the unit's own text is what makes
    the two differ.

    Deliberately the unit's text and NOT `parent_context(unit)`: widening to the whole
    parent is what production does before building a prompt, and it is also what makes
    the file-level number meaningless. Scored on the parent, this metric would be the
    symbol-recall figure again. The consequence is stated where it is reported — a
    windowed leg can serve the model the right region and still miss here, because the
    window that matched did not contain the name.
    """
    if not symbols:
        return False
    if not _unit_matches_file(doc, target_file):
        return False
    return any(sym in doc.page_content for sym in symbols)


def _rank_of_span(
    ranked_docs: List[Document], target_file: str, symbols: Sequence[str]
) -> Optional[int]:
    """1-based position of the first unit that IS the answer, else None."""
    for idx, doc in enumerate(ranked_docs, start=1):
        if _unit_localizes_answer(doc, target_file, symbols):
            return idx
    return None


def _spannable(dataset: Sequence[Dict[str, Any]]) -> int:
    """
    Queries that the span metric can score at all: those naming ≥1 expected symbol.

    Counted separately because a query with no symbols cannot be a span hit, and
    scoring it as a miss would report "no symbol was retrieved" for a query that never
    asked for one — the same mistake as reporting an unmeasured stage as 0 ms.
    """
    return sum(1 for item in dataset if item.get("expected_symbols"))


def _rank_of(ranked_docs: List[Document], target_file: str) -> Optional[int]:
    """1-based position of the first unit from `target_file`, else None."""
    for idx, doc in enumerate(ranked_docs, start=1):
        if _unit_matches_file(doc, target_file):
            return idx
    return None


def _empty_leg() -> Dict[str, Any]:
    return {
        "hits": 0,
        "ranks": [],
        "span_hits": 0,
        "span_ranks": [],
        "span_scored": 0,
        "symbol_hits": 0,
        "symbols_expected": 0,
    }


# ── Latency attribution ───────────────────────────────────────────────────────
#
# The harness used to print one `avg_latency_ms` per query, measured from a single
# `t0` at the top of the loop. Two things were wrong with that. It could not answer
# the only question a latency number is for — *which stage* moved — so a reranker
# regression and a query-expansion regression looked identical. And the window it
# measured ended after the leg-scoring code, which joins every retrieved chunk back
# into parent text to check symbol coverage: work the harness does and production
# never does. The published latency therefore included the scoring of the benchmark,
# and the more legs you added, the "slower" retrieval got.
#
# So: one clock per query, one mark per stage, and the scoring stage measured under
# its own name so it can be excluded honestly rather than quietly.

#: The one stage that is not product work. Measured so it can be reported and left
#: out of the totals, never folded into them.
HARNESS_STAGE = "score"


class _StageClock:
    """Wall time per named stage of one query's retrieval, in milliseconds."""

    def __init__(self) -> None:
        self._at = time.monotonic()
        self._ms: Dict[str, float] = {}

    def mark(self, stage: str) -> None:
        """Close `stage`: everything since the previous mark belongs to it."""
        now = time.monotonic()
        self._ms[stage] = self._ms.get(stage, 0.0) + (now - self._at) * 1000
        self._at = now

    def as_dict(self) -> Dict[str, float]:
        return {stage: round(ms, 2) for stage, ms in self._ms.items()}

    def product_ms(self) -> float:
        return round(sum(ms for stage, ms in self._ms.items() if stage != HARNESS_STAGE), 2)


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """
    Nearest-rank percentile, no interpolation.

    Deliberately not numpy-style interpolated percentiles: a benchmark number should
    be a sample that actually happened, and with ~40 queries interpolation invents a
    latency no query ever paid. Returns None for no samples, so a caller cannot read
    "no data" as "zero".
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return round(ordered[index], 2)


def _latency_stats(values: Sequence[float]) -> Dict[str, Any]:
    if not values:
        return {"avg": None, "p50": None, "p95": None, "max": None, "samples": 0}
    return {
        "avg": round(sum(values) / len(values), 2),
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "max": round(max(values), 2),
        "samples": len(values),
    }


def _stage_stats(samples: Sequence[Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    """Per-stage stats over the same queries as the total, dominant stage first."""
    names = sorted({stage for sample in samples for stage in sample})
    stats = {name: _latency_stats([s[name] for s in samples if name in s]) for name in names}
    return dict(sorted(stats.items(), key=lambda item: -(item[1]["p50"] or 0)))


#: Headline quality metrics, with the direction that counts as an improvement.
QUALITY_METRICS = {
    "hit_rate_at_k": "higher",
    "mean_reciprocal_rank_mrr": "higher",
    "symbol_recall_pct": "higher",
    "precision_at_k_pct": "higher",
    # The stricter pair, plus the rate over answerable queries only. Present in the
    # table so a change in them is visible next to the looser numbers they qualify:
    # file-level hit rate up while span-level is flat is retrieval getting luckier,
    # not better.
    "span_hit_rate_at_k": "higher",
    "span_mrr": "higher",
    "span_precision_at_k_pct": "higher",
    "hit_rate_at_k_answerable": "higher",
}

#: Which quality metric each measurement rule can move, so a definition change blocks
#: exactly the rows it invalidates rather than the whole table.
#:
#: The matching rule rewrites what counts as being from the right file, so it touches
#: every file-based metric — but NOT `symbol_recall_pct`, which asks whether a name
#: appears in the text handed to the model and never consults the predicate. Refusing to
#: grade that too would turn a careful guard into a blanket "no data", the same mistake
#: as reporting an unmeasured stage as 0 ms.
#:
#: The depth rule is the opposite case: cutting the scored list to K changes what text
#: symbol recall is computed over, so it invalidates every quality metric (`None`).
MEASUREMENT_SENSITIVITY = {
    "ground_truth": frozenset({
        "hit_rate_at_k",
        "mean_reciprocal_rank_mrr",
        "precision_at_k_pct",
        "hit_rate_at_k_answerable",
        "span_hit_rate_at_k",
        "span_mrr",
        "span_precision_at_k_pct",
    }),
    "metric_depth": None,
}

#: Short names for the verdict column, where "metric_depth" would not fit.
MEASUREMENT_RULE_LABELS = {"ground_truth": "ground truth", "metric_depth": "depth"}


def _changed_rules(key: str, base: Dict[str, str], cur: Dict[str, str]) -> List[str]:
    """Which rule changes invalidate `key` — empty when the row can be graded."""
    return [
        MEASUREMENT_RULE_LABELS[rule]
        for rule, sensitive in MEASUREMENT_SENSITIVITY.items()
        if base.get(rule) != cur.get(rule) and (sensitive is None or key in sensitive)
    ]


#: Latency metrics: a bigger number is worse, so the sign convention is inverted.
LATENCY_METRICS = {
    "avg_latency_ms": "lower",
    "latency_ms.p50": "lower",
    "latency_ms.p95": "lower",
}


def _lookup(nested: Dict[str, Any], dotted: str) -> Any:
    node: Any = nested
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def compare_results(baseline: Dict[str, Any], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Stage-by-stage and metric-by-metric deltas between two runs.

    A single "latency went up 8%" is not actionable; "fuse went up 340%, everything
    else flat" is. Quality and latency are reported in the same table because a
    speedup that costs recall is not a speedup, and the harness is the place where
    that trade is supposed to be visible.
    """
    base_m = baseline.get("metrics", {})
    cur_m = current.get("metrics", {})
    rows: List[Dict[str, Any]] = []

    # A missing key is its own value here, not agreement: two runs from before either
    # rule existed both used substrings and the deep slice, so they ARE comparable with
    # each other, while either of them against a current run is a definition change
    # wearing the clothes of a regression.
    def _measurement_rules_of(result: Dict[str, Any]) -> Dict[str, str]:
        evaluation = result.get("evaluation") or {}
        rules: Dict[str, str] = {}
        for key in MEASUREMENT_RULES:
            block = evaluation.get(key)
            # The matching rule was recorded under its own name before `metric_depth`
            # existed, so look for both spellings rather than calling an old run "unset".
            if isinstance(block, dict):
                rules[key] = str(block.get("rule") or block.get("matching") or "unset")
            else:
                rules[key] = "unset"
        return rules

    base_rules = _measurement_rules_of(baseline)
    cur_rules = _measurement_rules_of(current)

    def add(key: str, better: str, before: Any, after: Any) -> None:
        def record(delta: Optional[float], pct: Optional[float], verdict: str) -> None:
            rows.append({"metric": key, "baseline": before, "current": after,
                         "delta": delta, "pct": pct, "better": better, "verdict": verdict})

        if before is None or after is None:
            # Absent in one file and present in the other: a metric added in this
            # version, not a change. Saying "new baseline" would read as a result.
            record(None, None, "no data")
            return

        delta = round(after - before, 3)
        if delta == 0:
            record(0.0, 0.0, "flat")
            return
        improved = delta > 0 if better == "higher" else delta < 0
        if not before:
            # A change out of (or into) a zero is a real change with no percentage.
            # Dividing by zero to get "-100%" would be worse than saying nothing.
            record(delta, None, ("better" if improved else "WORSE") + " from zero")
            return
        record(delta, round(delta / before * 100, 1), "better" if improved else "WORSE")

    for key, better in QUALITY_METRICS.items():
        before, after = base_m.get(key), cur_m.get(key)
        blocked_by = _changed_rules(key, base_rules, cur_rules)
        if blocked_by:
            # The row names which definition moved, because a stricter answer key and a
            # narrower scored slice are fixed by different things. The numbers stay
            # visible — each is true of its own run — but the verdict refuses to call
            # the difference progress.
            rows.append({"metric": key, "baseline": before, "current": after,
                         "delta": None, "pct": None, "better": better,
                         "verdict": "definition changed: " + ", ".join(blocked_by)})
            continue
        add(key, better, before, after)
    for key, better in LATENCY_METRICS.items():
        add(key, better, _lookup(base_m, key), _lookup(cur_m, key))

    base_stages = base_m.get("stage_ms") or {}
    cur_stages = cur_m.get("stage_ms") or {}
    for stage in sorted(set(base_stages) | set(cur_stages)):
        add(f"stage:{stage}", "lower",
            _lookup(base_stages.get(stage) or {}, "p50"),
            _lookup(cur_stages.get(stage) or {}, "p50"))
    return rows


def _dense_windows(result: Dict[str, Any]) -> int:
    """How many units the dense leg embedded, or 0 when the run predates the count."""
    return (result.get("evaluation", {}).get("dense_corpus", {}) or {}).get("windows", 0)


def format_comparison(rows: List[Dict[str, Any]]) -> str:
    """
    A fixed-width table, because this output is read in a terminal and pasted into a
    PR. The columns are separated by a space each — the first version of this packed
    them edge to edge and the delta ran into the current value in the render.
    """
    lines = ["", "=" * 78, "         SAVFLUX BENCHMARK — DELTA vs BASELINE", "=" * 78]
    lines.append(f"  {'metric':<26} {'baseline':>10} {'current':>10} {'delta':>10} {'change':>9}  verdict")
    lines.append("  " + "-" * 74)

    def fmt(value: Any) -> str:
        if value is None:
            return "—"
        return f"{value:.3f}" if isinstance(value, float) else str(value)

    for row in rows:
        pct = "—" if row["pct"] is None else f"{row['pct']:+.1f}%"
        lines.append(
            f"  {row['metric']:<26} {fmt(row['baseline']):>10} {fmt(row['current']):>10} "
            f"{fmt(row['delta']):>10} {pct:>9}  {row['verdict']}"
        )
    blocked = [r["metric"] for r in rows if r["verdict"].startswith("definition changed")]
    if blocked:
        reasons = sorted({
            part
            for row in rows
            if row["verdict"].startswith("definition changed")
            for part in row["verdict"].split(": ", 1)[1].split(", ")
        })
        listed = ", ".join(blocked[:3]) + (", …" if len(blocked) > 3 else "")
        lines.append("")
        lines.append("  Quality rows are not graded: the two runs score different definitions")
        lines.append(f"  ({len(blocked)} row(s): {listed}). Differing: {' and '.join(reasons)}.")
        if "ground truth" in reasons:
            lines.append("  Ground truth: the baseline counted ANY file whose NAME contained the")
            lines.append("  answer key as a hit, so `tests/test_batch_review.py` satisfied a query")
            lines.append("  about `api/review.py`; the current run matches the file exactly.")
        if "depth" in reasons:
            lines.append("  Depth: the baseline scored an @K metric over the whole candidate list")
            lines.append("  handed to the reranker (3K deep), which is not hit@K; the current run")
            lines.append("  counts positions 1..K and lists the deeper position separately.")
        lines.append("  Neither number is wrong. They are not the same metric.")
    lines.append("")
    lines.append("  Latency is measured on this machine; only compare runs taken on the")
    lines.append("  same corpus, same top_k, and with the reranker in the same state.")
    return "\n".join(lines)


def _units_per_expected_file_stats(counts: List[int]) -> Dict[str, Any]:
    """
    How many retrieval units each query's expected file contributed.

    This is the honesty figure beside a file-level hit rate: it says how many
    chances a "hit" had. A chunk-shaped corpus gives a file many units, so a hit
    means "one of these landed in top-K", while a miss means none of them did — an
    asymmetry the hit rate alone hides.
    """
    if not counts:
        return {"min": 0, "median": 0, "p90": 0, "max": 0, "queries_with_no_unit": 0}
    ordered = sorted(counts)
    return {
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))],
        "max": ordered[-1],
        # A query whose expected file contributed no unit cannot be a hit whatever
        # the retriever does. That is a property of the corpus, not of retrieval,
        # and it must not be readable as a quality result.
        "queries_with_no_unit": sum(1 for c in ordered if c == 0),
    }


def _dense_corpus(docs: List[Document]) -> List[Document]:
    """
    The documents the dense leg indexes: embed windows, not whole files.

    WHY THIS DIFFERS FROM `corpus_docs`
    -----------------------------------
    Ingestion stores one row per embed window, because the embedder reads 256
    tokens and silently discards everything after them — 46-55% of this project's
    chunks are longer than that window. Embedding whole files here would measure a
    retrieval path production does not have, and would hide the exact defect this
    harness exists to catch: a query whose answer sits past the window boundary is
    unreachable by the dense leg, and the harness would record that as the
    embedder's retrieval quality.

    The windows come from `children_of`, the function ingestion calls, so this
    models the pipeline instead of reimplementing it in a form that can drift.

    BM25 deliberately does NOT get these. It has no window, and production feeds
    it whole chunks (`_bm25_corpus` in retrieval_service). The two legs read
    different text from the same row, on purpose.
    """
    out: List[Document] = []
    for doc in docs:
        out.extend(children_of(doc))
    return out


async def evaluate_pipeline(
    dataset: List[Dict[str, Any]],
    corpus_docs: List[Document],
    top_k: int = 5,
    verbose: bool = True,
    embedder_mode: str = "model",
    rerank_enabled: bool = True,
    embedder_pin: Optional[str] = None,
    corpus_shape: str = "chunks",
    corpus_files: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Evaluates retrieval performance across a benchmark dataset, PER RETRIEVAL LEG.

    Metrics computed:
    - Hit Rate @ K:      fraction of queries where the target file was in top-K results
    - MRR:               mean of 1/rank for the first hit (0 if miss)
    - Symbol Recall:     fraction of expected code symbols found in retrieved chunks
    - Avg latency (ms):  mean retrieval + reranking time per query

    WHY PER-LEG
    -----------
    This benchmark used to run BM25 through flat RRF and call it "the pipeline". It
    was neither the pipeline production runs (which adds a dense branch and fuses with
    weighted two_branch_rrf) nor a measurement of the embedding model, which it never
    loaded — so swapping the embedder could not move any number it produced.

    Reporting each leg separately answers the question that actually decides whether
    an embedder swap is worth a re-index: how much does the dense branch contribute
    over BM25 alone, on this query mix? If BM25-only already scores highly, semantic
    retrieval is not the bottleneck and a new embedder is money spent on the wrong
    leg. If dense adds a lot, it is.

    `bm25_only` and `dense_only` are each fused within their own branch with the same
    RRF used in production; `fused` is production's weighted two_branch_rrf.
    """
    bm25 = BM25Index(corpus_docs)

    dense_docs = _dense_corpus(corpus_docs)
    # `DenseIndex.__init__` is where every window is embedded, so the clock brackets the
    # construction. A model that is 10x better at retrieval and takes 40 minutes to index
    # is still not what a $0 laptop tool ships with, and nothing else here would say so.
    embed_started = time.perf_counter()
    dense = DenseIndex(dense_docs, _build_embedder(embedder_mode, embedder_pin))
    embed_index_ms = round((time.perf_counter() - embed_started) * 1000.0, 2)
    embed_units_per_second = (
        round(len(dense_docs) / (embed_index_ms / 1000.0), 2) if embed_index_ms > 0 else None
    )

    legs: Dict[str, Dict[str, Any]] = {
        "bm25_only": _empty_leg(),
        "dense_only": _empty_leg(),
        "fused": _empty_leg(),
        "fused_reranked": _empty_leg(),
    }
    rerank_observed = False

    hits_at_k = 0
    reciprocal_ranks: List[float] = []
    span_hits_at_k = 0
    span_reciprocal_ranks: List[float] = []
    span_precision_values: List[float] = []
    answerable_hits: List[bool] = []
    unanswerable: List[str] = []
    symbol_hits = 0
    total_expected_symbols = 0
    latencies: List[float] = []
    stage_samples: List[Dict[str, float]] = []
    precision_values: List[float] = []
    unit_counts: List[int] = []

    query_results = []

    # ── The dataset is part of the instrument ─────────────────────────────────
    # Two ways a benchmark query measures nothing, and neither is visible in the
    # output it produces: a ground truth that matches NO unit in the corpus can never
    # hit (the query is a guaranteed miss, and hit rate becomes a report on the file
    # list), and a ground truth that matches units of SEVERAL files credits a document
    # that answers nothing about the query. The first is reported; the second is a bug
    # in the dataset and stops the run, because "ambiguous" is not a property of the
    # code under test but of how the question was written, and a pin to an older
    # checkout cannot invent a second file with the same basename.
    corpus_paths = sorted({doc.metadata.get("source", "") for doc in corpus_docs})
    ambiguous: Dict[str, List[str]] = {}
    for ground_truth in sorted({item["ground_truth_file"] for item in dataset}):
        matched = sorted(
            source
            for source in corpus_paths
            if _ground_truth_matches(PurePosixPath(source).name, source, ground_truth)
        )
        if len(matched) > 1:
            ambiguous[ground_truth] = matched
    if ambiguous:
        detail = "; ".join(
            f"{gt} matches {len(files)} files ({', '.join(files[:3])}"
            + (", …)" if len(files) > 3 else ")")
            for gt, files in ambiguous.items()
        )
        raise RuntimeError(
            "ambiguous ground truth — qualify it with a directory so it names one "
            f"file: {detail}"
        )

    for item in dataset:
        raw_query = item["query"]
        target_file = item["ground_truth_file"]
        expected_syms = item.get("expected_symbols", [])
        # How many units of the corpus this query's file contributed — i.e. how many
        # chances the file-level hit had. Counted with the same predicate the rank
        # is scored with, so the figure describes the metric beside it.
        expected_file_units = sum(
            1 for doc in corpus_docs if _unit_matches_file(doc, target_file)
        )
        unit_counts.append(expected_file_units)

        clock = _StageClock()
        query, file_scope = extract_file_scope(raw_query)

        # Same cheap query-variant strategy production uses, on both branches.
        variants = list(local_query_variants(query))
        clock.mark("plan")
        bm25_lists = [
            bm25.search(query=variant, top_k=top_k * 3, file_filter=file_scope)
            for variant in variants
        ]
        clock.mark("lexical")
        # The dense branch searches WINDOWS, and many of them belong to the same
        # file, so its candidates are collapsed to one entry per file before being
        # compared against the BM25 branch — whose corpus is already one entry per
        # file. Ranking every window and then collapsing gives both branches the
        # same candidate depth (top_k*3 files), so the only thing this comparison
        # varies is COVERAGE: before, a file's vector was its first window; now the
        # file is reachable through any of its windows.
        #
        # Leaving the depth uncollapsed would let a depth change masquerade as a
        # coverage improvement, which is the failure mode this whole harness exists
        # to prevent. It costs nothing: the vectors are precomputed, so this is a
        # sort over `len(dense_docs)` similarities.
        dense_lists = [
            dedupe_to_parents(dense.search(query=variant, top_k=len(dense_docs)))[: top_k * 3]
            for variant in variants
        ]
        # Embedding the query is part of `dense`, not its own stage: with the
        # offline embedder it is a hash, and reporting it separately would be a
        # number that describes the fake model rather than anything a user pays.
        clock.mark("dense")

        # ── Each leg, scored independently ───────────────────────────────────
        leg_rankings = {
            "bm25_only": reciprocal_rank_fusion(bm25_lists, top_n=top_k),
            "dense_only": reciprocal_rank_fusion(dense_lists, top_n=top_k),
            # Production's fusion: weighted branches, not flat RRF over both lists,
            # which would give BM25 N× the weight for N query variants.
            "fused": two_branch_rrf(dense_lists, bm25_lists, top_n=top_k * 3),
        }

        clock.mark("fuse")

        if rerank_enabled:
            inner_reranked = await rerank(query, leg_rankings["fused"], top_n=top_k)
            # A leg must not be reported as measured when it silently did not run.
            # `rerank` returns its input unchanged when the cross-encoder cannot be
            # loaded, so the presence of its score is the only evidence it ran — and
            # asserting it here keeps a missing model from masquerading as a result.
            if any(RERANK_SCORE_KEY in d.metadata for d in inner_reranked):
                rerank_observed = True
            leg_rankings["fused_reranked"] = inner_reranked
        else:
            leg_rankings["fused_reranked"] = leg_rankings["fused"]
        clock.mark("rerank")

        for leg_name, full_ranking in leg_rankings.items():
            leg = legs[leg_name]
            # K, and only K. The fused leg is deliberately built `top_k * 3` deep —
            # production reranks from a longer candidate list — and scoring the hit over
            # that whole list meant `hit_rate_at_k` with --no-rerank (i.e. every CI and
            # offline run) measured hit@3K. Measured on the last committed artefact: 8 of
            # its 35 claimed hits sat at rank 6..13, outside the K the metric is named
            # after. `rank_in_fused_list` below keeps the deep position where it is
            # useful as a diagnostic instead of hidden.
            ranking = full_ranking[:top_k]
            leg_rank = _rank_of(ranking, target_file)
            if leg_rank is not None:
                leg["hits"] += 1
                leg["ranks"].append(1.0 / leg_rank)
            else:
                leg["ranks"].append(0.0)

            if expected_syms:
                leg["span_scored"] += 1
                leg_span_rank = _rank_of_span(ranking, target_file, expected_syms)
                if leg_span_rank is not None:
                    leg["span_hits"] += 1
                    leg["span_ranks"].append(1.0 / leg_span_rank)
                else:
                    leg["span_ranks"].append(0.0)

            # Score symbol recall on what the MODEL would be shown, which is the
            # whole parent: production retrieves windows and widens them back with
            # `parent_context` before building the prompt. Scoring the retrieved
            # text itself would penalise the dense leg for the very narrowing that
            # lets it escape truncation, and the penalty would look like a
            # retrieval-quality result.
            leg_text = " ".join(parent_context(d) for d in ranking)  # the K the model sees
            leg["symbol_hits"] += sum(1 for s in expected_syms if s in leg_text)
            leg["symbols_expected"] += len(expected_syms)

        # Everything above this line that is not scoring is retrieval a user pays
        # for; everything between the leg loop and here is the harness reading its
        # own work back. Marked before the total so `product_ms` excludes it.
        clock.mark(HARNESS_STAGE)

        # The headline metrics follow production: fused, then reranked, then cut to K.
        fused_full = leg_rankings["fused_reranked"]
        ranked_docs = fused_full[:top_k]

        latency_ms = clock.product_ms()
        latencies.append(latency_ms)
        stage_samples.append(clock.as_dict())

        # ── Hit Rate & MRR ────────────────────────────────────────────────────
        rank = _rank_of(ranked_docs, target_file)

        if rank is not None:
            hits_at_k += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

        span_rank = _rank_of_span(ranked_docs, target_file, expected_syms)
        if expected_syms:
            if span_rank is not None:
                span_hits_at_k += 1
                span_reciprocal_ranks.append(1.0 / span_rank)
            else:
                span_reciprocal_ranks.append(0.0)

        relevant_count = sum(
            1 for doc in ranked_docs if _unit_matches_file(doc, target_file)
        )
        precision_values.append(relevant_count / len(ranked_docs) if ranked_docs else 0.0)
        span_relevant = sum(
            1 for doc in ranked_docs if _unit_localizes_answer(doc, target_file, expected_syms)
        )
        if expected_syms and ranked_docs:
            span_precision_values.append(span_relevant / len(ranked_docs))

        if expected_file_units:
            answerable_hits.append(rank is not None)
        else:
            # A query whose file contributed nothing is reported as unanswerable and
            # kept IN the headline denominator: dropping it would raise the hit rate
            # of the same retrieval, which is a flattering lie about the instrument.
            unanswerable.append(target_file)

        # ── Symbol Coverage ───────────────────────────────────────────────────
        retrieved_text = " ".join(parent_context(doc) for doc in ranked_docs)
        matched_symbols = [sym for sym in expected_syms if sym in retrieved_text]
        symbol_hits += len(matched_symbols)
        total_expected_symbols += len(expected_syms)

        deep_rank = _rank_of(fused_full, target_file)
        result = {
            "query": raw_query,
            "target_file": target_file,
            "rank": rank,
            "hit": rank is not None,
            # Where it landed in the candidate list the reranker was choosing from,
            # which can be past K. Informational only — no metric reads it — so that a
            # miss at rank 6 is still distinguishable from a miss at rank 600.
            "rank_in_fused_list": deep_rank,
            # The stricter question: not "was the file retrieved" but "was the chunk
            # that carries the answer retrieved". Absent, not 0, when the query names
            # no symbols to localise on.
            "span_rank": span_rank if expected_syms else None,
            "answerable": bool(expected_file_units),
            "symbols_matched": len(matched_symbols),
            "symbols_expected": len(expected_syms),
            "expected_file_units": expected_file_units,
            "latency_ms": latency_ms,
            "stage_ms": clock.as_dict(),
        }
        query_results.append(result)

        if verbose:
            status = f"✅ Rank #{rank}" if rank is not None else "❌ Miss"
            syms = f"{len(matched_symbols)}/{len(expected_syms)} symbols"
            print(f"  [{status}] {raw_query[:60]:<60} → {target_file} ({syms})")

    total_queries = len(dataset)
    span_scored = len(span_reciprocal_ranks)
    answerable_queries = len(answerable_hits)
    hit_rate     = hits_at_k / total_queries if total_queries else 0.0
    mrr          = sum(reciprocal_ranks) / total_queries if total_queries else 0.0
    sym_recall   = symbol_hits / total_expected_symbols if total_expected_symbols else 0.0
    avg_latency  = sum(latencies) / len(latencies) if latencies else 0.0

    def _leg_metrics(leg: Dict[str, Any]) -> Dict[str, Any]:
        n = total_queries or 1
        scored = leg["span_scored"]
        return {
            "hit_rate_at_k": round(leg["hits"] / n * 100, 2),
            "mean_reciprocal_rank_mrr": round(sum(leg["ranks"]) / n, 3),
            # None, not 0.0, when no query in the dataset named a symbol: this leg was
            # not measured at this granularity, and a zero would read as a failure to
            # localise rather than as an absence of anything to localise on.
            "span_hit_rate_at_k": round(leg["span_hits"] / scored * 100, 2) if scored else None,
            "span_mrr": round(sum(leg["span_ranks"]) / scored, 3) if scored else None,
            "symbol_recall_pct": round(
                leg["symbol_hits"] / leg["symbols_expected"] * 100
                if leg["symbols_expected"]
                else 0.0,
                2,
            ),
        }

    by_leg = {name: _leg_metrics(leg) for name, leg in legs.items()}
    if not rerank_enabled:
        # Reported as null rather than as a number equal to the fused leg. A leg that
        # did not execute must not appear in a results file as though it had.
        by_leg["fused_reranked"] = {
            "hit_rate_at_k": None,
            "mean_reciprocal_rank_mrr": None,
            "span_hit_rate_at_k": None,
            "span_mrr": None,
            "symbol_recall_pct": None,
            "note": "reranking disabled (--no-rerank)",
        }
    elif not rerank_observed:
        by_leg["fused_reranked"] = {
            "hit_rate_at_k": None,
            "mean_reciprocal_rank_mrr": None,
            "span_hit_rate_at_k": None,
            "span_mrr": None,
            "symbol_recall_pct": None,
            "note": "cross-encoder unavailable — reranking did not run",
        }

    result = {
        "metrics": {
            "total_queries": total_queries,
            "hit_rate_at_k": round(hit_rate * 100, 2),
            # What "hit" means. A unit is a chunk, but the predicate is "a unit from
            # the expected FILE", so this is a file-level metric and is labelled as
            # one everywhere it is reported — a file-level hit must not be readable
            # as "the answer was retrieved".
            "hit_rate_at_k_granularity": "file",
            "mean_reciprocal_rank_mrr": round(mrr, 3),
            "symbol_recall_pct": round(sym_recall * 100, 2),
            # ── The same retrieval, scored at the granularity of an answer ──────
            # Both numbers are printed because they answer different questions, and
            # only the pair says whether retrieval is localising or merely lucky:
            # file-level hit rate counts ANY chunk of the right file, so a retriever
            # that returns a module's import block is scored as if it had found the
            # function the user asked about. Span level requires an expected symbol
            # inside the retrieved unit itself.
            "span_hit_rate_at_k": round(
                span_hits_at_k / span_scored * 100, 2
            ) if span_scored else None,
            "span_mrr": round(
                sum(span_reciprocal_ranks) / span_scored, 3
            ) if span_scored else None,
            "span_precision_at_k_pct": round(
                (sum(span_precision_values) / len(span_precision_values)) * 100, 2
            ) if span_precision_values else None,
            "span_scored_queries": span_scored,
            "granularity_note": (
                "file level = any unit from the ground-truth file; span level = a unit "
                "from that file whose own text contains an expected symbol, so a "
                "windowed leg can legitimately serve the model the right region and "
                "still miss here"
            ),
            # The instrument's own health, kept in front of the score it could distort.
            "answerable_queries": answerable_queries,
            "unanswerable_queries": len(unanswerable),
            "hit_rate_at_k_answerable": round(
                sum(1 for hit in answerable_hits if hit) / answerable_queries * 100, 2
            ) if answerable_queries else None,
            "avg_latency_ms": round(avg_latency, 1),
            # The distribution, not just the mean: a retrieval path that is 40 ms
            # for 39 queries and 4 s for one has a mean that describes nothing, and
            # the tail is what a user notices.
            "latency_ms": _latency_stats(latencies),
            "stage_ms": _stage_stats(stage_samples),
            "harness_ms": _latency_stats([s.get(HARNESS_STAGE, 0.0) for s in stage_samples]),
            "latency_scope": (
                "retrieval, fusion and reranking per query; excludes harness-side "
                "symbol scoring, which is reported separately as `harness_ms`"
            ),
            "precision_at_k_pct": round(
                (sum(precision_values) / len(precision_values) if precision_values else 0.0) * 100,
                2,
            ),
            "by_leg": by_leg,
            # The label names the model when one was pinned, because "configured model"
            # cannot tell two baselines apart after the fact.
            "dense_leg_embedder": (
                "offline-hashing (NOT a quality model)"
                if embedder_mode == "offline"
                else (f"local model {embedder_pin} (pinned)"
                      if embedder_pin else "configured model")
            ),
            # Embedding the corpus is what a real model makes expensive, so the harness
            # times it. Deliberately outside the per-query latency rows: an index build
            # happens once per re-index, and mixing the two would make a slower embedder
            # look like slower retrieval. Also why a unit rate is reported — an absolute
            # time says nothing about the size of the corpus it was paid over.
            "embed_index_ms": embed_index_ms,
            "embed_units_per_second": embed_units_per_second,
        },
        "query_breakdown": query_results,
    }
    result["evaluation"] = {
        "top_k": top_k,
        "dataset_queries": total_queries,
        "dataset_sha256": hashlib.sha256(
            json.dumps(dataset, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "retrieval": (
            "dense + BM25 (weighted two-branch RRF)"
            + (" + cross-encoder reranking" if rerank_enabled else " (reranking disabled)")
        ),
        # What ONE retrieval unit is, and how many there are. `units` replaced a key
        # called `files`, which stopped being true when the corpus became chunks and
        # would have gone on reading plausibly. This is the key that tells a run
        # against production's units apart from one against whole files.
        "corpus_shape": corpus_shape,
        "corpus_source_files": corpus_files,
        "units_per_expected_file": _units_per_expected_file_stats(unit_counts),
        # The dense branch indexes windows, so its numbers are not comparable with
        # runs from before this key existed — those embedded whole files, truncated
        # at the embedder's window. Recording the counts makes the two kinds of run
        # tellable apart in the artefact instead of only in the commit message.
        "dense_corpus": {
            "units": len(corpus_docs),
            "windows": len(dense_docs),
        },
        # Identifies WHICH source produced these numbers. Two runs from different
        # commits are not comparable, and without this they look identical.
        "corpus_sha256": corpus_fingerprint(corpus_docs),
        "metric_depth": {
            "rule": METRIC_DEPTH_RULE,
            "top_k": top_k,
            "note": (
                "every @K metric counts positions 1..K of the leg's ranking; the "
                "candidate list the reranker is choosing from is deeper than that"
            ),
        },
        # Which matching rule produced the file-level numbers. A run scored with the
        # old substring rule credited `tests/test_batch_review.py` for a query about
        # `api/review.py`; comparing that run with one scored exactly would read as a
        # regression caused by the code under test. `compare_results` refuses to grade
        # quality across a difference here, and treats a missing key as its own value
        # ("unknown") rather than as agreement.
        "ground_truth": {
            "matching": GROUND_TRUTH_MATCHING,
            "span_metric": "expected symbol inside the retrieved unit",
            "unanswerable": sorted(set(unanswerable)),
            "ground_truth_files": len({item["ground_truth_file"] for item in dataset}),
            # An audit result, not a claim: `evaluate_pipeline` raises if any ground
            # truth here matched more than one file in the corpus it was given, so a
            # run that produced this block passed that check.
            "audit": "each ground truth resolved to exactly one corpus file",
        },
    }
    result["models"] = _model_provenance(embedder_mode, embedder_pin)
    return result


def _model_provenance(embedder_mode: str = "model", embedder_pin: Optional[str] = None) -> Dict[str, str]:
    """
    Record WHICH models produced these numbers.

    Without this the benchmark is not a benchmark. Comparing a run on
    all-MiniLM-L6-v2 with a run on jina-embeddings-v2-base-code is the entire
    point of having a harness, and two JSON files that both say
    "BM25 + RRF + cross-encoder reranking" cannot be told apart after the fact.

    Nothing here loads a model: it reads configuration and, for the reranker, the
    module constant that names it. A provenance block that required downloading
    2 GB to print a string would not get printed.
    """
    provenance: Dict[str, str] = {
        "retrieval": "dense + BM25 (weighted two-branch RRF) + cross-encoder reranking",
        # Which embedder produced the dense-leg numbers. Without this, an offline
        # plumbing run and a real quality run are indistinguishable in the artefact.
        "dense_embedder_mode": embedder_mode,
    }

    try:
        from app.core.config import get_settings

        s = get_settings()
        provenance["provider"] = s.llm_provider
        if s.llm_provider == "openai":
            provenance["embedder"] = s.openai_embedding_model
        else:
            provenance["embedder"] = s.embedding_model
            provenance["embedder_device"] = s.embedding_device
            provenance["embedder_batch_size"] = str(s.embedding_batch_size)
    except Exception as exc:  # pragma: no cover - config must never break a report
        provenance["embedder"] = f"<unavailable: {type(exc).__name__}>"

    if embedder_pin:
        # A pinned candidate is not the configured model, and a reader has to be able to
        # tell the two apart: the settings block above describes what production would use,
        # so say plainly that this run overrode it.
        provenance["embedder"] = embedder_pin
        provenance["embedder_source"] = "pinned by --embedding-model, not the configured model"

    # The reranker is a module constant, not a setting — read it, don't guess it.
    try:
        from app.services.reranker import RERANKER_MODEL

        provenance["reranker"] = RERANKER_MODEL
    except Exception as exc:  # pragma: no cover
        provenance["reranker"] = f"<unavailable: {type(exc).__name__}>"

    return provenance


# The corpus shape: what ONE retrieval unit is. "chunks" is what ingestion writes
# and therefore what the retriever ranks. "files" is the shape this harness used to
# measure with, kept so an existing baseline stays reproducible and both shapings can
# be compared in one run — it is not production's unit and the result says so.
CORPUS_SHAPES = ("chunks", "files")

# Stands in for the clone URL ingestion stamps. Using it instead of the local
# absolute path keeps every `source` repo-relative, so a corpus fingerprinted on one
# machine matches the same corpus checked out at a different path — which is what
# `corpus_fingerprint`'s docstring has always claimed to do.
HARNESS_REPO_URL = "local://savflux-eval-corpus"


CORPUS_EXCLUDED_DIRS = frozenset({
    # Virtualenvs and installed dependencies. `rglob("*.py")` does not know the
    # difference between this project and everything pip installed alongside it.
    ".venv", "venv", "env", "site-packages", "dist-packages",
    "node_modules", "__pycache__",
    # Tooling and build output.
    ".git", ".mypy_cache", ".pytest_cache", ".tox", ".ruff_cache",
    "build", "dist", ".eggs",
})


def _is_project_source(path: Path, root: Path) -> bool:
    """
    True when `path` is this project's own source rather than an installed package.

    WHY THIS EXISTS
    ---------------
    `rglob("*.py")` over `backend/` matched **19,501 files** here instead of the
    ~1,200 that are actually this project, because it descended into
    `backend/.venv/lib/python3.11/site-packages/`. Two consequences, both bad:

    1. It was slow for no reason — building the BM25 index over 19.5k documents took
       125 seconds, and every dense query scored against all of them.
    2. Worse, **the corpus depended on the machine**. Install one more package and
       the benchmark scores the same queries against a different document set, so a
       hit-rate from CI is not comparable with a hit-rate from a laptop. A metric
       that moves when the environment moves cannot support a decision.

    Hidden directories other than the root are excluded too: they are tooling, not
    source, and their contents vary between machines.
    """
    try:
        relative_parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return False
    return not any(
        part in CORPUS_EXCLUDED_DIRS or part.startswith(".")
        for part in relative_parts
    )


def resolve_corpus_dir(cli_value: Optional[str], repo_root: Path) -> Path:
    """
    Where to build the corpus from: an explicit pin, or this repo's backend/.

    Split out of `__main__` so the flag has a seam a test can reach. The default
    has to be derived from `repo_root` rather than the working directory, because
    the benchmark is run both from the repo root (CI, the docs) and from inside
    `backend/`, and a corpus that depends on where you stood would be the same class
    of problem as the one this flag exists to fix.
    """
    if cli_value:
        return Path(cli_value)
    return repo_root / "backend"


def corpus_fingerprint(docs: List[Document]) -> str:
    """
    A hash that identifies this exact corpus, so two runs can be told apart.

    WHY THIS IS NEEDED
    ------------------
    The corpus is this project's own source. That is deliberate — the benchmark asks
    answerable questions about real code — but it means the corpus changes whenever
    the product changes. Add a test file and `bm25_only` moves, not because retrieval
    changed but because the document set did. Two runs from different commits are
    therefore not comparable, and nothing in the artifact said so: both carried a
    hit rate and a `dataset_sha256` for the *queries*, which were identical.

    That is the same failure the per-leg work was about. A metric that moves when
    the environment moves cannot support a decision, and one that moves when the
    code moves cannot support a comparison.

    The fix is not to freeze the corpus — it has to track the code to be worth
    measuring. It is to make the drift visible: `--corpus-dir` pins a checkout so two
    runs CAN be compared, and this hash records which corpus produced each run so a
    comparison never rests on an assumption. Same intent as `dense_embedder_mode`.

    Hashes content and relative path, both, because moving a file between packages
    changes which module a symbol lives in without changing a byte of its text.
    """
    digest = hashlib.sha256()
    for doc in sorted(docs, key=lambda d: d.metadata.get("source", "")):
        digest.update(doc.metadata.get("source", "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(doc.page_content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def project_source_files(backend_dir: Path) -> List[Path]:
    """This project's own Python files under `backend_dir`, in a stable order."""
    return [
        p for p in sorted(backend_dir.rglob("*.py"))
        if _is_project_source(p, backend_dir)
    ]


def _file_units(files: List[Path]) -> List[Document]:
    """
    The old shape: one unit per FILE. Kept for reproducing pre-existing baselines.

    A read failure raises rather than skipping the file. Skipping silently shrinks
    the corpus, and a smaller corpus produces a perfectly plausible number.
    """
    units = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(f"could not read corpus file {path}: {exc}") from exc
        units.append(Document(
            page_content=content,
            metadata={"source": str(path), "file_name": path.name},
        ))
    return units


def _chunk_units(files: List[Path], source_root: Path) -> List[Document]:
    """
    Production's units: one chunk per symbol, built by ingestion's own path.

    Calling `_load_and_split` rather than `chunk_code_file` directly is deliberate.
    That function is the whole file→chunks path — AST boundaries where a parser
    exists, the character-splitter fallback where it does not, the content hash and
    the source IDs. Re-deriving it here would let the benchmark measure a corpus
    production never builds, which is the defect this function exists to fix.

    One file at a time so a file that contributes nothing is *attributable*. A
    corpus that quietly drops a file is smaller on one machine than on another, and
    the run still prints a number.
    """
    from app.services.ingestion_service import _load_and_split

    units: List[Document] = []
    for path in files:
        if path.stat().st_size == 0:
            # An empty file has nothing to index and ingestion writes no rows for
            # it either. Counted, not guessed at: the corpus block reports how many
            # source files contributed no units.
            continue
        produced = _load_and_split(
            [path], repo_url=HARNESS_REPO_URL, source_root=source_root
        )
        if not produced:
            raise RuntimeError(
                f"{path} is not empty but produced no retrieval units. The corpus "
                "would be silently smaller than the source tree — fix the read or "
                "exclude the file deliberately."
            )
        units.extend(produced)
    return units


def _assert_unit_shape(units: List[Document], shape: str) -> None:
    """
    Assert the corpus shape instead of trusting it.

    Both checks are loud because both failures are otherwise silent: oversized units
    quietly make the harness measure a retrieval unit production never ranks, and
    units without a source make file-level ground truth unreachable while the hit
    rate still looks like a quality result.
    """
    if shape != "chunks":
        return

    oversized = [d for d in units if len(d.page_content) > MAX_CHUNK_CHARS]
    if oversized:
        largest = max(len(d.page_content) for d in oversized)
        raise RuntimeError(
            f"{len(oversized)} corpus units exceed MAX_CHUNK_CHARS "
            f"({MAX_CHUNK_CHARS}); the largest is {largest} characters. Production "
            "caps a chunk at that size, so these are not the units it ranks."
        )

    unlabelled = [
        d for d in units
        if not d.metadata.get("file_name") or not d.metadata.get("source")
    ]
    if unlabelled:
        raise RuntimeError(
            f"{len(unlabelled)} corpus units carry no file_name/source. File-level "
            "ground truth matches on those fields, so such a unit can never be "
            "counted as a hit."
        )


def load_corpus(backend_dir: Path, shape: str = "chunks") -> List[Document]:
    """
    The documents the retriever ranks: the units ingestion writes.

    WHAT CHANGED, AND WHY
    ---------------------
    This used to return one Document per FILE, and the dense leg windowed those
    files. That measured a retrieval path production does not have. Ingestion writes
    one chunk per symbol, capped at `MAX_CHUNK_CHARS`, and the two-branch retriever
    ranks those chunks. File units here are 7,021 characters at the median and up to
    49,937 — a single "document" can be 62 embed windows collapsing back to one
    parent — while production's largest unit is a 3,000-character chunk with at most
    four windows.

    The gap was not cosmetic. Measured over the same 44 queries with the same code,
    changing nothing but this shape moves symbol recall from 82.9% to 66.7% on the
    BM25 leg and from 89.4% to 82.9% fused. Retrieving a file made every symbol
    inside it "present", which is why symbol recall tracked hit rate almost exactly
    at file shape (89.4% vs 81.8%) and separates from it at chunk shape.

    `shape="files"` rebuilds the old corpus: it exists so a stored baseline stays
    reproducible and so both shapings can be measured in one run. It is not what
    production indexes, and every result records which shape produced it.
    """
    if shape not in CORPUS_SHAPES:
        raise ValueError(
            f"unknown corpus shape {shape!r}; expected one of {CORPUS_SHAPES}"
        )

    files = project_source_files(backend_dir)
    units = _file_units(files) if shape == "files" else _chunk_units(files, backend_dir)
    _assert_unit_shape(units, shape)
    return units


def print_report(result: Dict[str, Any]) -> None:
    m = result["metrics"]
    ev = result.get("evaluation", {})
    granularity = m.get("hit_rate_at_k_granularity", "file")
    shape = ev.get("corpus_shape", "unknown")
    per_file = ev.get("units_per_expected_file") or {}
    dense = ev.get("dense_corpus") or {}
    source_files = ev.get("corpus_source_files")

    corpus_line = f"{dense.get('units', '?')} units ({shape})"
    if source_files:
        corpus_line += f" from {source_files} source files"
    print()
    print("=" * 60)
    print("         SAVFLUX RAG BENCHMARK REPORT")
    print("=" * 60)
    print(f"  Corpus               : {corpus_line}")
    print(f"  Total queries        : {m['total_queries']}")
    print(f"  Hit Rate @ K         : {m['hit_rate_at_k']}%"
          f"   ({granularity}-level: any chunk of the right file)")
    print(f"  Mean Reciprocal Rank : {m['mean_reciprocal_rank_mrr']}")
    span_hr = m.get("span_hit_rate_at_k")
    if span_hr is None:
        print("  Span Hit Rate @ K    : —   (no query named expected symbols)")
    else:
        print(f"  Span Hit Rate @ K    : {span_hr}%"
              f"   (unit-level: the chunk itself carries the symbol,"
              f" {m.get('span_scored_queries')}/{m['total_queries']} queries)")
    print(f"  Symbol Recall        : {m['symbol_recall_pct']}%")
    print(f"  Precision @ K        : {m['precision_at_k_pct']}%")
    answerable = m.get("answerable_queries")
    unanswerable = m.get("unanswerable_queries") or 0
    if unanswerable:
        # Kept in the headline number and called out here: excluding them would raise
        # the score of unchanged retrieval.
        rate = m.get("hit_rate_at_k_answerable")
        print(f"  ⚠️  {unanswerable} of {m['total_queries']} queries' ground truth matches no"
              f" unit in this corpus, so they cannot hit."
              + (f"  Over the {answerable} answerable ones: {rate}%." if rate is not None else ""))
    print(f"  Avg latency          : {m['avg_latency_ms']} ms/query")
    stats = m.get("latency_ms") or {}
    if stats.get("p50") is not None:
        print(f"                       p50 {stats['p50']} ms · p95 {stats['p95']} ms · "
              f"max {stats['max']} ms  (product stages only)")
    stages = m.get("stage_ms") or {}
    shown = [(name, stage) for name, stage in stages.items()
             if name != HARNESS_STAGE and stage.get("p50") is not None]
    if shown:
        total = sum(stage["p50"] for _, stage in shown) or 1.0
        print("  Where the time goes    :")
        for name, stage in shown:
            share = stage["p50"] / total * 100
            bar = "█" * max(1, int(share / 4))
            print(f"      {name:<8} {bar:<26} p50 {stage['p50']:>7} ms  ({share:.0f}%)")
    harness = (m.get("harness_ms") or {}).get("p50")
    if harness is not None:
        print(f"      (scoring the legs adds {harness} ms/query, not counted above)")
    embed_ms = m.get("embed_index_ms")
    embed_rate = m.get("embed_units_per_second")
    if embed_ms is not None:
        rate = f" · {embed_rate:,.0f} units/s" if embed_rate else ""
        print(f"  Dense index build      : {embed_ms:,.0f} ms for "
              f"{_dense_windows(result):,} windows{rate} — what a heavier embedder "
              "costs once per re-index, which per-query latency cannot show")
    if per_file:
        print(f"  Units per expected file: min {per_file['min']}  "
              f"median {per_file['median']}  p90 {per_file['p90']}  "
              f"max {per_file['max']}")
        print(f"    a {granularity}-level hit means ONE of those units landed in top-K")
        if per_file.get("queries_with_no_unit"):
            print(f"    {per_file['queries_with_no_unit']} queries' expected file has "
                  "NO unit in the corpus — those cannot hit")
    print("=" * 60)

    by_leg = m.get("by_leg") or {}
    if by_leg:
        # The table that decides whether an embedder swap is worth a re-index.
        print("  BY RETRIEVAL LEG           hit@K      MRR    span@K   symbols"
              "   (hit@K is file-level)")
        for name in ("bm25_only", "dense_only", "fused", "fused_reranked"):
            leg = by_leg.get(name)
            if not leg:
                continue
            hit = leg.get("hit_rate_at_k")
            if hit is None:
                print(f"    {name:<22s} {'—':>6s} {'—':>8s} {'—':>8s}   {leg.get('note', '')}")
            else:
                span = leg.get("span_hit_rate_at_k")
                print(
                    f"    {name:<22s} {hit:>5.1f}%  {leg['mean_reciprocal_rank_mrr']:>6.3f}"
                    f"  {f'{span:>6.1f}%' if span is not None else '     —'}"
                    f"  {leg['symbol_recall_pct']:>5.1f}%"
                )
        print("=" * 60)

    # Which models produced these numbers. Printed before the grade, not after:
    # a hit rate with no model attached to it is not a result.
    models = result.get("models", {})
    if models:
        print("  MODELS")
        for key in sorted(models):
            print(f"    {key:<20s}: {models[key]}")
        print("=" * 60)
    # Grade bands
    hr = m["hit_rate_at_k"]
    if hr >= 80:
        grade = "🟢 EXCELLENT"
    elif hr >= 60:
        grade = "🟡 GOOD"
    elif hr >= 40:
        grade = "🟠 FAIR"
    else:
        grade = "🔴 NEEDS WORK"
    print(f"  Overall grade        : {grade} ({hr}% hit rate)")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SavFlux RAG Benchmark")
    parser.add_argument(
        "--corpus-dir",
        help=(
            "Directory to build the corpus from (default: this repo's backend/). "
            "The corpus is this project's own source, so it changes whenever the "
            "code does — which makes runs from different commits incomparable. "
            "Point this at a pinned checkout to hold the document set still while "
            "comparing one thing: e.g. a stash of the previous commit, or a second "
            "checkout. Both runs record evaluation.corpus_sha256, so whether two "
            "runs are comparable is checkable rather than assumed."
        ),
    )
    parser.add_argument(
        "--eval-file", help="Path to external JSON benchmark file"
    )
    parser.add_argument("--top-k", type=int, default=5, help="Top-K candidates to evaluate")
    parser.add_argument(
        "--embedding-model",
        default=None,
        metavar="NAME",
        help=(
            "Pin a LOCAL embedding model by name or path for the dense leg (e.g. "
            "sentence-transformers/all-MiniLM-L6-v2) instead of the configured "
            "EMBEDDING_MODEL, so two embedder baselines can be compared on one corpus. "
            "The weights must already be cached (HF_HOME or an Ollama model): nothing is "
            "downloaded at score time and a paid embedding API is never called, whatever "
            "LLM_PROVIDER says. Index and query both use this model, so a Chroma index "
            "built with a different one has to be rebuilt."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Only print JSON result (for CI)")
    parser.add_argument("--json-out", help="Write JSON result to this file path")
    parser.add_argument(
        "--embedder",
        choices=("model", "offline"),
        default="model",
        help=(
            "Which embedder drives the dense leg. 'model' = the configured "
            "EMBEDDING_MODEL, i.e. production (needs weights). 'offline' = a "
            "deterministic hashing embedder needing no download, for CI plumbing "
            "checks only — its numbers are not a measure of retrieval quality."
        ),
    )
    parser.add_argument(
        "--corpus-shape",
        choices=CORPUS_SHAPES,
        default="chunks",
        help=(
            "What one retrieval unit is. 'chunks' (default) is what ingestion "
            "writes and what the retriever ranks: one chunk per symbol. 'files' "
            "is the shape this harness used before — one unit per whole file, up "
            "to thousands of characters and dozens of embed windows — kept only so "
            "an older baseline can be reproduced and the two compared in one run."
        ),
    )
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help=(
            "Skip cross-encoder reranking. Use when the model is not available: "
            "without this, each query retries a cross-encoder download with "
            "exponential backoff before the failure path disables it, which turns a "
            "few-second run into several minutes and corrupts the latency metric."
        ),
    )
    parser.add_argument(
        "--compare",
        metavar="BASELINE.json",
        help=(
            "Print the per-stage and per-metric delta against a previous run's "
            "--json-out file. A total alone cannot tell a reranker regression from a "
            "query-expansion one, which is the difference between two fixes."
        ),
    )
    parser.add_argument(
        "--fail-on-regression",
        type=float,
        metavar="PCT",
        help=(
            "Exit 1 when p50 or p95 latency grew by more than PCT percent versus "
            "--compare. Off by default: these numbers come from one machine, and a "
            "laptop with a compiler running will exceed any fixed threshold. Treat a "
            "threshold here as a smoke gate, not as a benchmark."
        ),
    )
    args = parser.parse_args()

    # Load dataset
    if args.eval_file:
        dataset = json.loads(Path(args.eval_file).read_text())
    else:
        dataset = BENCHMARK_DATASET

    # Build corpus from backend source, or from a pinned checkout when asked.
    backend_dir = resolve_corpus_dir(args.corpus_dir, Path(__file__).resolve().parent)
    if not backend_dir.is_dir():
        raise SystemExit(f"--corpus-dir is not a directory: {backend_dir}")
    source_files = project_source_files(backend_dir)
    corpus = load_corpus(backend_dir, shape=args.corpus_shape)

    if not args.quiet:
        print(f"\n🔬 SavFlux RAG Benchmark")
        print(f"   Corpus: {len(corpus)} units ({args.corpus_shape})"
              f" from {len(source_files)} source files in {backend_dir}")
        print(f"   Corpus sha256: {corpus_fingerprint(corpus)[:16]}")
        if args.corpus_shape == "files":
            print("   ⚠️  --corpus-shape files: not the unit production indexes;")
            print("      file-level numbers from this shape read higher. See STRATEGY.md.\n")
        print(f"   Queries: {len(dataset)}  |  top_k={args.top_k}")
        print()

    if not args.quiet and args.embedder == "offline":
        print("   ⚠️  --embedder offline: dense-leg numbers come from a deterministic")
        print("      hashing embedder. They prove the plumbing works and say nothing")
        print("      about retrieval quality. Use --embedder model for that.\n")

    result = asyncio.run(evaluate_pipeline(
        dataset=dataset,
        corpus_docs=corpus,
        top_k=args.top_k,
        verbose=not args.quiet,
        embedder_mode=args.embedder,
        embedder_pin=args.embedding_model,
        rerank_enabled=not args.no_rerank,
        corpus_shape=args.corpus_shape,
        corpus_files=len(source_files),
    ))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))

    rows = None
    if args.compare:
        baseline_path = Path(args.compare)
        if not baseline_path.is_file():
            raise SystemExit(f"--compare: no such file: {baseline_path}")
        rows = compare_results(json.loads(baseline_path.read_text()), result)
        print(format_comparison(rows))

    if args.fail_on_regression is not None:
        if not rows:
            raise SystemExit("--fail-on-regression needs --compare to diff against")
        threshold = args.fail_on_regression
        offenders = [
            row for row in rows
            if row["metric"] in LATENCY_METRICS
            and row["pct"] is not None
            and row["pct"] > threshold
        ]
        if offenders:
            for row in offenders:
                print(f"  ⚠️  {row['metric']}: {row['pct']:+.1f}% (limit {threshold}%)")
            raise SystemExit(1)

    if args.quiet:
        print(json.dumps(result))
    else:
        print_report(result)
