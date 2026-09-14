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
from typing import List, Dict, Any, Optional
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
    Splits on camelCase, snake_case, and non-alphanumeric boundaries.
    """
    # Split camelCase: "getUserById" -> "get User By Id"
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # Split non-alphanumerics
    tokens = re.findall(r"[A-Za-z0-9_]+", s.lower())
    return [t for t in tokens if len(t) > 1]


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
            # Create a unique key based on source and chunk content snippet
            doc_id = f"{doc.metadata.get('source', '')}::{doc.metadata.get('chunk_index', 0)}::{doc.page_content[:50]}"
            if doc_id not in doc_lookup:
                doc_lookup[doc_id] = doc
                rrf_scores[doc_id] = 0.0
            rrf_scores[doc_id] += 1.0 / (k + rank)

    sorted_doc_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [doc_lookup[doc_id] for doc_id in sorted_doc_ids[:top_n]]


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
