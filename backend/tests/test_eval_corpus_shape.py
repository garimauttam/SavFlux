"""
The benchmark's corpus shape: production's unit, not the harness's convenience.

`load_corpus` used to return one Document per FILE and the dense leg windowed those
files. Production indexes one chunk per symbol. The gap was not cosmetic — at file
shape the harness reported bm25_only 82.9% and fused 89.4% symbol recall; at chunk
shape, the same queries and the same code give 66.7% and 82.9%. Retrieving a file
made every symbol inside it "present", which is why symbol recall tracked the hit
rate almost exactly at file shape.

These tests pin the shape, the two guards that keep it from silently reverting, and
the labels that stop a file-level number being read as "the answer was retrieved".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.documents import Document

REPO_ROOT = Path(__file__).resolve().parents[2]

SIMPLE_FILE = """\
def alpha(value):
    return value + 1


def beta(value):
    return value * 2


def gamma(value):
    return value - 3
"""


@pytest.fixture()
def eval_rag_module():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag

    return eval_rag


@pytest.fixture()
def corpus_dir(tmp_path):
    """A tiny stand-in for the checkout: one file, three symbols."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "shapes.py").write_text(SIMPLE_FILE)
    return root


def _unit_texts(units):
    return [u.page_content for u in units]


# ── 1. The unit is a chunk ────────────────────────────────────────────────────

def test_a_file_becomes_one_unit_per_symbol(eval_rag_module, corpus_dir):
    """
    The unit the retriever ranks is a symbol, not a file.

    Three top-level functions must produce three units, each holding one of them.
    Asserted on the content rather than on a count, because a count of 3 would also
    be satisfied by three arbitrary slices of the file.
    """
    units = eval_rag_module.load_corpus(corpus_dir)

    assert len(units) == 3, _unit_texts(units)
    for needle in ("def alpha", "def beta", "def gamma"):
        matching = [u for u in units if needle in u.page_content]
        assert len(matching) == 1, f"{needle!r} appears in {len(matching)} units"
    # Each unit is one symbol, so no unit holds two of them.
    assert all(u.page_content.count("def ") == 1 for u in units)
    # Same file, one source string: the file-level metric groups units by this.
    assert len({u.metadata["source"] for u in units}) == 1


def test_units_are_symbol_sized_not_file_sized(eval_rag_module, corpus_dir):
    """
    A unit is far smaller than the file it came from — the whole point.

    At file shape the fixture's file would be a single 100-character unit; with a
    real source file it is thousands of characters and dozens of embed windows.
    """
    units = eval_rag_module.load_corpus(corpus_dir)
    file_chars = len(SIMPLE_FILE)

    assert max(len(u.page_content) for u in units) < file_chars
    assert all(len(u.page_content) <= eval_rag_module.MAX_CHUNK_CHARS for u in units)


def test_this_repos_own_corpus_is_chunks(eval_rag_module):
    """The real corpus, not a fixture: production's unit, within its ceiling."""
    corpus = eval_rag_module.load_corpus(REPO_ROOT / "backend")
    files = eval_rag_module.project_source_files(REPO_ROOT / "backend")

    assert len(corpus) > len(files), (
        f"{len(corpus)} units for {len(files)} files is file shape — a multi-symbol "
        "file must yield several units"
    )
    for doc in corpus:
        assert len(doc.page_content) <= eval_rag_module.MAX_CHUNK_CHARS
        assert doc.metadata.get("file_name"), f"unit with no file_name: {doc.metadata}"
        assert doc.metadata.get("source"), f"unit with no source: {doc.metadata}"


def test_a_unit_carries_a_repo_relative_source(eval_rag_module, corpus_dir):
    """
    `source` must not be the machine's absolute path.

    With an absolute path the corpus fingerprint changes when the checkout moves, so
    the same corpus measured on two machines looks like two different corpora. The
    fingerprint's own docstring has always claimed it hashes a *relative* path.
    """
    units = eval_rag_module.load_corpus(corpus_dir)

    for doc in units:
        assert doc.metadata["source"].startswith(eval_rag_module.HARNESS_REPO_URL)
        assert str(corpus_dir) not in doc.metadata["source"]


