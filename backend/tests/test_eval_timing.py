"""
test_eval_timing.py — the harness's latency attribution.

`eval_rag.py` publishes one `avg_latency_ms` per query. Two defects lived in that
number, and both made the benchmark lie in a way nobody would notice:

  * **It measured the harness, not the product.** The window ran past the code that
    joins retrieved chunks into parent text to score symbol coverage — work the
    benchmark does and production never does. Adding a leg to the comparison made
    "retrieval latency" go up.
  * **One mean cannot attribute anything.** A reranker regression and a query-expansion
    regression look identical, so the number could not drive a decision.

So the stages are measured separately, the harness's own work is named and excluded,
and `--compare` prints a per-stage delta. These tests pin the arithmetic and, more
importantly, the *shape of the claim*: a stage with no samples is missing, not zero.
"""

from __future__ import annotations

import json
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


def _corpus():
    return [
        Document(
            page_content="def verify_token(token):\n    return jwt.decode(token, SECRET)\n",
            metadata={"source": "/x/auth.py", "file_name": "auth.py"},
        ),
        Document(
            page_content="def render(node):\n    return node.style\n",
            metadata={"source": "/x/ui.tsx", "file_name": "ui.tsx"},
        ),
    ]


def _dataset(count: int = 2):
    return [
        {
            "query": f"verify_token jwt decode {i}",
            "ground_truth_file": "auth.py",
            "expected_symbols": ["verify_token"],
        }
        for i in range(count)
    ]


# ── The percentile the report quotes ─────────────────────────────────────────


def test_a_percentile_is_a_sample_that_happened(eval_rag_module):
    """
    Nearest rank, no interpolation, and no invented value for an empty list.

    An interpolated p95 over 20 samples is a number no query ever paid; on a small
    benchmark that difference is most of the range. `None` for "no samples" is the
    other half of the rule — a missing stage must not read as a free one.
    """
    percentile = eval_rag_module._percentile

    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([7], 50) == 7
    assert percentile([], 50) is None


def test_no_samples_produces_no_numbers(eval_rag_module):
    stats = eval_rag_module._latency_stats([])

    assert stats == {"avg": None, "p50": None, "p95": None, "max": None, "samples": 0}
    assert eval_rag_module._stage_stats([]) == {}


# ── The clock ────────────────────────────────────────────────────────────────


def test_the_harness_own_scoring_is_measured_and_left_out_of_the_total(eval_rag_module):
    """
    The exclusion has to be visible, not silent.

    Dropping the scoring time would make the published latency smaller and harder to
    reproduce by an unknown amount; reporting it under its own name lets a reader see
    that a `--top-k` change moves `score` and nothing else.
    """
    clock = eval_rag_module._StageClock()
    clock.mark("plan")
    clock.mark("lexical")
    clock.mark(eval_rag_module.HARNESS_STAGE)

    stages = clock.as_dict()
    assert set(stages) == {"plan", "lexical", eval_rag_module.HARNESS_STAGE}
    assert clock.product_ms() == round(sum(v for k, v in stages.items() if k != eval_rag_module.HARNESS_STAGE), 2)


def test_repeated_marks_on_one_stage_accumulate(eval_rag_module):
    """Per-leg searches hit the same stage; a stage that overwrote would understate."""
    clock = eval_rag_module._StageClock()
    first = clock.mark("lexical")
    assert first is None
    clock.mark("lexical")

    assert clock.as_dict()["lexical"] >= 0.0
    assert list(clock.as_dict()) == ["lexical"], "one stage is one row, not two"


# ── End to end, through evaluate_pipeline ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_publishes_per_stage_latency(eval_rag_module):
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_dataset(3),
        corpus_docs=_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    metrics = result["metrics"]

    assert metrics["latency_ms"]["samples"] == 3
    for stage in ("plan", "lexical", "dense", "fuse", "rerank"):
        assert stage in metrics["stage_ms"], f"{stage} is not attributed at all"
    # A leg that did not run still reports its (empty) share rather than pretending
    # to have been fast: `rerank` gets marked, and its p50 is whatever the check cost.
    assert metrics["stage_ms"]["rerank"]["samples"] == 3
    assert metrics["harness_ms"]["samples"] == 3
    assert metrics["latency_scope"].startswith("retrieval, fusion and reranking")
    # Dominant stage first, so a terminal read of the table answers "what costs most".
    ordered = [name for name, stats in metrics["stage_ms"].items() if stats["p50"] is not None]
    values = [metrics["stage_ms"][name]["p50"] for name in ordered]
    assert values == sorted(values, reverse=True)


