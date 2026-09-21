"""
test_offline_embedder.py — The instrument, and the defect it exists to make visible.

Two jobs here:

1. Prove the offline embedder is a *faithful* double. If it is not deterministic, or
   if its window does not really truncate, then every conclusion drawn from it is an
   artefact of the double rather than a property of the pipeline.

2. Reproduce the truncation defect offline. `test_a_fact_past_the_window_is_unreachable_by_dense_search`
   is the "before" that small-to-big retrieval has to beat. It currently passes
   because the defect is real; it is expected to change when the fix lands, and the
   change should be deliberate.

No network, no weights, no model download — this runs anywhere, including CI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.services.offline_embedder import (
    DEFAULT_DIM,
    DEFAULT_WINDOW_TOKENS,
    OfflineEmbedder,
    cosine,
    rank_by_dense,
)


class _Doc:
    """Minimal stand-in for langchain_core.documents.Document."""

    def __init__(self, text: str, name: str = ""):
        self.page_content = text
        self.metadata = {"source": name or text[:8]}

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"_Doc({self.page_content[:30]!r}...)"


# ── 1. The double must be faithful ───────────────────────────────────────────


def test_the_window_matches_the_real_default_embedder():
    """
    Pinned to all-MiniLM-L6-v2's shape. If the double's window is bigger than the
    real model's, tests written against it would pass while production truncates.
    """
    assert DEFAULT_DIM == 384
    assert DEFAULT_WINDOW_TOKENS == 256


def test_embedding_the_same_text_twice_is_identical():
    e = OfflineEmbedder()
    text = "def verify_token(token): return jwt.decode(token, SECRET)"
    assert e.embed_query(text) == e.embed_query(text)
    assert e.embed_documents([text]) == [e.embed_query(text)]


def test_determinism_survives_a_new_interpreter():
    """
    Vectors must be identical across processes.

    Python's builtin `hash()` is salted per process, so an embedder built on it
    produces different vectors in the indexing run and the query run — retrieval
    then silently returns noise, with no error anywhere. The failure is invisible
    inside a single process, so this test starts a second one.

    WHY THE SAMPLE TEXT IS LONG. This test was written first with a three-token
    string and was **flaky**: a normalised vector over three tokens can only sum to
    one of four values, so two salted runs collided roughly 62% of the time and the
    test detected the bug only about a third of the time. A long sample makes the
    space of possible sums continuous, so a collision needs a coincidence rather
    than a coin flip. Do not shorten this string.
    """
    sample = " ".join(f"identifier_{i}_token" for i in range(200))
    backend_dir = str(Path(__file__).resolve().parents[1])

    # Built with an f-string and repr(), not %-formatting: `print('%.12f' % ...)`
    # inside a template that is itself %-formatted collides on the `%.12f`, and the
    # outer format consumes the wrong argument. repr() also round-trips the float
    # exactly, so the comparison is on the full precision of the sum rather than a
    # truncated rendering that could hide a difference.
    code = (
        f"import sys; sys.path.insert(0, {backend_dir!r}); "
        "from app.services.offline_embedder import OfflineEmbedder; "
        f"print(repr(sum(OfflineEmbedder().embed_query({sample!r}))))"
    )

    outputs = set()
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        outputs.add(result.stdout.strip())

    assert len(outputs) == 1, (
        f"the embedder produced different vectors in different processes: {outputs}. "
        "Something is using the builtin hash(), which is salted per process."
    )


def test_the_window_really_truncates():
    e = OfflineEmbedder(window_tokens=3)
    assert e.tokens_of("alpha beta gamma delta epsilon") == ["alpha", "beta", "gamma"]
    assert e.dropped_token_count("alpha beta gamma delta epsilon") == 2


def test_a_window_of_none_never_truncates():
    e = OfflineEmbedder(window_tokens=None)
    assert e.dropped_token_count(" ".join(str(i) for i in range(5000))) == 0


def test_the_default_reports_what_a_real_chunk_loses():
    """
    The defect in one assertion: a chunk near the chunker's ceiling loses most of
    itself before it is embedded.
    """
    from app.services import code_chunker

    e = OfflineEmbedder()
    chunk = " ".join(f"identifier_{i}" for i in range(code_chunker.MAX_CHUNK_CHARS // 8))
    dropped = e.dropped_token_count(chunk)
    assert dropped > 0, "the chunker's ceiling now fits the default embedder's window"


@pytest.mark.parametrize("bad_dim", [0, -1])
def test_a_degenerate_dimension_is_rejected(bad_dim):
    with pytest.raises(ValueError):
        OfflineEmbedder(dim=bad_dim)


@pytest.mark.parametrize("bad_window", [0, -5])
def test_a_degenerate_window_is_rejected(bad_window):
    """
    A window of 0 embeds nothing, so every document produces the same vector. The
    retriever then returns the first document for every query — which looks like a
    working system returning bad results, not a broken configuration.
    """
    with pytest.raises(ValueError):
        OfflineEmbedder(window_tokens=bad_window)


def test_an_empty_text_produces_a_well_defined_vector():
    """A zero vector makes cosine similarity undefined; it must not be returned."""
    vec = OfflineEmbedder().embed_query("")
    assert len(vec) == DEFAULT_DIM
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-9, "must be a unit vector"


def test_vectors_are_unit_length():
    vec = OfflineEmbedder().embed_query("def f(x): return x + 1")
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-9


def test_a_large_document_with_no_shared_vocabulary_stays_near_zero():
    """
    Covers the signed-bucket design, in the only regime where it is observable.

    Measured, not assumed: at the defaults (384 dims, a 256-token window) each token
    gets its own bucket and the sign makes no difference at all — 0.3428 versus
    0.3427 on the same fixture. It only starts to matter once tokens greatly
    outnumber dimensions, where an all-positive accumulation drifts every vector
    toward the same direction and unrelated documents begin to look alike.

    So this test disables the window and uses 4000 tokens, where the effect is real:

        signed   : unrelated similarity -0.0213
        unsigned : unrelated similarity +0.0726

    The assertion is that a document sharing no vocabulary with the query does not
    look positively similar to it. If the sign is ever removed, this is the test that
    notices — which is the honest reason it exists, rather than a claim that the sign
    is doing work at the defaults.
    """
    wide = OfflineEmbedder(window_tokens=None)
    query = wide.embed_query("alpha7 alpha9")
    unrelated = wide.embed_query(" ".join(f"zulu{i}" for i in range(4000)))

    assert cosine(query, unrelated) < 0.04, (
        "a document sharing no vocabulary with the query scored positively; the "
        "hashed buckets have lost their sign and every vector is drifting toward "
        "the same direction"
    )


def test_unrelated_text_scores_below_related_text():
    """
    NEGATIVE CONTROL for the similarity measure itself.

    Without this, a broken embedder that returns one fixed vector for everything
    would pass every "is the answer retrievable" test, because everything would be
    equally retrievable.
    """
    e = OfflineEmbedder()
    q = e.embed_query("verify_token validates the JWT signature")
    related = e.embed_query("def verify_token(token): checks the JWT signature")
    unrelated = e.embed_query("frontend button styling gradient border radius")

    assert cosine(q, related) > cosine(q, unrelated)


# ── 2. The defect, reproduced offline ────────────────────────────────────────


def _long_chunk_with_tail_fact() -> str:
    """
    A chunk shaped like a real one from this repo's corpus: several hundred tokens
    of ordinary statements, with one distinctive identifier near the very end — the
    position a real chunker produces for the last statements of a long function.
    """
    body = [f"const handler{i} = () => process(item{i});" for i in range(220)]
    body.append("const ROLLBACK_TICKET = 'QX-4417';")
    return "\n".join(body)


def test_a_fact_past_the_window_is_unreachable_by_dense_search():
    """
    THE DEFECT, reproduced with no model and no network.

    A distinctive identifier sits past the embedder's window. Dense search for it
    cannot find it, because the embedder never saw it. This is what is happening to
    roughly half of the chunks in this repo's own corpus, and it is invisible in
    production: retrieval returns *something*, ranked plausibly, with no error.

    When small-to-big retrieval lands, this test should be revisited — the child
    window will contain the fact, so the child becomes findable. The assertion is
    kept as-is until then so that the change is deliberate rather than incidental.
    """
    embedder = OfflineEmbedder()
    chunk = _long_chunk_with_tail_fact()

    assert embedder.dropped_token_count(chunk) > 0, "fixture no longer exceeds the window"

    results = rank_by_dense("ROLLBACK_TICKET QX-4417", [_Doc(chunk)], embedder)
    _, score = results[0]

    # The strong form of the claim, and it is directly checkable: the identifier is
    # simply not among the tokens the embedder absorbed. This is the actual cause,
    # not a correlation.
    seen = " ".join(embedder.tokens_of(chunk)).lower()
    assert "rollback_ticket" not in seen, "the window did not drop the tail after all"

    # A document sharing no tokens with the query scores ~0; the equivalent short
    # chunk scores 0.87. A 0.05 ceiling sits an order of magnitude below the control,
    # so this is a separation, not a knife-edge.
    assert score < 0.05, (
        f"score {score} suggests the tail fact was visible, but the window is "
        f"{embedder.window_tokens} tokens and the chunk is longer"
    )


def test_the_same_fact_is_reachable_when_it_fits_the_window():
    """
    CONTROL for the test above.

    Identical text, identical query, window large enough to hold it. If this failed,
    the previous test would prove nothing — it would just show that the retriever is
    broken, not that the window is the reason.
    """
    small = "const ROLLBACK_TICKET = 'QX-4417';"
    embedder = OfflineEmbedder()

    assert embedder.dropped_token_count(small) == 0

    results = rank_by_dense("ROLLBACK_TICKET QX-4417", [_Doc(small)], embedder)
    _, score = results[0]

    assert score > 0.5, f"the fact was not visible even when it fit: {score}"


def test_truncation_is_the_only_difference_between_those_two_runs():
    """
    Isolates the cause, at the level where the claim is exact.

    Same text, same query, same embedder — the only variable is the window. The
    assertion is on the absorbed *tokens* rather than the similarity score, because
    tokens are the actual mechanism: the identifier is either among them or it is
    not, with no threshold in between.

    A note on the score comparison at the end, since it is deliberately not the
    primary claim. This hashing double spreads a fixed amount of mass across its
    dimensions, so a long document's similarity to a two-token query is tiny even
    when the window is removed (measured: 0.0055). A real embedding model does not
    behave that way — it produces a sharp signal for the passage that answers the
    question. So the score ordering is directionally right but the *margin* is a
    property of the double, not of MiniLM. Use this class to test reachability, not
    ranking quality; ranking quality is what eval_rag.py is for.
    """
    embedder = OfflineEmbedder()
    chunk = _long_chunk_with_tail_fact()
    wide = OfflineEmbedder(window_tokens=None)

    assert "rollback_ticket" not in " ".join(embedder.tokens_of(chunk)).lower()
    assert "rollback_ticket" in " ".join(wide.tokens_of(chunk)).lower()
    assert embedder.dropped_token_count(chunk) > 0
    assert wide.dropped_token_count(chunk) == 0

    cut = rank_by_dense("ROLLBACK_TICKET QX-4417", [_Doc(chunk)], embedder)[0][1]
    full = rank_by_dense("ROLLBACK_TICKET QX-4417", [_Doc(chunk)], wide)[0][1]
    assert full > cut, f"removing the window did not help (cut={cut}, full={full})"


def test_most_of_a_ceiling_sized_chunk_never_reaches_the_embedder():
    """
    Quantifies the defect in the unit that matters: absorbed vs dropped tokens.

    Measured with the same token rule the real default embedder's window is
    expressed in. At the chunker's 3000-character ceiling, roughly 70% of a chunk's
    tokens are discarded before it is embedded. This is the number to beat, and it
    is why the earlier character-based estimate (46-55% of chunks truncated) was an
    understatement of the damage per chunk.
    """
    embedder = OfflineEmbedder()
    chunk = _long_chunk_with_tail_fact()

    total = len(embedder.tokens_of(chunk)) + embedder.dropped_token_count(chunk)
    absorbed = len(embedder.tokens_of(chunk))
    fraction_dropped = embedder.dropped_token_count(chunk) / total

    assert absorbed == embedder.window_tokens, (absorbed, embedder.window_tokens)
    assert 0.5 < fraction_dropped < 0.85, (
        f"expected roughly 70% of this chunk to be dropped, got {fraction_dropped:.1%}. "
        "If this moved a lot, the fixture changed — re-measure before trusting it."
    )


def test_the_retriever_ranks_the_right_document_first():
    """
    Sanity check on the whole path: the plumbing actually retrieves.

    Without this, a retriever that always returns the first document would satisfy
    the truncation tests by accident.
    """
    docs = [
        _Doc("function renderButton() { return <button>Save</button>; }", "ui.tsx"),
        _Doc("def verify_token(token): return jwt.decode(token, SECRET)", "auth.py"),
        _Doc("SELECT * FROM users WHERE id = ?", "queries.sql"),
    ]
    top, score = rank_by_dense("verify_token jwt decode", docs, OfflineEmbedder())[0]
    assert top.metadata["source"] == "auth.py", score


# ── 3. It must not be switchable on by accident ──────────────────────────────


def test_the_offline_embedder_is_not_reachable_from_configuration():
    """
    A test double that can be enabled by an environment variable is a test double
    that will one day run in production, serving lexical similarity while every
    dashboard says embeddings are healthy.

    Nothing in the config vocabulary names it, and `get_embedding_fn` must never
    return it.
    """
    from app.core.config import Settings

    text = Path(__file__).resolve().parents[1] / "app" / "core" / "config.py"
    source = text.read_text(encoding="utf-8")
    assert "OfflineEmbedder" not in source, (
        "config.py references the offline embedder — it must not be selectable at runtime"
    )

    for provider in ("ollama", "deepseek"):
        settings = Settings(_env_file=None, llm_provider=provider, openai_api_key="sk-test")
        assert "Offline" not in settings.embedding_model


def test_rank_by_dense_uses_the_document_path_for_documents():
    """
    Tests the protocol, not the arithmetic.

    `rank_by_dense` must embed documents through `embed_documents`. With this
    embedder the two paths happen to be identical, so a mutation that swaps them
    changes nothing measurably — an equivalent mutant. On a real model they are not
    interchangeable: BGE and E5 expect "query: " / "passage: " prefixes, and Jina's
    retrieval models are instruction-tuned per task. Sending documents down the query
    path would be correct today and wrong the moment the embedder is swapped, which
    is the exact operation this harness exists to evaluate.

    A recording double makes the distinction observable when the vectors cannot.
    """
    calls: list[tuple[str, object]] = []

    class _RecordingEmbedder:
        def embed_documents(self, texts):
            calls.append(("documents", list(texts)))
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text):
            calls.append(("query", text))
            return [1.0, 0.0]

    docs = [_Doc("first chunk", "a.py"), _Doc("second chunk", "b.py")]
    rank_by_dense("some query", docs, _RecordingEmbedder())

    assert calls[0][0] == "documents", f"documents went through the wrong path: {calls}"
    assert calls[0][1] == ["first chunk", "second chunk"], calls
    assert calls[1][0] == "query", f"the query did not use the query path: {calls}"
    assert len(calls) == 2, f"expected exactly one call per path, got {calls}"
