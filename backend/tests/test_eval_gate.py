"""
test_eval_gate.py — the regression gate has to actually fail.

WHY THIS FILE EXISTS
--------------------
CI's quality check was a single absolute floor: `if hit_rate < 40: fail`. The
benchmark scores 65.91. So a change that took retrieval from 66% to 45% — a
chunker that stopped emitting symbols, an RRF weight set to zero, a query
enhancer that began eating the filename scope — passed CI. The floor was set so
far below the score that the gate could not fail for any plausible regression,
which is the same as not having one.

The replacement holds each quality metric to a floor describing the largest drop
that does not mean the pipeline broke. These tests assert the property that
matters: a run that is healthy passes, a run that is broken fails, and neither
the noise a real corpus edit produces nor a change of measurement definitions
is mistaken for either.

The old 40% floor is kept as an explicit test, because the temptation to put the
number back is exactly what a future reader will not notice doing.

These run against the offline embedder, so they need no weights and no network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "benchmarks" / "rag_offline_baseline.json"


@pytest.fixture()
def ev():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag

    return eval_rag


def run(**metrics):
    """A minimal result document with the metrics a run always reports."""
    return {
        "metrics": {
            "hit_rate_at_k": 65.91,
            "span_hit_rate_at_k": 52.27,
            "hit_rate_at_k_answerable": 65.91,
            "symbol_recall_pct": 68.29,
            "precision_at_k_pct": 18.18,
            "span_precision_at_k_pct": 12.73,
            "mean_reciprocal_rank_mrr": 0.432,
            "span_mrr": 0.313,
            "by_leg": {"fused": {
                "hit_rate_at_k": 65.91,
                "span_hit_rate_at_k": 52.27,
                "symbol_recall_pct": 68.29,
                "mean_reciprocal_rank_mrr": 0.432,
            }},
            **metrics,
        },
        "evaluation": {
            "corpus_shape": "chunks",
            "top_k": 5,
            "corpus_sha256": "aaa",
            "dataset_sha256": "bbb",
            "metric_depth": {"rule": "top-k-slice-v1"},
            "ground_truth": {"matching": "exact-basename-v2"},
        },
    }


# ── 1. A healthy run passes ─────────────────────────────────────────────────


def test_an_unchanged_run_passes(ev):
    outcome = ev.evaluate_gate(run(), run())
    assert outcome["passed"], outcome["failures"]
    assert outcome["failures"] == []


def test_every_gated_metric_is_actually_graded(ev):
    """A gate that silently stops checking a metric is worse than no gate."""
    outcome = ev.evaluate_gate(run(), run())
    graded = {row["metric"] for row in outcome["rows"]}
    assert graded == set(ev.QUALITY_FLOORS) | {
        f"by_leg.fused.{m}" for m in ev.LEG_FLOORS["fused"]
    }
    assert outcome["skipped"] == []


def test_an_improvement_passes(ev):
    outcome = ev.evaluate_gate(run(), run(hit_rate_at_k=78.0, symbol_recall_pct=80.0))
    assert outcome["passed"]


# ── 2. A broken run fails. This is the whole point. ─────────────────────────


def test_a_collapse_ci_used_to_call_a_pass_now_fails(ev):
    """
    66% → 45%. The old check asked `is it below 40` and answered no. A retrieval
    pipeline that lost 21 points of its score is broken, and the gate says so.
    """
    outcome = ev.evaluate_gate(run(), run(hit_rate_at_k=45.0, span_hit_rate_at_k=35.0))
    assert not outcome["passed"]
    failed = {r["metric"] for r in outcome["failures"]}
    assert "hit_rate_at_k" in failed
    assert "span_hit_rate_at_k" in failed


def test_a_regression_just_past_the_floor_fails(ev):
    """Boundary: the floor is a floor, not a suggestion."""
    baseline, floor = 65.91, ev.QUALITY_FLOORS["hit_rate_at_k"]
    assert not ev.evaluate_gate(run(), run(hit_rate_at_k=baseline - floor - 0.01))["passed"]
    assert ev.evaluate_gate(run(), run(hit_rate_at_k=baseline - floor + 0.01))["passed"]


def test_the_production_leg_is_gated_separately(ev):
    """
    A regression that shows up in `fused` but not in the headline average is the
    kind that matters, because fused is what the agent actually retrieves with.
    """
    outcome = ev.evaluate_gate(run(), run(by_leg={"fused": {
        "hit_rate_at_k": 40.0, "span_hit_rate_at_k": 52.27,
        "symbol_recall_pct": 68.29, "mean_reciprocal_rank_mrr": 0.432,
    }}))
    assert not outcome["passed"]
    assert "by_leg.fused.hit_rate_at_k" in {r["metric"] for r in outcome["failures"]}


def test_mrr_is_gated_on_its_own_scale(ev):
    """MRR lives on 0..1, so a 0.05 drop there is 16% relative — a real change."""
    assert not ev.evaluate_gate(run(), run(mean_reciprocal_rank_mrr=0.36))["passed"]
    assert ev.evaluate_gate(run(), run(mean_reciprocal_rank_mrr=0.40))["passed"]


# ── 3. Noise and definitional churn must not be called regressions ───────────


def test_one_query_of_drift_does_not_fail(ev):
    """
    Over 44 queries, one query is 2.27 points of a rate. A baseline refreshed
    against a corpus that gained a file will see that much movement routinely,
    and a gate that fails on it is a gate people disable.
    """
    assert ev.evaluate_gate(run(), run(hit_rate_at_k=63.64))["passed"]


def test_a_changed_measurement_definition_is_skipped_not_graded(ev):
    """
    A stricter answer key makes two runs genuinely incomparable. Failing on the
    difference would be grading a definition change as a pipeline regression —
    which is the specific lie this harness exists to avoid.
    """
    current = run()
    current["evaluation"]["ground_truth"]["matching"] = "exact-path-v3"
    outcome = ev.evaluate_gate(run(), current)
    assert outcome["passed"]
    assert any("definition changed" in s["reason"] for s in outcome["skipped"])


def test_a_changed_corpus_shape_is_a_warning_not_a_failure(ev):
    current = run()
    current["evaluation"]["corpus_shape"] = "files"
    outcome = ev.evaluate_gate(run(), current)
    assert outcome["passed"]
    assert any("corpus_shape" in w for w in outcome["warnings"])


def test_a_changed_corpus_is_reported_but_does_not_fail(ev):
    """
    The corpus is this repository, so it changes on nearly every commit. A
    fingerprint-keyed gate would fail every PR or never fire; a silent one would
    let a reader trust a number that includes an unexplained change. The answer
    is to say it out loud.
    """
    current = run()
    current["evaluation"]["corpus_sha256"] = "changed"
    outcome = ev.evaluate_gate(run(), current)
    assert outcome["passed"]
    assert any("corpus changed" in w for w in outcome["warnings"])


def test_a_metric_this_run_did_not_measure_is_skipped(ev):
    current = run()
    del current["metrics"]["span_precision_at_k_pct"]
    outcome = ev.evaluate_gate(run(), current)
    assert outcome["passed"]
    assert any(s["metric"] == "span_precision_at_k_pct" for s in outcome["skipped"])


# ── 4. The committed baseline is real, and the old floor is recorded ─────────


def test_the_committed_baseline_exists_and_is_a_baseline(ev):
    assert BASELINE_PATH.is_file(), (
        "the regression gate has nothing to compare against. Record one with "
        "  python eval_rag.py --embedder offline --no-rerank "
        "--save-baseline benchmarks/rag_offline_baseline.json"
    )
    import json
    doc = json.loads(BASELINE_PATH.read_text())
    assert doc["baseline_format"] == "rag-baseline-v1"
    assert doc["metrics"]["hit_rate_at_k"] > 0
    assert doc["evaluation"]["corpus_sha256"]


def test_the_committed_baseline_omits_the_per_query_breakdown(ev):
    """
    28 kB of per-query detail that changes on every commit. A baseline whose
    diff is unreadable is a baseline nobody reviews.
    """
    import json
    doc = json.loads(BASELINE_PATH.read_text())
    assert "query_breakdown" not in doc
    assert BASELINE_PATH.stat().st_size < 20_000


def test_the_old_absolute_floor_would_not_have_caught_a_real_regression(ev):
    """
    Kept as a number rather than a story: the score is ~66 and the old floor was
    40, so it could not fail for any regression worth noticing.
    """
    import json
    doc = json.loads(BASELINE_PATH.read_text())
    scored = doc["metrics"]["hit_rate_at_k"]
    broken_run = ev.evaluate_gate(doc, run(hit_rate_at_k=45.0))
    assert not broken_run["passed"], "the gate should catch a 21-point collapse"
    assert not (45.0 < 40), "…and the old rule would have called that a pass"


# ── 5. The baseline document is what it claims to be ────────────────────────


def test_baseline_document_keeps_the_inputs_and_drops_the_noise(ev):
    full = run()
    full["query_breakdown"] = [{"query": "q", "rank": 1} for _ in range(500)]
    doc = ev.baseline_document(full)
    assert "metrics" in doc and "evaluation" in doc
    assert "query_breakdown" not in doc
    assert doc["baseline_format"] == "rag-baseline-v1"


def test_format_gate_names_the_failing_metrics(ev):
    outcome = ev.evaluate_gate(run(), run(hit_rate_at_k=40.0, mean_reciprocal_rank_mrr=0.30))
    text = ev.format_gate(outcome)
    assert "FAIL" in text
    assert "hit_rate_at_k" in text
    # A reader who hit this in CI has to be told what to do next.
    assert "--save-baseline" in text


# ── 6. The committed baseline must describe the tree it ships with ──────────
#
# This one exists because the first committed baseline did not, and the symptom
# arrived on someone else's machine: every run reported
#
#     NOTE  the corpus changed since this baseline was recorded
#
# The baseline is built from this repository's own source, so adding ANY .py file
# under backend/ changes the corpus. The baseline was recorded first and
# backend/tests/test_eval_gate.py was added afterwards, in the same commit — so
# the very tree that shipped the baseline had a different corpus than the
# baseline recorded, and a fresh clone could never be clean.
#
# Nothing else would have caught it. The gate still passed; the metric deltas
# were exactly zero. A warning that fires on every run is a warning nobody reads,
# so the staleness is now a test failure instead: touching backend source without
# re-recording the baseline fails CI and says what to run.

@pytest.fixture(scope="module")
def built_corpus():
    """The same corpus the benchmark builds: this repo's backend/, chunked.

    Deliberately independent of the `ev` fixture, which is function-scoped: a
    module-scoped fixture may not request one. Importing here is the same
    `sys.path` dance, done once.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag as module

    return module, module.load_corpus(REPO_ROOT / "backend", "chunks")


