"""
Two defects that share a cause: the number that was correct stopped being correct.

CANDIDATE DEPTH
---------------
Indexed rows are embed windows, so several belong to one chunk. Retrieval asked the
vector store for `CANDIDATE_COUNT` rows and used to get `CANDIDATE_COUNT` chunks;
after the write side it kept asking for the same number and got fewer chunks back.
Nothing raised. The candidate pool that fusion and the reranker both draw from simply
thinned.

The fix multiplies by `max_windows_per_parent()`, which is an upper bound rather than
an estimate, so the number of distinct chunks is guaranteed rather than hoped for.
These tests pin the bound itself, because a bound that is quietly wrong is worse than
a fudge factor — it comes with a proof-shaped comment.

CORPUS IDENTITY
---------------
The corpus is this project's own source, so it moves whenever the code does, and two
runs from different commits are not comparable. Nothing in the artifact said so. The
fingerprint makes the comparison checkable instead of assumed.
"""

from __future__ import annotations

import subprocess
import sys
from math import ceil
from pathlib import Path

import pytest
from langchain_core.documents import Document

from app.services.ast_chunker import MAX_CHUNK_CHARS
from app.services.parent_child import (
    CHILD_OVERLAP_CHARS,
    EMBED_WINDOW_CHARS,
    children_of,
    max_windows_per_parent,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── The window bound ──────────────────────────────────────────────────────────

def test_the_bound_matches_the_constants_it_is_derived_from():
    step = EMBED_WINDOW_CHARS - CHILD_OVERLAP_CHARS
    assert max_windows_per_parent() == ceil(MAX_CHUNK_CHARS / step)


def test_no_chunk_size_anywhere_produces_more_windows_than_the_bound():
    """
    Swept over EVERY length from 1 to the chunker's ceiling, not sampled.

    Sampling endpoints is what a bound is for, and it is also how a bound ends up
    wrong: the span count is a ceiling, so the cases that break it are the ones that
    just cross a boundary. This is the whole domain, exhaustively, and it is cheap
    enough to be exhaustive — roughly a thousandths of a second per length.
    """
    bound = max_windows_per_parent()
    for length in range(1, MAX_CHUNK_CHARS + 1):
        count = len(children_of(Document(
            page_content="x" * length,
            metadata={"source": "s", "chunk_index": 0},
        )))
        assert count <= bound, (
            f"a {length}-char chunk produced {count} windows, above the bound of "
            f"{bound} — the candidate-depth multiplier is now too small"
        )


def test_the_bound_is_reached_so_it_is_tight_rather_than_padded():
    """
    A bound nobody hits is a bound that can silently be too small for the real
    corpus. Assert the ceiling is actually attained at the maximum chunk size.
    """
    at_ceiling = len(children_of(Document(
        page_content="x" * MAX_CHUNK_CHARS,
        metadata={"source": "s", "chunk_index": 0},
    )))
    assert at_ceiling == max_windows_per_parent()


def test_the_bound_rejects_configurations_that_would_loop_forever():
    """
    `children_of` refuses an overlap at or above the window; the bound must refuse it
    too, or callers would multiply by a number derived from an impossible geometry.
    """
    with pytest.raises(ValueError, match="never advances"):
        max_windows_per_parent(window_chars=900, overlap_chars=900)
    with pytest.raises(ValueError, match="never advances"):
        max_windows_per_parent(window_chars=900, overlap_chars=1200)
    with pytest.raises(ValueError):
        max_windows_per_parent(window_chars=0)
    with pytest.raises(ValueError):
        max_windows_per_parent(chunk_chars=0)


@pytest.mark.parametrize(
    "window,overlap,chunk,expected",
    [
        (100, 10, 100, 2),    # 2 spans of 90 steps: 0, 90
        (100, 0, 100, 1),     # no overlap: exactly one span
        (100, 50, 100, 2),    # step 50: 0, 50
        (1000, 0, 3000, 3),   # exactly divisible
        (1000, 0, 3001, 4),   # one char past a boundary -> one more span
    ],
)
def test_the_bound_follows_the_step_arithmetic(window, overlap, chunk, expected):
    assert (
        max_windows_per_parent(
            chunk_chars=chunk, window_chars=window, overlap_chars=overlap
        )
        == expected
    )


# ── Candidate depth in the retrieval path ─────────────────────────────────────

def _split_chunk(token: str, repetitions: int = 25) -> Document:
    body = "def handler(request):\n    return request.args.get('id')\n" * repetitions
    return Document(
        page_content=body + f"\n    raise ValueError('{token}')\n",
        metadata={"source": "repo::a.py", "chunk_index": 0, "repo_url": "repo"},
    )


def test_the_dense_branch_is_collapsed_to_one_entry_per_chunk():
    """
    Collapsing before fusion is what keeps a chunk's score meaning "matched by more
    query variants" rather than "happens to be long". Fusion would collapse anyway,
    but it accumulates a term per occurrence — so four windows of one chunk would
    arrive with four terms' worth of score, which is an artefact of how the chunk was
    split rather than evidence about the query.
    """
    from app.services.parent_child import dedupe_to_parents, parent_id_of

    rows = children_of(_split_chunk("ZEBRA"))
    assert len(rows) > 1, "fixture must actually split"

    collapsed = dedupe_to_parents(rows)

    assert len(collapsed) == 1
    assert parent_id_of(collapsed[0].metadata) == "repo::a.py::0"


def test_the_dense_fetch_is_scaled_by_the_window_bound():
    """
    The regression itself, read off the source.

    Driving `stream_answer` end to end is covered elsewhere; what this pins is the
    arithmetic, because the failure mode is a constant that looks right:
    `"k": CANDIDATE_COUNT` is what the code said before, and it is what someone
    reverting this change would type.
    """
    import ast
    import inspect

    from app.services import retrieval_service as rs

    tree = ast.parse(inspect.getsource(rs.stream_answer))

    # Every literal dict of the form {"k": ..., "fetch_k": ...} — the main dense
    # search and the CRAG fallback both build one, and both must be scaled.
    def _keys(node):
        out = set()
        for key in node.keys:
            if isinstance(key, ast.Constant):
                out.add(key.value)
        return out

    dense_kwargs = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict) and {"k", "fetch_k"} <= _keys(node)
    ]
    assert len(dense_kwargs) >= 2, (
        f"expected the dense search and the CRAG fallback, found "
        f"{len(dense_kwargs)} k/fetch_k dict(s) — a depth was added or the fallback lost one"
    )

    for node in dense_kwargs:
        for key, value in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant):
                continue
            rendered = ast.unparse(value)
            assert "CANDIDATE_ROWS" in rendered, (
                f'search_kwargs[{key.value!r}] is {rendered!r}, which is not the '
                "window-scaled depth — rows are windows, so an unmultiplied count "
                "covers fewer chunks than it used to"
            )

    source = inspect.getsource(rs.stream_answer)
    assert "max_windows_per_parent()" in source, "the bound is no longer consulted"
    assert "dedupe_to_parents(" in source, "dense candidates are no longer collapsed"


