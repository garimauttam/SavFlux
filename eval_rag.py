"""
eval_rag.py — Automated RAG Evaluation & Benchmarking Suite.

Measures core retrieval and response generation metrics without needing expensive paid API tiers:
1. Context Hit Rate @ K: Was the ground-truth target file/chunk retrieved in top-K candidates?
2. Mean Reciprocal Rank (MRR): What was the reciprocal rank (1/rank) of the ground truth?
3. Retrieval Precision @ K: Ratio of retrieved documents relevant to the query.
4. Answer Groundedness / Faithfulness (LLM-as-a-judge / Rule-based check).
5. Symbol Coverage: Did retrieved chunks contain the expected function/class names?

Usage:
  python eval_rag.py                     # runs full benchmark against self (SavFlux backend code)
  python eval_rag.py --eval-file file.json  # load external JSON benchmark dataset
  python eval_rag.py --top-k 10 --quiet    # machine-readable JSON output

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
import os
import sys
import time
from pathlib import Path

# Add backend directory to sys.path for direct script execution
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))

from typing import List, Dict, Any, Optional
from langchain_core.documents import Document
from app.services.hybrid_retriever import (
    BM25Index,
    reciprocal_rank_fusion,
    two_branch_rrf,
)
from app.services.reranker import RERANK_SCORE_KEY, rerank
from app.services.query_enhancer import extract_file_scope, local_query_variants
from app.services.offline_embedder import DenseIndex


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

def _build_embedder(mode: str):
    """
    Return the embedder the dense leg will use.

    `offline` is the deterministic hashing embedder: no download, no network, and
    therefore runnable in CI. Its similarities are lexical, so numbers from it prove
    the dense path *works* and say nothing about retrieval quality.

    `model` is whatever `EMBEDDING_MODEL` names, i.e. what production uses. It needs
    the weights available locally. There is deliberately NO silent fallback to the
    offline embedder: a quality number that quietly came from a lexical stand-in is
    worse than a failure, because it looks like evidence.
    """
    if mode == "offline":
        from app.services.offline_embedder import OfflineEmbedder

        return OfflineEmbedder()

    if mode != "model":
        raise ValueError(f"unknown embedder mode {mode!r}; expected 'offline' or 'model'")

    from app.services.llm_factory import get_embedding_fn

    try:
        return get_embedding_fn()
    except Exception as exc:  # noqa: BLE001 - any load failure is actionable here
        raise RuntimeError(
            f"Could not load the configured embedding model: {type(exc).__name__}: {exc}.\n"
            "Run with --embedder offline for a plumbing-only check that needs no "
            "weights, or install the model (needs access to huggingface.co)."
        ) from exc


def _rank_of(ranked_docs: List[Document], target_file: str) -> Optional[int]:
    """1-based position of the first document from `target_file`, else None."""
    needle = target_file.lower()
    for idx, doc in enumerate(ranked_docs, start=1):
        if needle in doc.metadata.get("file_name", "").lower():
            return idx
        if needle in doc.metadata.get("source", "").lower():
            return idx
    return None


def _empty_leg() -> Dict[str, Any]:
    return {"hits": 0, "ranks": [], "symbol_hits": 0, "symbols_expected": 0}


async def evaluate_pipeline(
    dataset: List[Dict[str, Any]],
    corpus_docs: List[Document],
    top_k: int = 5,
    verbose: bool = True,
    embedder_mode: str = "model",
    rerank_enabled: bool = True,
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
    dense = DenseIndex(corpus_docs, _build_embedder(embedder_mode))

    legs: Dict[str, Dict[str, Any]] = {
        "bm25_only": _empty_leg(),
        "dense_only": _empty_leg(),
        "fused": _empty_leg(),
        "fused_reranked": _empty_leg(),
    }
    rerank_observed = False

    hits_at_k = 0
    reciprocal_ranks: List[float] = []
    symbol_hits = 0
    total_expected_symbols = 0
    latencies: List[float] = []
    precision_values: List[float] = []

    query_results = []

    for item in dataset:
        raw_query = item["query"]
        target_file = item["ground_truth_file"]
        expected_syms = item.get("expected_symbols", [])

        query, file_scope = extract_file_scope(raw_query)

        t0 = time.monotonic()

        # Same cheap query-variant strategy production uses, on both branches.
        variants = list(local_query_variants(query))
        bm25_lists = [
            bm25.search(query=variant, top_k=top_k * 3, file_filter=file_scope)
            for variant in variants
        ]
        dense_lists = [dense.search(query=variant, top_k=top_k * 3) for variant in variants]

        # ── Each leg, scored independently ───────────────────────────────────
        leg_rankings = {
            "bm25_only": reciprocal_rank_fusion(bm25_lists, top_n=top_k),
            "dense_only": reciprocal_rank_fusion(dense_lists, top_n=top_k),
            # Production's fusion: weighted branches, not flat RRF over both lists,
            # which would give BM25 N× the weight for N query variants.
            "fused": two_branch_rrf(dense_lists, bm25_lists, top_n=top_k * 3),
        }

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

        for leg_name, ranking in leg_rankings.items():
            leg = legs[leg_name]
            leg_rank = _rank_of(ranking, target_file)
            if leg_rank is not None:
                leg["hits"] += 1
                leg["ranks"].append(1.0 / leg_rank)
            else:
                leg["ranks"].append(0.0)

            leg_text = " ".join(d.page_content for d in ranking)
            leg["symbol_hits"] += sum(1 for s in expected_syms if s in leg_text)
            leg["symbols_expected"] += len(expected_syms)

        # The headline metrics follow production: fused, then reranked.
        ranked_docs = leg_rankings["fused_reranked"]

        latency_ms = (time.monotonic() - t0) * 1000
        latencies.append(latency_ms)

        # ── Hit Rate & MRR ────────────────────────────────────────────────────
        rank = _rank_of(ranked_docs, target_file)

        if rank is not None:
            hits_at_k += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)

        relevant_count = sum(
            1
            for doc in ranked_docs
            if target_file.lower() in doc.metadata.get("file_name", "").lower()
            or target_file.lower() in doc.metadata.get("source", "").lower()
        )
        precision_values.append(relevant_count / len(ranked_docs) if ranked_docs else 0.0)

        # ── Symbol Coverage ───────────────────────────────────────────────────
        retrieved_text = " ".join(doc.page_content for doc in ranked_docs)
        matched_symbols = [sym for sym in expected_syms if sym in retrieved_text]
        symbol_hits += len(matched_symbols)
        total_expected_symbols += len(expected_syms)

        result = {
            "query": raw_query,
            "target_file": target_file,
            "rank": rank,
            "hit": rank is not None,
            "symbols_matched": len(matched_symbols),
            "symbols_expected": len(expected_syms),
            "latency_ms": round(latency_ms, 1),
        }
        query_results.append(result)

        if verbose:
            status = f"✅ Rank #{rank}" if rank is not None else "❌ Miss"
            syms = f"{len(matched_symbols)}/{len(expected_syms)} symbols"
            print(f"  [{status}] {raw_query[:60]:<60} → {target_file} ({syms})")

    total_queries = len(dataset)
    hit_rate     = hits_at_k / total_queries if total_queries else 0.0
    mrr          = sum(reciprocal_ranks) / total_queries if total_queries else 0.0
    sym_recall   = symbol_hits / total_expected_symbols if total_expected_symbols else 0.0
    avg_latency  = sum(latencies) / len(latencies) if latencies else 0.0

    def _leg_metrics(leg: Dict[str, Any]) -> Dict[str, Any]:
        n = total_queries or 1
        return {
            "hit_rate_at_k": round(leg["hits"] / n * 100, 2),
            "mean_reciprocal_rank_mrr": round(sum(leg["ranks"]) / n, 3),
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
            "symbol_recall_pct": None,
            "note": "reranking disabled (--no-rerank)",
        }
    elif not rerank_observed:
        by_leg["fused_reranked"] = {
            "hit_rate_at_k": None,
            "mean_reciprocal_rank_mrr": None,
            "symbol_recall_pct": None,
            "note": "cross-encoder unavailable — reranking did not run",
        }

    result = {
        "metrics": {
            "total_queries": total_queries,
            "hit_rate_at_k": round(hit_rate * 100, 2),
            "mean_reciprocal_rank_mrr": round(mrr, 3),
            "symbol_recall_pct": round(sym_recall * 100, 2),
            "avg_latency_ms": round(avg_latency, 1),
            "precision_at_k_pct": round(
                (sum(precision_values) / len(precision_values) if precision_values else 0.0) * 100,
                2,
            ),
            "by_leg": by_leg,
            "dense_leg_embedder": (
                "offline-hashing (NOT a quality model)"
                if embedder_mode == "offline"
                else "configured model"
            ),
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
    }
    result["models"] = _model_provenance(embedder_mode)
    return result


def _model_provenance(embedder_mode: str = "model") -> Dict[str, str]:
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

    # The reranker is a module constant, not a setting — read it, don't guess it.
    try:
        from app.services.reranker import RERANKER_MODEL

        provenance["reranker"] = RERANKER_MODEL
    except Exception as exc:  # pragma: no cover
        provenance["reranker"] = f"<unavailable: {type(exc).__name__}>"

    return provenance


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


def load_corpus(backend_dir: Path) -> List[Document]:
    """Load this project's own Python source as the evaluation corpus."""
    docs = []
    for p in sorted(backend_dir.rglob("*.py")):
        if not _is_project_source(p, backend_dir):
            continue
        try:
            content = p.read_text(encoding="utf-8")
            docs.append(Document(
                page_content=content,
                metadata={"source": str(p), "file_name": p.name},
            ))
        except Exception:
            pass
    return docs