def test_the_baseline_corpus_matches_the_tree_it_ships_with(built_corpus):
    import json
    module, corpus = built_corpus
    recorded = json.loads(BASELINE_PATH.read_text())["evaluation"]["corpus_sha256"]
    actual = module.corpus_fingerprint(corpus)

    assert actual == recorded, (
        "the committed baseline was recorded against a different corpus.\n"
        f"  baseline: {recorded[:16]}\n"
        f"  this tree: {actual[:16]}\n"
        "Adding any .py file under backend/ changes the corpus, so a baseline "
        "recorded before that change is stale and every run reports a corpus "
        "NOTE it can never clear.\n"
        "Re-record it:\n"
        "  python eval_rag.py --embedder offline --no-rerank \\\n"
        "      --save-baseline benchmarks/rag_offline_baseline.json"
    )


def test_the_baseline_records_the_corpus_size_actually_present(built_corpus):
    """The unit count is what a reader checks first when a number looks wrong."""
    import json
    module, corpus = built_corpus
    doc = json.loads(BASELINE_PATH.read_text())
    dense = module._dense_corpus(corpus)
    # `units` is the raw chunk count the sparse leg indexes; `windows` is the
    # dense leg's windows. They differ because a chunk longer than the embedder
    # window becomes several windows, so a reader who checks only one of the
    # two gets a number that looks wrong for a different reason.
    assert doc["evaluation"]["dense_corpus"]["units"] == len(corpus)
    assert doc["evaluation"]["dense_corpus"]["windows"] == len(dense)
    assert doc["evaluation"]["corpus_source_files"] == len(
        module.project_source_files(REPO_ROOT / "backend")
    )