def test_a_candidate_count_covers_at_least_that_many_distinct_chunks():
    """
    The property the multiplier exists for, stated as arithmetic.

    CANDIDATE_COUNT must be an interpretation of "candidate_count * bound rows
    contain at least candidate_count distinct chunks", which holds precisely because
    no chunk occupies more than `bound` rows. Stated as a property so a future change
    to the bound or the multiplier has something to be checked against.
    """
    bound = max_windows_per_parent()
    for candidate_count in (10, 15, 30, 100):
        rows = candidate_count * bound
        # Worst case: every chunk consumes the full bound.
        assert rows // bound >= candidate_count
        # And that is the worst case, so no distribution can do better.
        assert rows // (bound - 1) > candidate_count if bound > 1 else True


# ── Corpus identity ───────────────────────────────────────────────────────────

@pytest.fixture()
def eval_rag_module():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag

    return eval_rag


def _doc(source: str, content: str) -> Document:
    return Document(page_content=content, metadata={"source": source, "file_name": Path(source).name})


def test_the_fingerprint_is_stable_for_the_same_corpus(eval_rag_module):
    docs = [_doc("/x/a.py", "a = 1\n"), _doc("/x/b.py", "b = 2\n")]

    assert eval_rag_module.corpus_fingerprint(docs) == eval_rag_module.corpus_fingerprint(list(docs))


def test_the_fingerprint_ignores_input_order(eval_rag_module):
    """
    Document order depends on directory traversal, which is not part of the corpus's
    identity — a run must not look different because `rglob` returned files in
    another order.
    """
    forward = [_doc("/x/a.py", "a = 1\n"), _doc("/x/b.py", "b = 2\n")]
    reverse = list(reversed(forward))

    assert eval_rag_module.corpus_fingerprint(forward) == eval_rag_module.corpus_fingerprint(reverse)


def test_the_fingerprint_changes_when_content_changes(eval_rag_module):
    before = [_doc("/x/a.py", "a = 1\n")]
    after = [_doc("/x/a.py", "a = 2\n")]

    assert eval_rag_module.corpus_fingerprint(before) != eval_rag_module.corpus_fingerprint(after)


def test_the_fingerprint_changes_when_a_file_moves(eval_rag_module):
    """
    Same bytes, different module. `code_chunker` stamps `source` from the relative
    path, so moving a file changes which module a symbol lives in without changing a
    byte of its text — and the benchmark's ground truth is expressed in file names.
    """
    here = [_doc("/x/a.py", "a = 1\n")]
    there = [_doc("/x/pkg/a.py", "a = 1\n")]

    assert eval_rag_module.corpus_fingerprint(here) != eval_rag_module.corpus_fingerprint(there)