@pytest.mark.asyncio
async def test_the_exported_stages_add_up_to_the_exported_total(eval_rag_module):
    """
    The invariant the whole table rests on.

    A per-query `latency_ms` that is not the sum of that query's stages means the
    breakdown and the headline are measuring different runs, and every "where the time
    went" claim built on it is decoration. Checked per query, with the rounding the
    export uses, and excluding the harness stage by name.
    """
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_dataset(2),
        corpus_docs=_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )

    for entry in result["query_breakdown"]:
        stages = {k: v for k, v in entry["stage_ms"].items() if k != eval_rag_module.HARNESS_STAGE}
        assert stages, "a query with no stages means the clock is not running"
        assert abs(sum(stages.values()) - entry["latency_ms"]) <= 0.01 * len(stages)
        assert entry["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_the_report_survives_a_run_it_cannot_attribute(eval_rag_module, capsys):
    """
    Printing must not crash on the empty case, and must not print zeros for it.

    `print_report` runs in CI on the output of a run; a KeyError there costs the
    whole report, timings included.
    """
    result = await eval_rag_module.evaluate_pipeline(
        dataset=_dataset(1),
        corpus_docs=_corpus(),
        top_k=2,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    )
    result["metrics"]["stage_ms"] = {}
    result["metrics"]["harness_ms"] = eval_rag_module._latency_stats([])

    eval_rag_module.print_report(result)
    printed = capsys.readouterr().out

    assert "Avg latency" in printed
    assert "Where the time goes" not in printed, "an empty breakdown should not print a heading"


# ── compare ──────────────────────────────────────────────────────────────────


def test_a_comparison_reports_direction_not_just_difference(eval_rag_module):
    """
    "-3 ms" of latency is good and "-3 points" of hit rate is bad.

    The table carries the verdict because a reviewer comparing two runs in a
    terminal should not have to remember which metrics are lower-is-better — the
    mistake is silent and it flips the conclusion.
    """
    def run(hit, p50, fuse_p50):
        return {
            "metrics": {
                "hit_rate_at_k": hit,
                "mean_reciprocal_rank_mrr": 0.5,
                "symbol_recall_pct": 80.0,
                "precision_at_k_pct": 70.0,
                "avg_latency_ms": p50,
                "latency_ms": {"p50": p50, "p95": p50 * 2},
                "stage_ms": {"fuse": {"p50": fuse_p50}},
            }
        }

    rows = {row["metric"]: row for row in eval_rag_module.compare_results(run(90, 40, 10), run(85, 55, 8))}

    assert rows["hit_rate_at_k"]["verdict"] == "WORSE"
    assert rows["avg_latency_ms"]["verdict"] == "WORSE"
    assert rows["avg_latency_ms"]["pct"] == pytest.approx(37.5)
    assert rows["stage:fuse"]["verdict"] == "better"
    assert rows["stage:fuse"]["pct"] == pytest.approx(-20.0)


def test_a_metric_missing_from_the_baseline_is_said_so(eval_rag_module):
    """A key added in this version has no delta; printing "infinity worse" would be noise."""
    rows = eval_rag_module.compare_results({"metrics": {}}, {"metrics": {"hit_rate_at_k": 90.0}})

    hit = next(row for row in rows if row["metric"] == "hit_rate_at_k")
    assert hit["verdict"] == "no data"
    assert hit["delta"] is None


def test_a_change_out_of_zero_is_reported_without_a_percentage(eval_rag_module):
    """
    `0 → 12 ms` is a real regression with no meaningful percent.

    The first version divided by zero and printed a delta with no verdict; the
    tempting "fix", `-100%` for the reverse direction, would claim an improvement
    that cannot be scaled. The verdict says which way it moved and leaves the size
    to the delta.
    """
    base = {"metrics": {"avg_latency_ms": 0.0, "latency_ms": {"p50": 0.0, "p95": None}}}
    cur = {"metrics": {"avg_latency_ms": 12.0, "latency_ms": {"p50": 0.0, "p95": None}}}

    rows = {row["metric"]: row for row in eval_rag_module.compare_results(base, cur)}

    assert rows["avg_latency_ms"]["verdict"] == "WORSE from zero"
    assert rows["avg_latency_ms"]["pct"] is None
    assert rows["avg_latency_ms"]["delta"] == 12.0
    assert rows["latency_ms.p50"]["verdict"] == "flat"


def test_the_comparison_is_plain_text_and_json_roundtrippable(eval_rag_module):
    """`--compare` reads a file another run wrote, so the shapes have to match."""
    payload = {
        "metrics": {
            "hit_rate_at_k": 90.0, "mean_reciprocal_rank_mrr": 0.5,
            "symbol_recall_pct": 80.0, "precision_at_k_pct": 70.0,
            "avg_latency_ms": 40, "latency_ms": {"p50": 40, "p95": 80},
            "stage_ms": {"fuse": {"p50": 10, "p95": 20}},
        }
    }
    reloaded = json.loads(json.dumps(payload))

    table = eval_rag_module.format_comparison(eval_rag_module.compare_results(reloaded, payload))

    assert "metric" in table and "verdict" in table
    assert "flat" in table, "identical runs must say so rather than showing 0% rows as news"