def test_the_corpus_is_reproducible_across_machines(built_corpus, tmp_path):
    """
    A fingerprint nobody can reproduce is not a fingerprint. The corpus hash is
    what lets two runs be compared, so it must depend on the *content* of the
    source tree and on nothing else -- not the absolute path of the checkout,
    not the machine it was built on.

    This is the check behind the gate's promise that a PR run and a laptop run
    are talking about the same corpus. The same backend tree is copied to a
    different absolute path and re-ingested: if the hash moved, every NOTE on
    every PR is telling the reader nothing.
    """
    import shutil

    import json

    module, corpus = built_corpus
    elsewhere = tmp_path / "a-completely-different-checkout-path"
    # Copy only what the corpus is built from. Copying the whole backend/
    # directory would drag the virtualenv and __pycache__ along with it, which
    # is slow in CI and can exhaust the runner's disk.
    for source in module.project_source_files(REPO_ROOT / "backend"):
        target = elsewhere / source.relative_to(REPO_ROOT / "backend")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    moved = module.load_corpus(elsewhere, "chunks")

    assert module.corpus_fingerprint(moved) == module.corpus_fingerprint(corpus)
    assert json.loads(BASELINE_PATH.read_text())["evaluation"]["corpus_sha256"] == (
        module.corpus_fingerprint(corpus)
    )