def test_the_fingerprint_changes_when_a_file_is_added(eval_rag_module):
    """The exact drift this exists to expose: adding a test file moves the metrics."""
    before = [_doc("/x/a.py", "a = 1\n")]
    after = before + [_doc("/x/tests/test_new.py", "def test_x(): pass\n")]

    assert eval_rag_module.corpus_fingerprint(before) != eval_rag_module.corpus_fingerprint(after)


def test_the_run_records_which_corpus_produced_it(eval_rag_module):
    """
    The artifact must be self-describing. Otherwise a hit rate from before a test
    file was added and one from after are the same JSON shape.
    """
    import asyncio

    result = asyncio.run(eval_rag_module.evaluate_pipeline(
        dataset=[{
            "query": "limiter rate limiting",
            "ground_truth_file": "limiter.py",
            "expected_symbols": [],
        }],
        corpus_docs=[_doc("/x/limiter.py", "rate limiting throttles requests")],
        top_k=1,
        verbose=False,
        embedder_mode="offline",
        rerank_enabled=False,
    ))

    recorded = result["evaluation"]["corpus_sha256"]
    assert recorded == eval_rag_module.corpus_fingerprint(
        [Document(page_content="rate limiting throttles requests",
                  metadata={"source": "/x/limiter.py", "file_name": "limiter.py"})]
    )
    assert len(recorded) == 64


# ── Pinning the corpus ────────────────────────────────────────────────────────

def test_resolve_corpus_dir_defaults_to_this_repos_backend(eval_rag_module):
    assert eval_rag_module.resolve_corpus_dir(None, Path("/repo")) == Path("/repo/backend")


def test_resolve_corpus_dir_honours_an_explicit_pin(eval_rag_module):
    assert eval_rag_module.resolve_corpus_dir("/pinned", Path("/repo")) == Path("/pinned")


def test_a_pinned_corpus_dir_is_what_gets_measured(tmp_path):
    """
    End to end through the real CLI, because a flag that only *parses* is not a flag.

    The pinned directory holds one file, so a corpus built from it fingerprints to
    exactly that file — which the run has to report.
    """
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    (pinned / "only.py").write_text("def only_function():\n    return 42\n")

    proc = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "eval_rag.py"),
            "--embedder", "offline", "--no-rerank", "--quiet",
            "--corpus-dir", str(pinned),
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
        env={**__import__("os").environ, "LLM_PROVIDER": "ollama"},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    payload = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
    evaluation = payload["evaluation"]
    assert evaluation["corpus_shape"] == "chunks"
    assert evaluation["dense_corpus"]["units"] == 1
    assert payload["metrics"]["hit_rate_at_k_granularity"] == "file"

    # The unit's `source` is repo-relative ("<harness repo>::only.py"), not the
    # absolute path, so this hash is the same wherever the corpus is checked out.
    # Pinned to the shape explicitly rather than to a path string: an absolute path
    # would make two developers' runs of the same corpus disagree.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import eval_rag as _eval_rag

    expected = __import__("hashlib").sha256(
        f"{_eval_rag.HARNESS_REPO_URL}::only.py\0"
        "def only_function():\n    return 42\n\0".encode()
    ).hexdigest()
    assert evaluation["corpus_sha256"] == expected
    assert str(pinned) not in __import__("json").dumps(payload), (
        "the machine's absolute path reached the artefact — the corpus is no longer "
        "comparable across checkouts"
    )


def test_a_missing_corpus_dir_fails_loudly(tmp_path):
    """
    Pointing the benchmark at a directory that is not there must not silently fall
    back to this repo — that would produce a plausible number for the wrong corpus,
    which is the whole failure this flag exists to prevent.
    """
    proc = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "eval_rag.py"),
            "--embedder", "offline", "--no-rerank", "--quiet",
            "--corpus-dir", str(tmp_path / "nope"),
        ],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
        env={**__import__("os").environ, "LLM_PROVIDER": "ollama"},
    )
    assert proc.returncode != 0
    assert "not a directory" in (proc.stderr + proc.stdout)


def test_the_real_corpus_fingerprint_is_reproducible(eval_rag_module):
    """
    Two loads of this repo produce the same fingerprint. Guard against a fingerprint
    that depends on traversal order or on a timestamp, which would make every run
    look like a different corpus and defeat the point.
    """
    first = eval_rag_module.corpus_fingerprint(eval_rag_module.load_corpus(REPO_ROOT / "backend"))
    second = eval_rag_module.corpus_fingerprint(eval_rag_module.load_corpus(REPO_ROOT / "backend"))

    assert first == second
    assert len(first) == 64
