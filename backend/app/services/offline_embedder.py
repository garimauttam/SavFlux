"""
offline_embedder.py — A deterministic embedder with no model, no download and no
network, which nonetheless truncates its input exactly the way a real one does.

WHY THIS EXISTS
---------------
The dense half of the retrieval pipeline could not be tested or measured without
weights from huggingface.co. That had two costs, both real:

  1. CI never exercised the dense path at all, so the benchmark was green while
     measuring a different pipeline than the one users run.
  2. The truncation defect — chunks up to 3000 characters embedded by a model that
     reads 256 tokens — was structurally invisible, because the only leg CI did
     measure (BM25) indexes full text and therefore reads every byte the embedder
     throws away.

This class makes both testable. It is a *hashing* embedder: tokens are hashed into
a fixed number of dimensions and the resulting vector is L2-normalised. Two texts
sharing vocabulary get a high cosine similarity, which is enough to prove that the
plumbing works and — more importantly — enough to prove when it does not.

`window_tokens` is the point of the whole class. A real embedder silently discards
input past its window; so does this one. A test can therefore assert, offline and in
CI, that a fact placed past the window is unreachable by dense search. That
reproduction is the "before" that small-to-big retrieval has to beat, and it is why
this file exists rather than a three-line Mock.

WHAT THIS IS NOT
----------------
It is not a retrieval-quality model and must never be the default. Its similarity is
lexical — it cannot match a paraphrase to a symbol, which is the entire reason a real
embedding model is in the pipeline. Numbers produced with it say "the wiring works",
never "retrieval is good".

`test_offline_embedder.py` asserts it is not reachable from configuration, so this
cannot be switched on by accident by setting an environment variable.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

# Matches the real default embedder (all-MiniLM-L6-v2): 384 dims, 256-token window.
# Same shape, so a pipeline built against this double is a pipeline that fits the
# real thing.
DEFAULT_DIM = 384
DEFAULT_WINDOW_TOKENS = 256

# Word-ish tokens. Code identifiers, not bytes: the window must be measured in the
# same unit a real tokenizer counts in, or the truncation being reproduced would be
# an artefact of the double rather than a property of the model.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class OfflineEmbedder:
    """
    Deterministic, dependency-free embedder implementing LangChain's interface.

    Implements `embed_documents` / `embed_query`, which is exactly the surface
    Chroma's LangChain wrapper calls, so this substitutes for `HuggingFaceEmbeddings`
    without the pipeline knowing.

    Determinism is a hard requirement and comes from `hashlib`, never from Python's
    builtin `hash()`: the builtin is salted per process (PYTHONHASHSEED), so vectors
    would differ between an index run and a query run and retrieval would silently
    return nonsense. `test_offline_embedder.py` runs a second interpreter to prove
    this, because the failure is invisible within a single process.
    """

    def __init__(
        self,
        dim: int = DEFAULT_DIM,
        window_tokens: int | None = DEFAULT_WINDOW_TOKENS,
    ) -> None:
        if dim < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        if window_tokens is not None and window_tokens < 1:
            raise ValueError(
                f"window_tokens must be >= 1 or None, got {window_tokens}. "
                "A window of 0 embeds nothing and makes every vector identical, "
                "which looks like a working retriever that always returns the "
                "first document."
            )
        self.dim = dim
        self.window_tokens = window_tokens

    # ── The window: the behaviour under test ────────────────────────────────

    def tokens_of(self, text: str) -> list[str]:
        """
        The tokens this embedder will actually see, after truncation.

        Exposed rather than private so a test can assert *what was dropped* instead
        of only asserting a retrieval outcome — when a test fails, "the answer was
        in token 300 and the window is 256" is a better error message than
        "document not found".
        """
        tokens = _TOKEN_RE.findall(text)
        if self.window_tokens is None:
            return tokens
        return tokens[: self.window_tokens]

    def dropped_token_count(self, text: str) -> int:
        """How many tokens this embedder silently discards. 0 when it fits."""
        total = len(_TOKEN_RE.findall(text))
        return total - len(self.tokens_of(text))

    # ── Embedding ───────────────────────────────────────────────────────────

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in self.tokens_of(text):
            digest = hashlib.blake2b(token.lower().encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            bucket = value % self.dim
            # Signed hashing (the standard hashing trick): a second bit of the same
            # digest chooses the direction, so distinct tokens landing in the same
            # bucket cancel instead of accumulating.
            #
            # Honesty about when this matters, because the obvious justification for
            # it is not true at these parameters. Measured: at the defaults (384 dims,
            # a 256-token window) the sign changes nothing observable — 0.3428 versus
            # 0.3427 on the same fixture — because with fewer tokens than dimensions,
            # collisions are rare. It earns its place only once tokens greatly
            # outnumber dimensions, which is what `window_tokens=None` on a large
            # document produces: there the unrelated-similarity figure moves from
            # +0.0726 (unsigned) to -0.0213 (signed). A test covers exactly that
            # regime; nothing covers it at the defaults, because nothing can.
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vec[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # Empty, or every token collided to zero. Returning the zero vector
            # would make cosine similarity undefined; a fixed unit vector is a
            # well-defined "I contain nothing" that ranks last against any
            # non-empty query.
            vec = [0.0] * self.dim
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    # Aliases some callers use.
    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.embed_documents(texts)

    def __call__(self, text: str) -> list[float]:
        return self._vector(text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"OfflineEmbedder(dim={self.dim}, window_tokens={self.window_tokens})"


def cosine(a: list[float], b: list[float]) -> float:
    """
    Cosine similarity of two already-normalised vectors, i.e. their dot product.

    Kept here so tests and the eval harness share one definition. A disagreement
    between two implementations of "how similar" is a classic way to chase a
    retrieval bug that does not exist.
    """
    return sum(x * y for x, y in zip(a, b))


class DenseIndex:
    """
    Exact dense search over a fixed document set.

    Document vectors are computed once, in one `embed_documents` call, so scoring
    many queries against a corpus costs one embedding pass rather than one per
    query — the difference between seconds and minutes on a 600-file corpus, and
    the reason this is a class instead of a function.

    Documents are embedded through `embed_documents` and queries through
    `embed_query` even though `OfflineEmbedder` treats them identically. Several
    real models do not: BGE and E5 expect "query: " / "passage: " prefixes, and
    Jina's retrieval models are instruction-tuned per task. Getting this wrong is
    invisible until the embedder is swapped — which is the operation this index
    exists to make measurable.
    """

    def __init__(self, documents: list[Any], embedder: Any, text_of=lambda d: d.page_content):
        self.documents = documents
        self._embedder = embedder
        self._vectors = embedder.embed_documents([text_of(doc) for doc in documents])
        if len(self._vectors) != len(documents):
            raise ValueError(
                f"embedder returned {len(self._vectors)} vectors for "
                f"{len(documents)} documents"
            )

    def __len__(self) -> int:
        return len(self.documents)

    def search(self, query: str, top_k: int) -> list[Any]:
        """The `top_k` documents most similar to `query`, best first."""
        qv = self._embedder.embed_query(query)
        scored = [
            (doc, cosine(qv, vec)) for doc, vec in zip(self.documents, self._vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [doc for doc, _ in scored[:top_k]]

    def scores(self, query: str, top_k: int) -> list[tuple[Any, float]]:
        """Same ordering, with the similarity retained, for diagnostics."""
        qv = self._embedder.embed_query(query)
        scored = [
            (doc, cosine(qv, vec)) for doc, vec in zip(self.documents, self._vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


def rank_by_dense(
    query: str,
    documents: list[Any],
    embedder: OfflineEmbedder,
    text_of=lambda d: d.page_content,
) -> list[tuple[Any, float]]:
    """
    Top-k dense retrieval over `documents`, highest similarity first.

    Convenience wrapper over `DenseIndex` for the one-shot case (a test asking
    whether a fact is reachable at all). Anything scoring more than one query
    should build a `DenseIndex` instead, or it will re-embed the corpus every time.
    """
    return DenseIndex(documents, embedder, text_of).scores(query, len(documents))