# ── 2. Both legs read one shape ───────────────────────────────────────────────

def test_both_legs_read_the_same_unit_set(eval_rag_module, corpus_dir):
    """
    The lexical leg and the dense leg must differ in TEXT, not in unit.

    Production reads different text from the same row on purpose: dense reads the
    window, BM25 reads the parent. What must not differ is which documents exist.
    Both are built from `corpus_docs` here, so one entry per unit, and every dense
    parent resolves back to a unit rather than to a whole file.
    """
    from app.services.hybrid_retriever import BM25Index
    from app.services.parent_child import parent_context

    units = eval_rag_module.load_corpus(corpus_dir)
    dense_docs = eval_rag_module._dense_corpus(units)

    # BM25: exactly one entry per unit — not per window, and not per file.
    assert len(BM25Index(units).documents) == len(units)
    # Dense: windowed, but every window's parent is exactly a unit's text.
    assert {parent_context(d) for d in dense_docs} == {u.page_content for u in units}


def test_a_window_is_not_a_unit(eval_rag_module, corpus_dir):
    """
    Guard the guard: the dense leg really does window, so "same unit set" is not
    trivially true. If `_dense_corpus` ever returned its input unchanged this test
    fails, and so would the reasoning in the test above.
    """
    long_file = corpus_dir / "long.py"
    long_file.write_text("def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(400)))

    units = eval_rag_module.load_corpus(corpus_dir)
    dense_docs = eval_rag_module._dense_corpus(units)

    assert len(dense_docs) > len(units), "the dense leg is no longer windowing"


# ── 3. The guards ─────────────────────────────────────────────────────────────

def test_the_guard_rejects_an_oversized_unit(eval_rag_module, corpus_dir, monkeypatch):
    """
    An oversized unit means the harness is measuring something production never
    ranks. It must fail loudly rather than report a number for it.
    """
    # Below the fixture's ~36-character units, so the ceiling is genuinely exceeded.
    monkeypatch.setattr(eval_rag_module, "MAX_CHUNK_CHARS", 20)

    with pytest.raises(RuntimeError) as excinfo:
        eval_rag_module.load_corpus(corpus_dir)

    message = str(excinfo.value)
    assert "MAX_CHUNK_CHARS" in message
    assert "20" in message, f"the ceiling is not quoted back: {message}"


def test_the_guard_rejects_a_unit_with_no_file(eval_rag_module):
    """
    File-level ground truth matches on `file_name`/`source`. A unit without them can
    never be counted as a hit, so it would depress the score with nothing to explain
    it.
    """
    orphan = Document(page_content="def f():\n    return 1\n", metadata={})

    with pytest.raises(RuntimeError) as excinfo:
        eval_rag_module._assert_unit_shape([orphan], "chunks")

    assert "file_name" in str(excinfo.value)


def test_the_guard_does_not_apply_to_the_legacy_file_shape(eval_rag_module, corpus_dir):
    """
    The file shape is allowed to break the ceiling — that is what makes it the file
    shape, and it is kept deliberately for reproducing old baselines.
    """
    units = eval_rag_module.load_corpus(corpus_dir, shape="files")

    assert len(units) == 1
    eval_rag_module._assert_unit_shape(units, "files")   # must not raise


def test_a_non_empty_file_that_yields_nothing_is_loud(
    eval_rag_module, corpus_dir, monkeypatch
):
    """
    Ingestion's loader skips files it cannot read, which silently shrinks the corpus.
    A smaller corpus still produces a plausible number, so the harness refuses.
    """
    import app.services.ingestion_service as ingestion

    monkeypatch.setattr(ingestion, "_load_and_split", lambda *a, **k: [])

    with pytest.raises(RuntimeError) as excinfo:
        eval_rag_module.load_corpus(corpus_dir)

    assert "shapes.py" in str(excinfo.value), (
        f"the failure does not name the file, so it is undiagnosable: {excinfo.value}"
    )


def test_an_empty_file_contributes_nothing_and_is_not_an_error(eval_rag_module, corpus_dir):
    """
    `__init__.py` files are empty and legitimately produce no units. That is not the
    same as a file being dropped, and it must not be reported as a failure.
    """
    (corpus_dir / "__init__.py").write_text("")

    units = eval_rag_module.load_corpus(corpus_dir)

    assert len(units) == 3


def test_an_unknown_shape_is_rejected(eval_rag_module, corpus_dir):
    with pytest.raises(ValueError) as excinfo:
        eval_rag_module.load_corpus(corpus_dir, shape="paragraphs")

    assert "paragraphs" in str(excinfo.value)


# ── 4. Corpus identity ────────────────────────────────────────────────────────

def test_the_fingerprint_changes_with_the_shape(eval_rag_module, corpus_dir):
    """
    A baseline from the old shape must not be comparable with a new one silently. The
    fingerprint is what makes the two tellable apart in an artefact.
    """
    chunks = eval_rag_module.corpus_fingerprint(eval_rag_module.load_corpus(corpus_dir))
    files = eval_rag_module.corpus_fingerprint(
        eval_rag_module.load_corpus(corpus_dir, shape="files")
    )

    assert chunks != files
    # ...and each shape is stable, so the difference is the shape and not noise.
    assert chunks == eval_rag_module.corpus_fingerprint(eval_rag_module.load_corpus(corpus_dir))


def test_the_fingerprint_ignores_where_the_checkout_lives(eval_rag_module, tmp_path):
    """
    Same content, two different directories, one identity.

    This is what lets a baseline recorded on one machine be compared with a run on
    another. At file shape the absolute path was part of the hash, so they differed.
    """
    first = tmp_path / "checkout-a"
    second = tmp_path / "somewhere" / "else" / "checkout-b"
    for root in (first, second):
        root.mkdir(parents=True)
        (root / "shapes.py").write_text(SIMPLE_FILE)

    assert eval_rag_module.corpus_fingerprint(
        eval_rag_module.load_corpus(first)
    ) == eval_rag_module.corpus_fingerprint(eval_rag_module.load_corpus(second))


# ── 5. The artefact says what it measured ─────────────────────────────────────

@pytest.mark.asyncio
async def test_the_result_records_the_unit_shape_and_the_file_level_label(eval_rag_module):
    """
    A file-level hit rate must not be readable as "the answer was retrieved", and the
    number of chances each hit had has to be in the artefact rather than in a
    convention.
    """
    corpus = [
        Document(page_content=c, metadata={"source": "r::target.py", "file_name": "target.py"})
        for c in ("def alpha():\n    return 1\n", "def beta():\n    return 2\n",
                  "def gamma():\n    return 3\n")
    ]
    result = await eval_rag_module.evaluate_pipeline(
        dataset=[
            {"query": "alpha", "ground_truth_file": "target.py", "expected_symbols": []},
            {"query": "beta", "ground_truth_file": "absent.py", "expected_symbols": []},
        ],
        corpus_docs=corpus,
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
        corpus_shape="chunks",
        corpus_files=1,
    )

    assert result["metrics"]["hit_rate_at_k_granularity"] == "file"
    evaluation = result["evaluation"]
    assert evaluation["corpus_shape"] == "chunks"
    assert evaluation["dense_corpus"]["units"] == 3
    assert evaluation["corpus_source_files"] == 1

    # The per-file figure must be computed with the same predicate the rank uses.
    per_file = evaluation["units_per_expected_file"]
    expected_counts = [
        sum(1 for d in corpus if eval_rag_module._unit_matches_file(d, item["ground_truth_file"]))
        for item in ([{"ground_truth_file": "target.py"}, {"ground_truth_file": "absent.py"}])
    ]
    assert per_file["max"] == max(expected_counts) == 3
    assert per_file["min"] == 0
    # A query whose file contributed no unit cannot hit, and the artefact says so.
    assert per_file["queries_with_no_unit"] == 1
    assert [q["expected_file_units"] for q in result["query_breakdown"]] == expected_counts


@pytest.mark.asyncio
async def test_the_units_per_file_count_uses_the_rank_s_own_predicate(eval_rag_module):
    """
    The number printed beside the hit rate must describe the hit rate's predicate.

    File-level matching accepts a substring and falls back to `source`. A count that
    used exact `file_name` equality would agree with it on an obvious fixture and
    disagree on a real one — a target of "limiter.py" against
    "rate_limiter.py", or a unit whose `source` carries the match.

    The expectation is derived from the module's own predicate, so a second
    definition inside the code disagrees with this test rather than being confirmed
    by it.
    """
    corpus = [
        Document(page_content="def a():\n    return 1\n",
                 metadata={"source": "r::target.py", "file_name": "target.py"}),
        # Matches only as a substring of file_name.
        Document(page_content="def b():\n    return 2\n",
                 metadata={"source": "r::prefix_target.py", "file_name": "prefix_target.py"}),
        # Matches only through `source`, which is empty-file_name territory.
        Document(page_content="def c():\n    return 3\n",
                 metadata={"source": "r::nested/target.py", "file_name": ""}),
    ]

    expected = sum(1 for d in corpus if eval_rag_module._unit_matches_file(d, "target.py"))
    assert expected == 3, "fixture no longer exercises both match paths"

    result = await eval_rag_module.evaluate_pipeline(
        dataset=[{"query": "target", "ground_truth_file": "target.py", "expected_symbols": []}],
        corpus_docs=corpus,
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
        corpus_shape="chunks",
    )

    assert result["query_breakdown"][0]["expected_file_units"] == expected
    assert result["evaluation"]["units_per_expected_file"]["max"] == expected
    assert result["evaluation"]["units_per_expected_file"]["min"] == expected


def test_the_report_states_the_metric_is_file_level(eval_rag_module, capsys):
    """
    The printed report is what a human reads. It must carry the same qualifier as
    the JSON, or the two disagree in the place a decision gets made.
    """
    result = {
        "metrics": {
            "total_queries": 1, "hit_rate_at_k": 100.0,
            "hit_rate_at_k_granularity": "file",
            "mean_reciprocal_rank_mrr": 1.0, "symbol_recall_pct": 50.0,
            "precision_at_k_pct": 20.0, "avg_latency_ms": 12.0, "by_leg": {},
        },
        "evaluation": {
            "corpus_shape": "chunks",
            "corpus_source_files": 135,
            "dense_corpus": {"units": 1591, "windows": 2375},
            "units_per_expected_file": {
                "min": 2, "median": 12, "p90": 50, "max": 53, "queries_with_no_unit": 0,
            },
        },
        "query_breakdown": [],
    }

    eval_rag_module.print_report(result)
    printed = capsys.readouterr().out

    assert "file-level" in printed
    assert "1591 units (chunks)" in printed
    assert "Units per expected file" in printed
    assert "max 53" in printed


def test_the_cli_runs_in_the_legacy_shape_and_says_so(corpus_dir):
    """
    `--corpus-shape files` must reach the artefact, not just parse.

    It exists so an old baseline can be reproduced; a flag that silently measured
    the default shape would make the comparison it exists for impossible.
    """
    proc = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "eval_rag.py"),
            "--embedder", "offline", "--no-rerank", "--quiet",
            "--corpus-shape", "files",
            "--corpus-dir", str(corpus_dir),
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
        env={**__import__("os").environ, "LLM_PROVIDER": "ollama"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["evaluation"]["corpus_shape"] == "files"
    assert payload["evaluation"]["dense_corpus"]["units"] == 1, (
        "file shape must be one unit per file"
    )
    assert payload["metrics"]["hit_rate_at_k_granularity"] == "file"
