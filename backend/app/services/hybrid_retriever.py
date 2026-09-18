"""
hybrid_retriever.py — Combines Dense Vector Search (ChromaDB) with BM25 Lexical Keyword Search using Reciprocal Rank Fusion (RRF).

WHY HYBRID SEARCH?
- Dense Vectors (Semantic Search): Great for concept-level questions ("how is auth handled?").
  Weak at exact symbol matching (variable names, error codes, specific function calls like `JWT_SECRET`).
- BM25 (Lexical/Keyword Search): Exact matching on tokens/identifiers.
  Weak at understanding synonyms or general concepts.
- Combined (RRF): Combines the rank positions of documents from both methods into a unified candidate pool.
"""

import re
from typing import List, Dict, Any, Optional, Set
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi


def _normalize_repo_url(url: str) -> str:
    """
    Normalize a repo URL for comparison in the BM25 filter.

    ChromaDB stores the normalized form (no trailing slash, no .git suffix).
    The caller may pass a URL in a different form — strip these so the filter
    doesn't silently return 0 results due to a trailing slash mismatch.
    """
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _tokenize(text: str) -> List[str]:
    """
    Code-aware tokenizer for BM25.
    Splits on camelCase, snake_case, acronym boundaries, and non-alphanumerics.
    Also emits the original unsplit identifier so exact-match queries still work.
    """
    seen: Set[str] = set()
    result: List[str] = []

    def _emit(tok: str) -> None:
        t = tok.lower()
        if len(t) > 1 and t not in seen:
            seen.add(t)
            result.append(t)

    for raw in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        _emit(raw)                                          # original (lowercased)
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)       # camelCase split
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)  # acronym boundary: HTTPClient
        for part in s.split():
            _emit(part)

    # Also split snake_case segments directly (handles foo_bar when _ is a separator)
    for tok in re.findall(r"[A-Za-z0-9]+", text.lower()):
        _emit(tok)

    return result


class BM25Index:
    """
    In-memory BM25 index over documents in the current collection.
    """
    def __init__(self, documents: List[Document]):
        self.documents = documents
        tokenized_corpus = [_tokenize(doc.page_content) for doc in documents]
        self.bm25 = BM25Okapi(tokenized_corpus) if tokenized_corpus else None

    def search(
        self,
        query: str,
        top_k: int = 15,
        repo_url: Optional[str] = None,
        repo_urls: Optional[List[str]] = None,  # multi-repo cross-search
        file_filter: Optional[str] = None,
    ) -> List[Document]:
        if not self.bm25 or not self.documents:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = self.bm25.get_scores(tokens)

        # Resolve repo filter: repo_urls (plural) takes precedence over repo_url (singular).
        # Normalize all URLs so trailing-slash / .git variants don't cause silent misses.
        if repo_urls:
            _repo_set: set[str] | None = {_normalize_repo_url(u) for u in repo_urls}
        elif repo_url:
            _repo_set = {_normalize_repo_url(repo_url)}
        else:
            _repo_set = None

        # Pair with documents and filter if needed
        doc_scores = []
        for idx, score in enumerate(scores):
            if score <= 0.0:
                continue
            doc = self.documents[idx]

            # Apply repo filter if provided — compare normalized forms
            if _repo_set and _normalize_repo_url(doc.metadata.get("repo_url", "")) not in _repo_set:
                continue
            
            # Apply file filter if provided
            if file_filter:
                source = doc.metadata.get("source", "")
                fname = doc.metadata.get("file_name", "")
                if file_filter.lower() not in source.lower() and file_filter.lower() not in fname.lower():
                    continue

            doc_scores.append((score, doc))

        doc_scores.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in doc_scores[:top_k]]


def reciprocal_rank_fusion(
    ranked_lists: List[List[Document]],
    k: int = 60,
    top_n: int = 15,
) -> List[Document]:
    """
    Reciprocal Rank Fusion (RRF) algorithm:
    RRF_score(d) = SUM_{list in ranked_lists} (1.0 / (k + rank(d)))
    
    k=60 is the standard constant from the original RRF paper (Cormack et al.).
    """
    rrf_scores: Dict[str, float] = {}
    doc_lookup: Dict[str, Document] = {}

    for doc_list in ranked_lists:
        for rank, doc in enumerate(doc_list, start=1):
            # Create a unique key based on source and chunk index (no page_content to avoid whitespace mismatches)
            doc_id = f"{doc.metadata.get('source', '')}::{doc.metadata.get('chunk_index', 0)}"
            if doc_id not in doc_lookup:
                doc_lookup[doc_id] = doc
                rrf_scores[doc_id] = 0.0
            rrf_scores[doc_id] += 1.0 / (k + rank)

    sorted_doc_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [doc_lookup[doc_id] for doc_id in sorted_doc_ids[:top_n]]


def two_branch_rrf(
    dense_lists: List[List[Document]],
    bm25_lists: List[List[Document]],
    k: int = 60,
    top_n: int = 15,
    dense_weight: float = 0.5,
    bm25_weight: float = 0.5,
) -> List[Document]:
    """
    Balanced two-branch RRF: fuse within each branch first, then combine with weights.

    WHY NOT flat reciprocal_rank_fusion(dense_lists + bm25_lists)?
    With 1 dense list and N bm25 lists, the flat approach gives bm25 N× more
    total weight. For 1 dense + 3 bm25 lists, lexical contributes 3× the score.
    This function fuses within each branch first (one merged score per branch),
    then combines with explicit 50/50 weights.
    """
    def _fuse_branch(lists: List[List[Document]]) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        for doc_list in lists:
            for rank, doc in enumerate(doc_list, start=1):
                did = f"{doc.metadata.get('source', '')}::{doc.metadata.get('chunk_index', 0)}"
                scores[did] = scores.get(did, 0.0) + 1.0 / (k + rank)
        return scores

    dense_scores = _fuse_branch(dense_lists)
    bm25_scores = _fuse_branch(bm25_lists)

    # Build lookup (dense takes precedence for the same chunk)
    lookup: Dict[str, Document] = {}
    for doc_list in bm25_lists:
        for doc in doc_list:
            did = f"{doc.metadata.get('source', '')}::{doc.metadata.get('chunk_index', 0)}"
            lookup[did] = doc
    for doc_list in dense_lists:
        for doc in doc_list:
            did = f"{doc.metadata.get('source', '')}::{doc.metadata.get('chunk_index', 0)}"
            lookup[did] = doc

    combined: Dict[str, float] = {}
    for rank, (did, _) in enumerate(
        sorted(dense_scores.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        combined[did] = combined.get(did, 0.0) + dense_weight / (k + rank)

    for rank, (did, _) in enumerate(
        sorted(bm25_scores.items(), key=lambda x: x[1], reverse=True), start=1
    ):
        combined[did] = combined.get(did, 0.0) + bm25_weight / (k + rank)

    sorted_ids = sorted(combined.keys(), key=lambda x: combined[x], reverse=True)
    return [lookup[did] for did in sorted_ids[:top_n] if did in lookup]


def diversify_documents(
    documents: List[Document],
    top_n: int,
    max_per_source: int = 2,
) -> List[Document]:
    """Keep high-ranked evidence while preventing one file dominating context."""
    selected: List[Document] = []
    source_counts: dict[str, int] = {}
    for document in documents:
        source = document.metadata.get("source", "")
        if source_counts.get(source, 0) >= max_per_source:
            continue
        selected.append(document)
        source_counts[source] = source_counts.get(source, 0) + 1
        if len(selected) >= top_n:
            break
    return selected
