"""
test_eval_ground_truth.py — the benchmark's answer key must name one file, and answerability
is part of the instrument.

WHY THIS FILE EXISTS
--------------------
`eval_rag.py` decided "was this retrieved correctly" with `ground_truth in unit.file_name`,
a substring test over a corpus that includes `backend/tests/`. Measured on the 44-query
dataset at this commit, four of its seventeen ground truths matched several files at once:
`review.py` accepted `tests/test_batch_review.py`, `config.py` accepted
`tests/test_provider_config.py`, `ingest.py` accepted `tests/test_delta_ingest.py`, and
`review_agent.py` accepted `app/services/multi_review_agent.py` — a different module with a
different job. 11 of 44 queries could therefore be scored a hit by a document that answers
nothing about them.

That is invisible in the metric itself: a hit rate inflated by a loose answer key looks
exactly like a hit rate earned. So these tests pin the rule, and pin that the rule's
*consequences* are reported rather than folded into the score — an unanswerable query stays
in the denominator and is named, and a run whose answer key is ambiguous does not produce
numbers at all.
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


def _unit(source: str, content: str, index: int = 0) -> Document:
    """
    A corpus unit, shaped like the ones ingestion writes.

    `chunk_index` is not decoration: the dense branch windows each unit and
    `dedupe_to_parents` collapses windows that share a parent id, which for units of one
    file is derived from `source` plus this index. Two units of one file without it are
    therefore *merged into one retrieval unit* — a fixture that looks like a chunked file
    and behaves like a single one, which is exactly the kind of silent flattening that
    makes a span-level test pass for the wrong reason.
    """
    return Document(
        page_content=content,
        metadata={
            "source": source,
            "file_name": Path(source).name,
            "chunk_index": index,
        },
    )


def _query(gt: str, text: str = "something", symbols=()) -> dict:
    return {"query": text, "ground_truth_file": gt, "expected_symbols": list(symbols)}


# ── 1. the matching rule ──────────────────────────────────────────────────────


def test_a_longer_file_name_is_not_the_ground_truth(eval_rag_module):
    """
    The false hit, in isolation.

    The second assertion is the one that makes this a test of the change rather than of
    the fixture: `"review.py" in "test_batch_review.py"` is simply true, which is exactly
    why the old rule could not tell the two apart.
    """
    test_file = _unit("backend/tests/test_batch_review.py", "assert review_endpoint works")
    module = _unit("backend/app/api/review.py", "validate_file_path for the review endpoint")

    assert eval_rag_module._unit_matches_file(module, "review.py")
    assert not eval_rag_module._unit_matches_file(test_file, "review.py")
    assert "review.py" in test_file.metadata["file_name"], "fixture no longer exercises the old rule"


def test_a_ground_truth_may_be_qualified_with_a_directory(eval_rag_module):
    """
    Two `review.py` files in one corpus is a fact about Python packages, not a bug —
    `app/__init__.py` and five siblings already prove it — so the answer key has to be
    able to say which one it means.
    """
    api = _unit("backend/app/api/review.py", "x")
    core = _unit("backend/app/core/review.py", "x")

    assert eval_rag_module._unit_matches_file(api, "api/review.py")
    assert not eval_rag_module._unit_matches_file(core, "api/review.py")
    # A corpus pinned one directory up (`--corpus-dir backend/app`) stamps sources one
    # level deep, so the only thing to match is the whole of it.
    assert eval_rag_module._unit_matches_file(_unit("api/review.py", "x"), "api/review.py")
    # Both still satisfy the bare name, which is what makes the bare name unusable.
    assert eval_rag_module._unit_matches_file(api, "review.py")
    assert eval_rag_module._unit_matches_file(core, "review.py")


@pytest.mark.asyncio
async def test_retrieving_the_test_file_scores_no_hit_for_the_module(eval_rag_module):
    """
    The same defect end to end, where it would have moved the headline number.

    Only the test file matches this query lexically, so the lexical leg is the one whose
    ranking is not in question: it returns that unit first, and the answer key now says
    what that is — a miss. (The headline is scored on the fused leg, where the second
    unit's presence in a 3-deep list would make the outcome about fusion rather than
    about the predicate, which is not what this test is for.)
    """
    corpus = [
        _unit("backend/tests/test_batch_review.py", "batch review fixture asserts ordering"),
        _unit("backend/app/api/review.py", "validate_file_path rejects traversal"),
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("review.py", "batch review fixture asserts ordering", ["validate_file_path"])],
        corpus_docs=corpus,
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    assert result["metrics"]["by_leg"]["bm25_only"]["hit_rate_at_k"] == 0.0
    # One unit counts as this answer key, and it is the module — the test file that was
    # actually retrieved contributes nothing, which is the change.
    assert result["query_breakdown"][0]["expected_file_units"] == 1
    # And it is a real miss, not an unanswerable query: the file is in the corpus.
    assert result["metrics"]["unanswerable_queries"] == 0


@pytest.mark.asyncio
async def test_an_ambiguous_answer_key_stops_the_run(eval_rag_module):
    """
    A ground truth matching two files is not a harder query, it is an unusable one.

    Refusing to score it is the point: an ambiguous key makes the metric mean "either of
    two files was retrieved", and that sentence cannot be compared with anything.
    """
    corpus = [
        _unit("backend/app/api/review.py", "validate_file_path rejects traversal"),
        _unit("backend/app/core/review.py", "review defaults shared by the app"),
    ]
    with pytest.raises(RuntimeError) as exc:
        await eval_rag_module.evaluate_pipeline(
            dataset=[_query("review.py", "validate_file_path")],
            corpus_docs=corpus,
            top_k=2,
            verbose=False,
            embedder_mode="offline",
            rerank_enabled=False,
        )
    message = str(exc.value)
    assert "api/review.py" in message and "core/review.py" in message, message
    assert "qualify" in message, "the error has to name the fix, not just the failure"

    # The qualified form resolves it, and exactly one file's units count.
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("api/review.py", "validate_file_path")],
        corpus_docs=corpus,
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    assert result["query_breakdown"][0]["expected_file_units"] == 1


# ── 2. answerability is reported, not quietly excluded ────────────────────────


@pytest.mark.asyncio
async def test_a_query_the_corpus_cannot_answer_is_named_and_kept(eval_rag_module):
    """
    A ground truth for a file the corpus does not contain can never hit.

    It stays in `hit_rate_at_k`'s denominator — dropping it would raise the score of
    completely unchanged retrieval, which is a flattering lie about the instrument — and
    the stricter `hit_rate_at_k_answerable` is ABSENT when nothing was answerable rather
    than reported as 0.0, because that run measured nothing at that granularity.
    """
    corpus = [_unit("backend/app/services/other.py", "unrelated service code")]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("missing_module.py", "unrelated service code", ["other"])],
        corpus_docs=corpus,
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    m = result["metrics"]
    assert m["total_queries"] == 1
    assert m["hit_rate_at_k"] == 0.0
    assert m["unanswerable_queries"] == 1
    assert m["answerable_queries"] == 0
    assert m["hit_rate_at_k_answerable"] is None
    assert result["evaluation"]["ground_truth"]["unanswerable"] == ["missing_module.py"]
    assert result["query_breakdown"][0]["answerable"] is False


# ── 3. the span-level metric scores the unit, not the file ────────────────────


@pytest.mark.asyncio
async def test_a_file_hit_on_the_wrong_chunk_is_not_a_span_hit(eval_rag_module):
    """
    The two granularities must disagree, or the stricter one is decoration.

    Chunk 1 is from the right file and says nothing about the asked-for function; chunk 2
    carries the function. File-level scoring is satisfied by chunk 1 at rank 1. Span-level
    scoring has to reach past it, and the difference shows up as MRR 1.0 versus 0.5 on the
    same ranking — which is the whole reason to have both.
    """
    corpus = [
        _unit("backend/app/services/tokens.py", "counting tokens in a prompt, nothing else", 0),
        _unit("backend/app/services/tokens.py", "def rotate_credentials(tokens): counts tokens", 1),
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("tokens.py", "counting tokens in a prompt", ["rotate_credentials"])],
        corpus_docs=corpus,
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    row = result["query_breakdown"][0]
    assert row["rank"] == 1, "the fixture no longer puts the unhelpful chunk first"
    assert row["span_rank"] == 2
    m = result["metrics"]
    assert m["hit_rate_at_k"] == 100.0 and m["mean_reciprocal_rank_mrr"] == 1.0
    assert m["span_hit_rate_at_k"] == 100.0 and m["span_mrr"] == 0.5
    # Symbol recall still counts what the model was shown: both chunks together.
    assert m["symbol_recall_pct"] == 100.0
    assert "rotate_credentials" not in corpus[0].page_content


@pytest.mark.asyncio
async def test_span_metrics_are_absent_when_nothing_could_be_localised(eval_rag_module):
    """
    A dataset with no expected symbols has no span-level answer to check.

    Reporting 0.0% there would read as "retrieval failed to localise" for queries that
    never asked to localise anything — the same error as reporting an unmeasured pipeline
    stage as 0 ms.
    """
    corpus = [_unit("backend/app/services/tokens.py", "counting tokens in a prompt")]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("tokens.py", "counting tokens in a prompt")],
        corpus_docs=corpus,
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    m = result["metrics"]
    assert m["hit_rate_at_k"] == 100.0, "the file-level metric still ran"
    assert m["span_hit_rate_at_k"] is None
    assert m["span_mrr"] is None
    assert m["span_precision_at_k_pct"] is None
    assert m["span_scored_queries"] == 0
    assert result["query_breakdown"][0]["span_rank"] is None
    for leg in m["by_leg"].values():
        assert leg.get("span_hit_rate_at_k", "missing") is None


@pytest.mark.asyncio
async def test_hit_rate_means_k_and_not_the_candidate_list(eval_rag_module):
    """
    `hit_rate_at_k` has to be about the K in its name.

    The fused leg is built `top_k * 3` deep on purpose — that is the candidate list
    production hands the cross-encoder — and the harness used to score a hit over all of
    it. With reranking on, `rerank` truncates to K and the defect hid; with `--no-rerank`
    (every CI and offline run) it did not. Measured on the last committed artefact: 8 of
    its 35 claimed hits sat at ranks 6..13 with top_k=5.

    Here the target is the weakest of three units in a `top_k=1` run, so it is in the
    candidate list and out of the slice. Under the old scoring this test fails by reading
    100.0.
    """
    corpus = [
        _unit("backend/app/api/noise.py", "rate limit rate limit rate limit per window"),
        _unit("backend/app/api/other.py", "rate limit requests per window"),
        _unit("backend/app/core/limiter.py", "rate limit"),
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[_query("limiter.py", "rate limit requests per window", ["rate"])],
        corpus_docs=corpus,
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    row = result["query_breakdown"][0]
    assert row["rank"] is None, "a rank past K was counted as a hit@K"
    assert row["rank_in_fused_list"] is not None and row["rank_in_fused_list"] > 1, (
        "the fixture no longer puts the target inside the candidate list but outside K, "
        "so this test cannot tell the two scorings apart"
    )
    m = result["metrics"]
    assert m["hit_rate_at_k"] == 0.0
    assert m["mean_reciprocal_rank_mrr"] == 0.0
    # The leg table too, not only the headline: `fused` is the 3K-deep list itself, so
    # it is where an uncut ranking would show up as a hit. `bm25_only`/`dense_only` are
    # already K deep by construction, and are asserted at K to keep that from quietly
    # changing if someone widens them.
    legs = m["by_leg"]
    assert legs["fused"]["hit_rate_at_k"] == 0.0
    assert legs["bm25_only"]["hit_rate_at_k"] == 0.0
    assert legs["dense_only"]["hit_rate_at_k"] == 0.0
    assert m["precision_at_k_pct"] == 0.0
    assert m["span_hit_rate_at_k"] == 0.0
    assert m["answerable_queries"] == 1, "the file IS in the corpus; it just was not in K"
    assert result["evaluation"]["metric_depth"]["rule"] == eval_rag_module.METRIC_DEPTH_RULE
    assert result["evaluation"]["metric_depth"]["top_k"] == 1


# ── 4. two runs scored under different rules are not a trend ─────────────────


def _fake_run(matching: str | None, **metrics) -> dict:
    evaluation = {} if matching is None else {"ground_truth": {"matching": matching}}
    return {"metrics": dict(metrics), "evaluation": evaluation}


def test_quality_moves_are_not_graded_across_a_matching_rule_change(eval_rag_module):
    rows = {
        row["metric"]: row["verdict"]
        for row in eval_rag_module.compare_results(
            _fake_run(None, hit_rate_at_k=79.55, symbol_recall_pct=83.74),
            _fake_run(
                eval_rag_module.GROUND_TRUTH_MATCHING,
                hit_rate_at_k=77.27,
                symbol_recall_pct=83.74,
            ),
        )
    }
    assert rows["hit_rate_at_k"].startswith("definition changed")
    assert "ground truth" in rows["hit_rate_at_k"]
    # Symbol recall is computed over the text the model was given, which the matching
    # rule does not touch — refusing to grade it too would be a guard that stops being
    # information and becomes a blanket "no data".
    assert rows["symbol_recall_pct"] == "flat"


def test_a_depth_change_blocks_every_quality_metric(eval_rag_module):
    """
    The verdict names the definition that moved, and the two rules are not equally wide.

    A matching-rule change leaves `symbol_recall_pct` gradeable (the predicate never
    touches it); a depth change does not, because cutting the scored list to K changes
    which text recall is computed over. Asserting both directions is the only way to
    keep the guard from degenerating into "block everything, or block nothing".
    """
    baseline = _fake_run(eval_rag_module.GROUND_TRUTH_MATCHING, hit_rate_at_k=70.0,
                         symbol_recall_pct=80.0)
    current = _fake_run(eval_rag_module.GROUND_TRUTH_MATCHING, hit_rate_at_k=75.0,
                        symbol_recall_pct=80.0)
    current["evaluation"]["metric_depth"] = {"rule": "something-else"}
    rows = {r["metric"]: r["verdict"] for r in eval_rag_module.compare_results(baseline, current)}
    assert rows["hit_rate_at_k"] == "definition changed: depth"
    assert rows["symbol_recall_pct"] == "definition changed: depth"


def test_two_runs_under_the_same_rule_are_graded(eval_rag_module):
    matching = eval_rag_module.GROUND_TRUTH_MATCHING
    rows = {
        row["metric"]: row["verdict"]
        for row in eval_rag_module.compare_results(
            _fake_run(matching, hit_rate_at_k=70.0, span_hit_rate_at_k=60.0),
            _fake_run(matching, hit_rate_at_k=75.0, span_hit_rate_at_k=55.0),
        )
    }
    assert rows["hit_rate_at_k"] == "better"
    assert rows["span_hit_rate_at_k"] == "WORSE"


def test_two_old_runs_are_still_comparable_with_each_other(eval_rag_module):
    """
    A missing key is one value, not an absence of one.

    Both sides predate the rule change, so they agree with each other and their
    difference IS a regression signal. Treating "unknown" as incomparable would have
    quietly disabled the comparison feature for every baseline on disk.
    """
    rows = {
        row["metric"]: row["verdict"]
        for row in eval_rag_module.compare_results(
            _fake_run(None, hit_rate_at_k=70.0), _fake_run(None, hit_rate_at_k=74.0)
        )
    }
    assert rows["hit_rate_at_k"] == "better"


# ── 5. the shipped answer key is auditable ───────────────────────────────────


def test_every_shipped_ground_truth_names_exactly_one_file(eval_rag_module):
    """
    The dataset as a whole, against the corpus the harness would actually build.

    Not a tautology: it reads `project_source_files`, the same listing `load_corpus`
    consumes, so renaming or splitting a module without updating the answer key fails
    here instead of turning a query into a permanent miss. Two queries about
    `main.py` and `conftest.py` are deliberately in the corpus root rather than under
    `app/`, and both resolve.
    """
    files = eval_rag_module.project_source_files(REPO_ROOT / "backend")
    assert files, "no corpus source files found — is this still a repo checkout?"
    pairs = [(path.name, path.as_posix()) for path in files]

    ambiguous, unresolvable = [], []
    for ground_truth in sorted({
        item["ground_truth_file"] for item in eval_rag_module.BENCHMARK_DATASET
    }):
        matched = [
            source
            for name, source in pairs
            if eval_rag_module._ground_truth_matches(name, source, ground_truth)
        ]
        if len(matched) > 1:
            ambiguous.append((ground_truth, matched))
        elif not matched:
            unresolvable.append(ground_truth)

    assert not ambiguous, f"ambiguous ground truths: {ambiguous}"
    assert not unresolvable, f"ground truths no corpus file matches: {unresolvable}"


def test_the_report_prints_both_granularities(eval_rag_module, capsys):
    """
    A number that is only in the JSON is a number nobody reads. The printed report is
    what a human takes a decision from, so it has to carry the stricter metric and say
    which denominator each one used.
    """
    import asyncio

    result = asyncio.run(
        eval_rag_module.evaluate_pipeline(
            dataset=[_query("tokens.py", "counting tokens in a prompt", ["rotate_credentials"])],
            corpus_docs=[
                _unit("backend/app/services/tokens.py", "counting tokens in a prompt"),
                _unit("backend/app/services/tokens.py", "def rotate_credentials(token)"),
            ],
            top_k=2,
            verbose=False,
            embedder_mode="offline",
            rerank_enabled=False,
        )
    )
    eval_rag_module.print_report(result)
    out = capsys.readouterr().out
    assert "Span Hit Rate @ K" in out
    assert "file-level" in out.lower()
    assert "unit-level" in out.lower()


def test_the_report_says_nothing_was_scored_rather_than_zero(eval_rag_module, capsys):
    """
    The other half of the printed contract.

    A dataset with no expected symbols has no span-level number to show, and "0.0%" on
    the terminal is a claim about retrieval that no measurement supports — the same error
    as an unmeasured stage printed as 0 ms, one layer up.
    """
    import asyncio

    result = asyncio.run(
        eval_rag_module.evaluate_pipeline(
            dataset=[_query("tokens.py", "counting tokens in a prompt")],
            corpus_docs=[_unit("backend/app/services/tokens.py", "counting tokens in a prompt")],
            top_k=1,
            verbose=False,
            embedder_mode="offline",
            rerank_enabled=False,
        )
    )
    assert result["metrics"]["span_hit_rate_at_k"] is None
    eval_rag_module.print_report(result)
    out = capsys.readouterr().out
    assert "no query named expected symbols" in out
    assert "Span Hit Rate @ K    : 0.0%" not in out
