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

    # The bound is relative to the source files, not absolute, because the corpus is
    # now measured in CHUNKS: one file legitimately yields many units, so a fixed
    # ceiling would measure the file size distribution rather than the exclusion list.
    # The defect this catches is a venv leak, which multiplies both together.
    files = eval_rag_module.project_source_files(REPO_ROOT / "backend")
    assert files, "no source files found at all"
    assert len(corpus) < 50 * len(files), (
        f"{len(corpus)} units from {len(files)} source files is far more than this "
        "project produces — the exclusion list has stopped working"
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


# ── 5. The dense leg indexes windows, and collapses them to parents ──────────
#
# The dense branch used to embed whole files, which reproduced the very defect the
# pipeline was being fixed for: the embedder reads 256 tokens, so a file's tail was
# never in vector space and no query about it could be answered. A harness that
# measures a path production does not have cannot tell a fixed pipeline from a
# broken one.

# Long enough to need several 900-character windows.
WINDOW_FILLER = "def helper(value):\n    return value + 1\n" * 40
TAIL_FACT = "\ndef rotate_credentials(token):\n    return token[::-1]\n"


def _long_file():
    return Document(
        page_content=WINDOW_FILLER + TAIL_FACT,
        metadata={"source": "/x/big.py", "file_name": "big.py"},
    )


def test_the_dense_corpus_is_windowed_not_whole_files(eval_rag_module):
    from app.services.parent_child import EMBED_WINDOW_CHARS

    docs = eval_rag_module._dense_corpus([_long_file()])

    assert len(docs) > 1, "a file longer than the window must split"
    assert all(len(d.page_content) <= EMBED_WINDOW_CHARS for d in docs)


def test_the_dense_leg_can_reach_a_fact_past_the_embed_window(eval_rag_module):
    """
    The payoff, with the counterfactual measured rather than asserted.

    Embedding the whole file puts nothing past the window into its vector, so the
    tail fact is unreachable. Windowing makes it reachable. Both numbers are
    computed here rather than described, so this cannot pass by accident.
    """
    from app.services.offline_embedder import DenseIndex, OfflineEmbedder

    doc = _long_file()
    embedder = OfflineEmbedder()
    query = "rotate_credentials"

    whole_file_score = DenseIndex([doc], embedder).scores(query, top_k=1)[0][1]
    window_scores = DenseIndex(
        eval_rag_module._dense_corpus([doc]), embedder
    ).scores(query, top_k=1)

    # The hashing embedder's numbers are lexical, so this is a coverage claim, not
    # a quality claim: the tokens simply are or are not inside the embedded text.
    assert whole_file_score < 0.05, (
        "the whole-file vector unexpectedly contains the tail; the fixture no "
        "longer exceeds the embed window and this test proves nothing"
    )
    assert window_scores[0][1] > whole_file_score


@pytest.mark.asyncio
async def test_a_tail_fact_is_retrievable_end_to_end(eval_rag_module):
    dataset = [
        {
            "query": "rotate_credentials token",
            "ground_truth_file": "big.py",
            "expected_symbols": ["rotate_credentials"],
        }
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=dataset,
        corpus_docs=[_long_file()],
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    by_leg = result["metrics"]["by_leg"]
    assert by_leg["dense_only"]["hit_rate_at_k"] == 100.0
    # Symbol recall must count what the MODEL would see — the whole file — not the
    # window that happened to match. Scoring the window would report 0 here and
    # read as a retrieval failure.
    assert by_leg["dense_only"]["symbol_recall_pct"] == 100.0


@pytest.mark.asyncio
async def test_dense_candidates_are_collapsed_to_one_entry_per_file(
    eval_rag_module, monkeypatch
):
    """
    No file may appear twice in the dense branch's candidate list.

    Without the collapse, several windows of one file fill the top-k and the leg
    is scored on "one parent wearing five hats" — a depth artefact that would look
    like the windowing improved ranking.
    """
    recorded = []
    real_fusion = eval_rag_module.reciprocal_rank_fusion

    def spy(lists, **kwargs):
        recorded.append([list(docs) for docs in lists])
        return real_fusion(lists, **kwargs)

    monkeypatch.setattr(eval_rag_module, "reciprocal_rank_fusion", spy)
    corpus = [_long_file(), Document(
        page_content="unrelated decorative styling rules",
        metadata={"source": "/x/ui.tsx", "file_name": "ui.tsx"},
    )]
    dataset = [{
        "query": "rotate_credentials",
        "ground_truth_file": "big.py",
        "expected_symbols": [],
    }]

    await eval_rag_module.evaluate_pipeline(
        dataset=dataset,
        corpus_docs=corpus,
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )

    assert recorded, "fusion was never called; the spy is in the wrong place"

    from app.services.parent_child import parent_id_of

    # The dense branch's lists are the ones made of windows.
    dense_lists = [
        docs
        for call in recorded
        for docs in call
        if any("pc_parent_id" in d.metadata for d in docs)
    ]
    assert dense_lists, "no windowed list reached fusion; the dense leg is unwired"

    for docs in dense_lists:
        parents = [parent_id_of(d.metadata) for d in docs]
        assert len(parents) == len(set(parents)), (
            f"several windows of one file reached fusion: {parents}"
        )
        assert len(docs) <= 2 * 3, "candidate depth exceeded the requested top_k*3"


@pytest.mark.asyncio
async def test_the_result_records_that_the_dense_corpus_was_windowed(eval_rag_module):
    """
    Runs made before the dense leg indexed windows are not comparable with runs
    made after. The artefact has to say which kind it is.
    """
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[{
            "query": "rotate_credentials",
            "ground_truth_file": "big.py",
            "expected_symbols": [],
        }],
        corpus_docs=[_long_file()],
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    evaluation = result["evaluation"]
    dense_corpus = evaluation["dense_corpus"]
    # `units`, not `files`: this counts retrieval units, which are chunks now. The
    # old key read as a file count and would have kept reading plausibly.
    assert dense_corpus["units"] == 1
    assert dense_corpus["windows"] > 1
    assert evaluation["corpus_shape"] == "chunks"


# The symbol must live in the parent but NOT in the window that gets retrieved,
# or the two are indistinguishable and the test cannot fail.

HEAD_FACT = "def parse_manifest(path):\n    return json.loads(path.read_text())\n"


@pytest.mark.asyncio
async def test_symbol_recall_scores_the_parent_not_the_matched_window(eval_rag_module):
    """
    Guard-the-guard first: this only discriminates if the retrieved window lacks
    the symbol.

    A previous version of this test used a query matching the tail fact, which put
    the symbol inside the window that ranked first — so scoring window text found
    it anyway and the mutation survived. Here the query matches the HEAD, so the
    best window is the head one and the expected symbol sits past the window
    boundary. Only parent text can find it.
    """
    from app.services.parent_child import EMBED_WINDOW_CHARS

    doc = Document(
        page_content=HEAD_FACT + WINDOW_FILLER + TAIL_FACT,
        metadata={"source": "/x/big.py", "file_name": "big.py"},
    )
    dataset = [{
        "query": "parse_manifest",
        "ground_truth_file": "big.py",
        "expected_symbols": ["rotate_credentials"],
    }]

    result = await eval_rag_module.evaluate_pipeline(
        dataset=dataset,
        corpus_docs=[doc],
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )

    # The window the dense leg retrieves really does lack the symbol — otherwise
    # the assertion below would hold for the wrong reason.
    from app.services.offline_embedder import DenseIndex, OfflineEmbedder

    best_window = DenseIndex(
        eval_rag_module._dense_corpus([doc]), OfflineEmbedder()
    ).search("parse_manifest", top_k=1)[0]
    assert "rotate_credentials" not in best_window.page_content
    assert len(best_window.page_content) <= EMBED_WINDOW_CHARS

    assert result["metrics"]["by_leg"]["dense_only"]["symbol_recall_pct"] == 100.0