def print_report(result: Dict[str, Any]) -> None:
    m = result["metrics"]
    print()
    print("=" * 60)
    print("         SAVFLUX RAG BENCHMARK REPORT")
    print("=" * 60)
    print(f"  Total queries        : {m['total_queries']}")
    print(f"  Hit Rate @ K         : {m['hit_rate_at_k']}%")
    print(f"  Mean Reciprocal Rank : {m['mean_reciprocal_rank_mrr']}")
    print(f"  Symbol Recall        : {m['symbol_recall_pct']}%")
    print(f"  Precision @ K        : {m['precision_at_k_pct']}%")
    print(f"  Avg latency          : {m['avg_latency_ms']} ms/query")
    print("=" * 60)

    by_leg = m.get("by_leg") or {}
    if by_leg:
        # The table that decides whether an embedder swap is worth a re-index.
        print("  BY RETRIEVAL LEG           hit@K      MRR   symbols")
        for name in ("bm25_only", "dense_only", "fused", "fused_reranked"):
            leg = by_leg.get(name)
            if not leg:
                continue
            hit = leg.get("hit_rate_at_k")
            if hit is None:
                print(f"    {name:<22s} {'—':>6s} {'—':>8s}   {leg.get('note', '')}")
            else:
                print(
                    f"    {name:<22s} {hit:>5.1f}%  {leg['mean_reciprocal_rank_mrr']:>6.3f}"
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
    parser.add_argument("--eval-file", help="Path to external JSON benchmark file")
    parser.add_argument("--top-k", type=int, default=5, help="Top-K candidates to evaluate")
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
        "--no-rerank",
        action="store_true",
        help=(
            "Skip cross-encoder reranking. Use when the model is not available: "
            "without this, each query retries a cross-encoder download with "
            "exponential backoff before the failure path disables it, which turns a "
            "few-second run into several minutes and corrupts the latency metric."
        ),
    )
    args = parser.parse_args()

    # Load dataset
    if args.eval_file:
        dataset = json.loads(Path(args.eval_file).read_text())
    else:
        dataset = BENCHMARK_DATASET

    # Build corpus from backend source
    backend_dir = Path(__file__).resolve().parent / "backend"
    corpus = load_corpus(backend_dir)

    if not args.quiet:
        print(f"\n🔬 SavFlux RAG Benchmark")
        print(f"   Corpus: {len(corpus)} files from backend/")
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
        rerank_enabled=not args.no_rerank,
    ))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))

    if args.quiet:
        print(json.dumps(result))
    else:
        print_report(result)
