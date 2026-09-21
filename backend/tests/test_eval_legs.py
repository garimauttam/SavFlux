"""
test_eval_legs.py — The benchmark must measure the pipeline production runs.

WHY THIS FILE EXISTS
--------------------
`eval_rag.py` is a CI gate whose output decides whether retrieval changed. It had no
tests, and three defects that all pointed the same way — it was not measuring what it
claimed:

1. **No dense branch at all.** It built a `BM25Index` and never loaded an embedder, so
   `EMBEDDING_MODEL` could not move any number it printed.
2. **A different fusion algorithm.** It used flat `reciprocal_rank_fusion` over BM25
   lists; production uses weighted `two_branch_rrf`. Those are not the same function,
   and `two_branch_rrf`'s own docstring explains why the flat one is wrong for one
   dense list plus N lexical ones.
3. **A corpus polluted by the virtualenv.** `rglob("*.py")` over `backend/` matched
   19,501 files because it descended into `.venv/`. That made the run 100x slower than
   necessary and, worse, made the corpus depend on which packages happened to be
   installed — so a number from CI and a number from a laptop were not comparable.

These tests pin the fixed behaviour. They run against the offline embedder, so they
need no weights and no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from langchain_core.documents import Document

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def eval_rag_module():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag

    return eval_rag


# ── 1. The corpus is this project, not this machine ──────────────────────────


def test_the_corpus_excludes_installed_packages(eval_rag_module):
    """
    The defect that made the benchmark slow and machine-dependent.

    `backend/.venv/lib/python3.11/site-packages/` contains thousands of `.py` files
    that have nothing to do with this project. Including them meant the same query
    was scored against a different document set on every machine.
    """
    corpus = eval_rag_module.load_corpus(REPO_ROOT / "backend")
    sources = [d.metadata["source"] for d in corpus]

    assert corpus, "the corpus is empty"
    for marker in ("site-packages", "/.venv/", "/venv/", "node_modules", "__pycache__"):
        leaked = [s for s in sources if marker in s]
        assert not leaked, f"{len(leaked)} corpus documents leaked from {marker}: {leaked[:3]}"

    assert len(corpus) < 1000, (
        f"{len(corpus)} documents is far more than this project has — the exclusion "
        "list has stopped working"
    )


def test_the_corpus_still_contains_the_projects_own_source(eval_rag_module):
    """
    NEGATIVE CONTROL: excluding too much is also a failure.

    A corpus that excluded everything would pass the test above and measure nothing.
    """
    corpus = eval_rag_module.load_corpus(REPO_ROOT / "backend")
    names = {d.metadata["file_name"] for d in corpus}

    assert "llm_factory.py" in names
    assert "hybrid_retriever.py" in names
    assert "config.py" in names


def test_corpus_loading_is_deterministic(eval_rag_module):
    """Sorted order, so a diff between two runs is a real change."""
    first = [d.metadata["source"] for d in eval_rag_module.load_corpus(REPO_ROOT / "backend")]
    second = [d.metadata["source"] for d in eval_rag_module.load_corpus(REPO_ROOT / "backend")]
    assert first == second
    assert first == sorted(first)


# ── 2. The dense leg runs, and it is the configured embedder driving it ──────


def test_the_offline_embedder_mode_needs_no_model(monkeypatch):
    """
    `--embedder offline` must not touch configuration or the network.

    This is what lets CI exercise the dense branch at all.
    """
    import eval_rag

    from app.services.offline_embedder import OfflineEmbedder

    assert isinstance(eval_rag._build_embedder("offline"), OfflineEmbedder)


def test_the_model_mode_raises_rather_than_falling_back(monkeypatch):
    """
    A silent fallback to the lexical embedder would produce a quality number that
    came from a stand-in — worse than failing, because it looks like evidence.
    """
    import eval_rag

    from app.services import llm_factory

    def _cannot_load():
        raise OSError("no weights and no network")

    monkeypatch.setattr(llm_factory, "get_embedding_fn", _cannot_load)
    with pytest.raises(RuntimeError) as exc:
        eval_rag._build_embedder("model")
    assert "offline" in str(exc.value), "the error must name the way out"


def test_an_unknown_embedder_mode_is_rejected(eval_rag_module):
    with pytest.raises(ValueError):
        eval_rag_module._build_embedder("magic")


# ── 3. Every leg is scored, and from the right fusion ────────────────────────


def _tiny_corpus():
    return [
        Document(
            page_content="def verify_token(token):\n    return jwt.decode(token, SECRET)\n",
            metadata={"source": "/x/auth.py", "file_name": "auth.py"},
        ),
        Document(
            page_content="const styles = { color: 'red', padding: 4 };\n",
            metadata={"source": "/x/ui.tsx", "file_name": "ui.tsx"},
        ),
    ]


def _tiny_dataset():
    return [
        {
            "query": "verify_token jwt decode",
            "ground_truth_file": "auth.py",
            "expected_symbols": ["verify_token"],
        }
    ]


@pytest.mark.asyncio
async def test_every_leg_is_reported(eval_rag_module):
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_tiny_dataset(),
        corpus_docs=_tiny_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    by_leg = result["metrics"]["by_leg"]
    assert set(by_leg) == {"bm25_only", "dense_only", "fused", "fused_reranked"}


@pytest.mark.asyncio
async def test_the_fused_leg_uses_production_fusion(eval_rag_module, monkeypatch):
    """
    `fused` must be weighted two_branch_rrf, not flat reciprocal_rank_fusion.

    Flat RRF over one dense list plus N lexical ones gives the lexical branch N times
    the weight — three query variants means 3x. That is the exact failure
    `two_branch_rrf` was written to prevent, and the benchmark used to commit it.
    """
    import eval_rag

    calls: list[str] = []
    real_two_branch = eval_rag.two_branch_rrf
    real_flat = eval_rag.reciprocal_rank_fusion

    def spy_two_branch(*args, **kwargs):
        calls.append("two_branch_rrf")
        return real_two_branch(*args, **kwargs)

    def spy_flat(lists, *args, **kwargs):
        calls.append("reciprocal_rank_fusion")
        return real_flat(lists, *args, **kwargs)

    monkeypatch.setattr(eval_rag, "two_branch_rrf", spy_two_branch)
    monkeypatch.setattr(eval_rag, "reciprocal_rank_fusion", spy_flat)

    await eval_rag.evaluate_pipeline(
        dataset=_tiny_dataset(),
        corpus_docs=_tiny_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )

    assert "two_branch_rrf" in calls, "the fused leg is not using production fusion"


@pytest.mark.asyncio
async def test_a_leg_that_did_not_run_is_reported_as_missing_not_as_a_number(eval_rag_module):
    """
    The integrity rule: a leg that did not execute must not appear as a result.

    The reranker returns its input unchanged when the cross-encoder cannot be loaded,
    so `fused_reranked` would silently equal `fused`. Recording that as a real number
    would make an unloaded model look like a measured one.
    """
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_tiny_dataset(),
        corpus_docs=_tiny_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    reranked = result["metrics"]["by_leg"]["fused_reranked"]
    assert reranked["hit_rate_at_k"] is None
    assert "disabled" in reranked["note"]


@pytest.mark.asyncio
async def test_the_dense_leg_actually_changes_the_outcome(eval_rag_module):
    """
    Proof the dense branch is wired in rather than decorative.

    A corpus where lexical scoring cannot find the target but dense similarity can
    must show `dense_only` succeeding on that query. If the dense leg were a stub
    returning nothing, this fails.
    """
    corpus = [
        Document(
            page_content="rate limiting throttles requests per window",
            metadata={"source": "/x/limiter.py", "file_name": "limiter.py"},
        ),
        Document(
            page_content="colour palette and border radius tokens",
            metadata={"source": "/x/theme.css", "file_name": "theme.css"},
        ),
    ]
    dataset = [
        {
            "query": "limiter.py rate limiting",
            "ground_truth_file": "limiter.py",
            "expected_symbols": [],
        }
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=dataset,
        corpus_docs=corpus,
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    assert result["metrics"]["by_leg"]["dense_only"]["hit_rate_at_k"] == 100.0


# ── 4. Provenance records which embedder produced the numbers ────────────────


@pytest.mark.asyncio
async def test_the_result_says_the_dense_leg_was_not_a_quality_model(eval_rag_module):
    """
    An offline run and a real run must be distinguishable in the artefact. They are
    otherwise the same shape of JSON, and the offline one is not a quality result.
    """
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_tiny_dataset(),
        corpus_docs=_tiny_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    label = result["metrics"]["dense_leg_embedder"]
    assert "offline" in label.lower()
    assert "not a quality model" in label.lower(), label
    assert result["models"]["dense_embedder_mode"] == "offline"
