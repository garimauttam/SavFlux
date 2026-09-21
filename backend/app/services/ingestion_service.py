"""
ingestion_service.py — Handles everything from "raw code" to "searchable vectors".

The pipeline:
  GitHub URL / uploaded files
       ↓
  Clone or save to temp directory
       ↓
  Walk files, filter by extension (only code files)
       ↓
  Split into chunks (language-aware)
       ↓
  Embed each chunk via OpenAI
       ↓
  Upsert into ChromaDB with metadata (file path, language, line numbers)
"""

import hashlib
import os
import re
import shutil
import tempfile
import asyncio
from pathlib import Path
from typing import Callable, Optional

import git
from langchain.text_splitter import Language, RecursiveCharacterTextSplitter
from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_chroma import Chroma
from chromadb.config import Settings as ChromaSettings

from app.core.config import get_settings
from app.services.llm_factory import get_embedding_fn
from app.services.code_chunker import chunk_code_file

settings = get_settings()
ingestion_lock = asyncio.Lock()


def normalize_repo_url(repo_url: str) -> str:
    """Normalize GitHub repo URLs so repeated ingests hit the same index scope."""
    url = repo_url.strip()
    git_match = re.match(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$", url)
    if git_match:
        return f"https://github.com/{git_match.group('owner')}/{git_match.group('repo')}"
    if url.startswith("https://github.com/"):
        url = url.rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
    return url

# ── Language detection ────────────────────────────────────────────────────────
# Maps file extensions → LangChain Language enum
# Why? RecursiveCharacterTextSplitter has language-specific rules.
# For Python it splits on "class ", "def ", "\n\n" — respecting code structure.
# For plain text it just splits on newlines and spaces — wrong for code.
EXTENSION_TO_LANGUAGE = {
    ".py":   Language.PYTHON,
    ".js":   Language.JS,
    ".jsx":  Language.JS,
    ".ts":   Language.JS,     # TS shares JS splitter rules
    ".tsx":  Language.JS,
    ".go":   Language.GO,
    ".java": Language.JAVA,
    ".cpp":  Language.CPP,
    ".c":    Language.CPP,
    ".rs":   Language.RUST,
    ".rb":   Language.RUBY,
    ".md":   Language.MARKDOWN,
}

# Extensions we actually want to index — skip binaries, images, lock files
ALLOWED_EXTENSIONS = set(EXTENSION_TO_LANGUAGE.keys()) | {".txt", ".json", ".yaml", ".yml", ".env.example"}

# Directories that are never worth indexing
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}


def _get_vectorstore() -> Chroma:
    """
    ChromaDB vector store for write operations (ingestion, clear, metadata queries).

    NOT cached — intentionally creates a fresh client for each write operation.
    Unlike retrieval_service._get_vectorstore() which is a read-only singleton,
    writes here need a fresh connection to ensure ChromaDB's WAL (write-ahead log)
    is flushed and visible to subsequent read clients.

    get_embedding_fn() is provider-aware: OpenAI embeddings or local MiniLM
    depending on LLM_PROVIDER. MUST match whatever was used at index time.

    WHY client= INSTEAD OF persist_directory= + client_settings=?
    langchain-chroma==0.1.1 internally calls chromadb.Client(_client_settings).
    In chromadb==0.5.0, chromadb.Client() was changed to always create an
    ephemeral (in-memory) client regardless of Settings.is_persistent or
    persist_directory — only chromadb.PersistentClient(path=...) creates a
    durable client. Passing a pre-built PersistentClient via client= bypasses
    the broken auto-creation path and ensures all writes land on disk.
    """
    import chromadb as _chromadb
    persistent_client = _chromadb.PersistentClient(
        path=settings.chroma_persist_directory,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    return Chroma(
        client=persistent_client,
        collection_name=settings.chroma_collection_name,
        embedding_function=get_embedding_fn(),
    )


def _collect_files(root: Path) -> list[Path]:
    """
    Walk the directory tree and return only files worth indexing.
    We skip huge dirs (node_modules) that would waste tokens and money.
    """
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Modify dirnames IN PLACE — this tells os.walk not to recurse into them
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            if fpath.suffix in ALLOWED_EXTENSIONS:
                # Skip files over 500KB — likely auto-generated or minified
                if fpath.stat().st_size < 500_000:
                    collected.append(fpath)

    return collected


def _load_and_split(
    files: list[Path],
    repo_url: str,
    source_root: Path | None = None,
) -> list[Document]:
    """
    Load each file and split into chunks.

    Source files are chunked on AST boundaries where a parser exists — one chunk
    per function/class — because a chunk that is exactly one symbol embeds as that
    symbol instead of as a fragment of two. See `code_chunker.py`.

    Files with no grammar (markdown, JSON, YAML), grammars whose wheel is not
    installed, and files that would not parse fall back to
    RecursiveCharacterTextSplitter exactly as before.

    chunk_overlap=200 means adjacent chunks share 200 characters.
    This prevents a function signature being in chunk N and its body in chunk N+1
    with no overlap — the model would see the body without knowing what function it's in.
    """
    documents = []

    for fpath in files:
        ext = fpath.suffix
        language = EXTENSION_TO_LANGUAGE.get(ext)

        try:
            # TextLoader reads the file as a string
            loader = TextLoader(str(fpath), encoding="utf-8", autodetect_encoding=True)
            raw_docs = loader.load()
        except Exception:
            # Some files have weird encodings — skip them
            continue

        # Compute content hash once per file — used for delta re-indexing.
        # SHA-256 of the raw file bytes is stable across runs for identical content.
        file_bytes = b"".join(d.page_content.encode() for d in raw_docs)
        content_hash = hashlib.sha256(file_bytes).hexdigest()[:16]  # 16 hex chars is plenty

        source_text = raw_docs[0].page_content if raw_docs else ""

        # GitHub clones are temporary, so use a stable repo-relative source ID.
        # The physical path remains available for the current ingestion only.
        source_id = str(fpath)
        if source_root is not None:
            source_id = f"{repo_url}::{fpath.relative_to(source_root).as_posix()}"

        # ── AST-boundary chunking ─────────────────────────────────────────────
        # One chunk per top-level function/class — far better retrieval precision
        # than character-based splitting for code Q&A, because a chunk that is
        # exactly one symbol embeds as that symbol instead of as a fragment.
        #
        # `chunk_code_file` is the single routing point: Python goes through
        # ast.parse (stdlib, exact, already tested), every other language through
        # tree-sitter. Keeping the decision there rather than here means a caller
        # cannot accidentally pair a file with the wrong parser.
        #
        # An empty list means "no AST chunking applies" — an unsupported type, a
        # grammar whose wheel is not installed, or a file that would not parse —
        # and we fall through to the character splitter, which is exactly the
        # behaviour that predates all of this.
        if source_text:
            ast_chunks = chunk_code_file(
                source=source_text,
                file_path=source_id,
                file_name=fpath.name,
                language=ext.lstrip("."),
                repo_url=repo_url,
                content_hash=content_hash,
            )
            if ast_chunks:
                documents.extend(ast_chunks)
                continue

        # ── Fallback: RecursiveCharacterTextSplitter ──────────────────────────
        if language:
            # Language-aware splitter: knows to split on class/function boundaries
            splitter = RecursiveCharacterTextSplitter.from_language(
                language=language,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )
        else:
            # Generic splitter for JSON, YAML, etc.
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
            )

        chunks = splitter.split_documents(raw_docs)

        # Enrich metadata — this is what shows up in the "Sources" panel in the UI
        # Without metadata, you'd get answers but no way to cite where they came from
        #
        # start_line/end_line give non-Python files the same line-precise citations
        # the AST chunker produces for Python. RecursiveCharacterTextSplitter does
        # not report offsets, so we locate each chunk by scanning forward through
        # the source. `search_cursor` makes this a single linear pass over the file
        # rather than a rescan from position 0 per chunk (O(n) not O(n·chunks)), and
        # it also stops a repeated block — a common `import` line, a duplicated
        # config stanza — from matching an earlier occurrence and citing the wrong
        # region of the file.
        search_cursor = 0
        for i, chunk in enumerate(chunks):
            start_line: int | None = None
            end_line: int | None = None
            if source_text:
                offset = source_text.find(chunk.page_content, search_cursor)
                if offset == -1:
                    # Splitters may strip surrounding whitespace, so an exact match
                    # can fail. Retry on the stripped form before giving up.
                    stripped = chunk.page_content.strip()
                    offset = source_text.find(stripped, search_cursor) if stripped else -1
                if offset != -1:
                    start_line = source_text.count("\n", 0, offset) + 1
                    last_char = min(offset + len(chunk.page_content), len(source_text)) - 1
                    end_line = max(start_line, source_text.count("\n", 0, last_char) + 1)
                    # Advance past this chunk, but never past the next chunk's start:
                    # overlapping windows legitimately revisit earlier characters.
                    search_cursor = offset + max(1, len(chunk.page_content) - settings.chunk_overlap)

            chunk.metadata.update({
                "source": source_id,            # stable ID for cloned repos
                "physical_path": str(fpath),    # temporary path, if still available
                "file_name": fpath.name,         # just "auth.py"
                "language": ext.lstrip("."),     # "py", "js", etc.
                "repo_url": repo_url,
                "chunk_index": i,
                "content_hash": content_hash,   # for incremental delta re-indexing
            })
            # Only attach when resolved — ChromaDB rejects None metadata values,
            # and a missing span is better than a wrong one.
            if start_line is not None and end_line is not None:
                chunk.metadata["start_line"] = start_line
                chunk.metadata["end_line"] = end_line

        documents.extend(chunks)

    return documents


async def ingest_github_repo(
    repo_url: str,
    branch: Optional[str] = None,
    progress_callback: Optional[Callable] = None,
) -> dict:
    """
    Main ingestion entry point for GitHub repos.

    1. Clone to a temp directory (auto-deleted when done)
    2. Collect + split files
    3. Batch embed + store in ChromaDB
    Returns a summary dict for the API response.

    branch: when provided, clones that specific branch. When None, git uses
            the repo's default branch (whatever HEAD points to).
    """
    repo_url = normalize_repo_url(repo_url)
    tmp_dir = tempfile.mkdtemp()

    try:
        # ── Step 1: Clone ─────────────────────────────────────────────────────
        clone_msg = f"Cloning {repo_url}" + (f" @ {branch}" if branch else "") + "..."
        if progress_callback:
            await progress_callback({"step": "cloning", "message": clone_msg})

        # Build kwargs: only add `branch` when explicitly requested.
        # git.Repo.clone_from accepts branch= as the ref to checkout.
        clone_kwargs: dict = {"depth": 1}  # shallow clone — latest commit only
        if branch:
            clone_kwargs["branch"] = branch

        # We run git.Repo.clone_from in a thread because it's blocking I/O
        # asyncio.to_thread prevents it from blocking the FastAPI event loop
        await asyncio.to_thread(
            git.Repo.clone_from,
            repo_url,
            tmp_dir,
            **clone_kwargs,
        )

        # ── Step 2: Collect files ─────────────────────────────────────────────
        if progress_callback:
            await progress_callback({"step": "scanning", "message": "Scanning files..."})

        files = _collect_files(Path(tmp_dir))

        if not files:
            return {"status": "error", "message": "No indexable code files found in this repo."}

        if progress_callback:
            await progress_callback({
                "step": "splitting",
                "message": f"Found {len(files)} files. Splitting into chunks...",
            })

        # ── Step 3: Split into chunks ─────────────────────────────────────────
        documents = await asyncio.to_thread(
            _load_and_split, files, repo_url, Path(tmp_dir)
        )

        if progress_callback:
            await progress_callback({
                "step": "embedding",
                "message": f"Embedding {len(documents)} chunks (this may take a minute)...",
            })

        # ── Step 4: Incremental delta embed + store in ChromaDB ───────────────
        # Delta re-indexing: only embed files whose content has changed since
        # the last ingest. This makes re-indexing 10x faster for repos where
        # only a few files changed between runs.
        #
        # Algorithm:
        #   1. Fetch all existing (source → content_hash) pairs for this repo
        #   2. Compare with hashes of files collected this run
        #   3. Skip files whose hash hasn't changed (no re-embedding needed)
        #   4. Delete IDs for removed/changed files before adding new chunks
        #   5. Embed only the new/changed chunks
        vectorstore = _get_vectorstore()

        # Build a map: source_path → {hash, ids} from the current index.
        # Normalize repo URLs while reading so older entries such as trailing
        # slash/.git variants are treated as the same repo and cleaned up.
        indexed_map: dict[str, dict] = {}  # source → {"hash": str, "ids": [str]}
        try:
            existing = vectorstore._collection.get(include=["metadatas"])
            for eid, emeta in zip(
                existing.get("ids") or [],
                existing.get("metadatas") or [],
            ):
                if normalize_repo_url(emeta.get("repo_url", "")) != repo_url:
                    continue
                src = emeta.get("source", "")
                chash = emeta.get("content_hash", "")
                if src not in indexed_map:
                    indexed_map[src] = {"hash": chash, "ids": []}
                indexed_map[src]["ids"].append(eid)
        except Exception:
            pass  # Collection may not exist yet on first run — fine

        # Partition documents into "need embedding" vs "unchanged"
        new_docs: list[Document] = []
        stale_ids: list[str] = []
        seen_sources: set[str] = set()

        for doc in documents:
            src = doc.metadata["source"]
            chash = doc.metadata["content_hash"]
            if src not in seen_sources:
                seen_sources.add(src)
                if src in indexed_map:
                    if indexed_map[src]["hash"] == chash:
                        # File unchanged — keep existing vectors, skip re-embedding
                        continue
                    else:
                        # File changed — mark old vectors for deletion
                        stale_ids.extend(indexed_map[src]["ids"])
            new_docs.append(doc)

        # Delete IDs for files that no longer exist in the repo (removed files)
        current_sources = {
            f"{repo_url}::{f.relative_to(Path(tmp_dir)).as_posix()}"
            for f in files
        }
        for src, info in indexed_map.items():
            if src not in current_sources:
                stale_ids.extend(info["ids"])

        # Purge stale vectors (changed + removed files)
        if stale_ids:
            try:
                vectorstore._collection.delete(ids=stale_ids)
            except Exception:
                pass

        # Count skipped files as: total files collected minus unique source paths
        # that actually appear in new_docs (files that needed re-embedding).
        # Using seen_sources (populated during the partition loop above) is correct:
        # it holds exactly one entry per file that was NOT skipped.
        # Using a set over new_docs chunks would undercount if the same source
        # appears in multiple chunks but fewer chunks than total files.
        files_skipped = len(files) - len(seen_sources)

        if progress_callback:
            msg = (
                f"Embedding {len(new_docs)} new/changed chunks"
                + (f" ({files_skipped} files unchanged, skipped)" if files_skipped else "")
                + "..."
            )
            await progress_callback({"step": "embedding", "message": msg})

        # Embed and insert in batches to provide incremental progress updates
        # and prevent reverse-proxy timeout (SSE keepalive).
        BATCH_SIZE = 100
        for i in range(0, len(new_docs), BATCH_SIZE):
            batch = new_docs[i:i + BATCH_SIZE]
            await asyncio.to_thread(
                vectorstore.add_documents,
                documents=batch,
            )
            if progress_callback:
                processed = min(i + BATCH_SIZE, len(new_docs))
                await progress_callback({
                    "step": "embedding",
                    "message": f"Embedding chunks: {processed}/{len(new_docs)}...",
                })

        # Invalidate the read-singleton in retrieval_service so the next query
        # opens a fresh ChromaDB connection that sees the newly written data.
        # Without this, the cached Chroma instance holds a handle to the old
        # SQLite WAL state and may return stale (or empty) results immediately
        # after ingestion completes.
        try:
            from app.services.retrieval_service import _get_vectorstore as _rv
            _rv.cache_clear()
        except Exception:
            pass  # non-fatal — next cold start will fix it

        # Invalidate the BM25 disk cache — the collection just changed.
        # The next query will rebuild the BM25 index from the updated ChromaDB
        # and re-save it to disk automatically.
        try:
            from app.services.retrieval_service import invalidate_bm25_cache
            invalidate_bm25_cache()
        except Exception:
            pass

        chunks_added = len(new_docs)
        if progress_callback:
            skip_note = f" ({files_skipped} unchanged)" if files_skipped else ""
            await progress_callback({
                "step": "done",
                "message": f"✅ Indexed {chunks_added} chunks from {len(files) - files_skipped} files{skip_note}.",
            })

        # Trust ledger: record the exact upstream commit this index was
        # built from, so answers can be verified against a known revision.
        # Best-effort — verification must never break ingestion.
        try:
            head_sha = git.Repo(tmp_dir).head.commit.hexsha
            from app.services.trust_service import record_index
            record_index(repo_url, head_sha, files_indexed=len(files) - files_skipped)
        except Exception:
            pass

        # Time machine: seed the bare mirror from this temp clone (local copy,
        # no network). Powers per-file git log/blame without re-cloning.
        try:
            from app.services.history_service import seed_mirror_from_tmp
            await asyncio.to_thread(seed_mirror_from_tmp, repo_url, tmp_dir)
        except Exception:
            pass

        return {
            "status": "success",
            "repo_url": repo_url,
            "files_indexed": len(files) - files_skipped,
            "files_skipped": files_skipped,
            "chunks_created": chunks_added,
        }

    finally:
        # Always clean up the temp clone — repos can be hundreds of MB
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def ingest_uploaded_files(files_content: list[tuple[str, bytes]]) -> dict:
    """
    Ingestion for direct file uploads (when user doesn't have a GitHub URL).
    files_content: list of (filename, raw_bytes) tuples

    WHY _get_vectorstore() + add_documents() instead of Chroma.from_documents()?
    In chromadb==0.5.0, Chroma.from_documents(client_settings=...) internally calls
    chromadb.Client() which is always ephemeral (in-memory). The uploaded chunks are
    written to a throwaway store and lost the moment the call returns.
    Using _get_vectorstore() creates a PersistentClient — the same durable SQLite-backed
    store used by the GitHub ingestion path — so uploads survive restarts.
    """
    tmp_dir = tempfile.mkdtemp()

    try:
        # Write uploaded bytes to temp files so we can reuse the same pipeline
        paths = []
        for filename, content in files_content:
            fpath = Path(tmp_dir) / filename
            fpath.write_bytes(content)
            paths.append(fpath)

        documents = await asyncio.to_thread(_load_and_split, paths, "uploaded_files")

        # Guard: if every file failed to parse, documents will be empty.
        if not documents:
            return {
                "status": "error",
                "message": "No content could be extracted from the uploaded files. "
                           "Check that the files are valid text/code files.",
            }

        # Use the same PersistentClient-backed store as ingest_github_repo.
        vectorstore = _get_vectorstore()
        BATCH_SIZE = 100
        for i in range(0, len(documents), BATCH_SIZE):
            batch = documents[i:i + BATCH_SIZE]
            await asyncio.to_thread(vectorstore.add_documents, documents=batch)

        # Invalidate read singleton — same reason as in ingest_github_repo
        try:
            from app.services.retrieval_service import _get_vectorstore as _rv
            _rv.cache_clear()
        except Exception:
            pass

        # Invalidate BM25 disk cache — collection just changed
        try:
            from app.services.retrieval_service import invalidate_bm25_cache
            invalidate_bm25_cache()
        except Exception:
            pass

        return {
            "status": "success",
            "files_indexed": len(paths),
            "chunks_created": len(documents),
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def clear_index(repo_url: str | None = None) -> dict:
    """
    Delete vectors from ChromaDB.

    repo_url=None  → wipe the entire collection (full reset)
    repo_url=<url> → delete only chunks that belong to that repo

    WHY NOT JUST DELETE THE CHROMA FOLDER?
    Deleting the folder works but is OS-dependent and not thread-safe if another
    request is reading. ChromaDB's own delete() API is the correct way — it handles
    locking and keeps the SQLite WAL consistent.

    WHY _get_raw_collection() INSTEAD OF _get_vectorstore()?
    _get_vectorstore() calls get_embedding_fn() which imports sentence-transformers
    for non-OpenAI providers. If that package is absent the whole endpoint crashes
    with 500. Clear only needs raw ChromaDB metadata + delete — no embeddings.
    """
    collection = await asyncio.to_thread(_get_raw_collection)

    def _bust_read_cache():
        """Invalidate the retrieval singleton and BM25 disk cache so next query sees the mutations."""
        try:
            from app.services.retrieval_service import _get_vectorstore as _rv
            _rv.cache_clear()
        except Exception:
            pass
        try:
            from app.services.retrieval_service import invalidate_bm25_cache
            invalidate_bm25_cache()
        except Exception:
            pass

    if repo_url:
        normalized_repo_url = normalize_repo_url(repo_url)
        # Targeted delete — only this repo's chunks
        try:
            existing = collection.get(include=["metadatas"])
            ids = [
                eid
                for eid, metadata in zip(existing.get("ids") or [], existing.get("metadatas") or [])
                if normalize_repo_url(metadata.get("repo_url", "")) == normalized_repo_url
            ]
            if ids:
                collection.delete(ids=ids)
            _bust_read_cache()
            return {
                "status": "success",
                "message": f"Cleared {len(ids)} chunks for repo: {normalized_repo_url}",
                "deleted": len(ids),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    else:
        # Full reset — fetch all IDs then delete by ID.
        # WHY NOT collection.delete(where={})?
        # The behaviour of an empty `where` filter changed between ChromaDB versions:
        # in 0.4.x it deletes nothing; in 0.5.x it deletes everything.
        # Fetching IDs first and deleting by ID is correct in all versions.
        try:
            all_items = collection.get(include=[])  # IDs only — no embeddings fetched
            ids = all_items.get("ids") or []
            if ids:
                collection.delete(ids=ids)
            _bust_read_cache()
            return {
                "status": "success",
                "message": f"Cleared entire index ({len(ids)} chunks deleted).",
                "deleted": len(ids),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}


def _get_raw_collection():
    """
    Returns a raw ChromaDB collection without requiring an embedding function.

    WHY NOT USE _get_vectorstore() HERE?
    _get_vectorstore() calls get_embedding_fn() which imports sentence-transformers for
    non-OpenAI providers. If sentence-transformers is not installed, get_indexed_repos()
    crashes with ImportError → HTTP 500. Metadata listing never needs embeddings.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    client = chromadb.PersistentClient(
        path=settings.chroma_persist_directory,
        settings=ChromaSettings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(settings.chroma_collection_name)


def get_indexed_repos_sync() -> list[dict]:
    """
    Blocking core of :func:`get_indexed_repos`.

    WHY A SEPARATE SYNC ENTRY POINT?
    Synchronous callers (e.g. activity_service, which is itself invoked from a
    worker thread) previously reached for `asyncio.run(get_indexed_repos())`.
    That raises `RuntimeError: asyncio.run() cannot be called from a running
    event loop` whenever the caller is already inside one — which is always true
    under uvicorn. The failure was swallowed by a bare `except`, so indexed
    repositories silently never appeared in the activity feed in production.

    Exposing the blocking implementation lets sync callers use it directly and
    async callers wrap it in `asyncio.to_thread` — nobody needs a nested loop.
    """
    collection = _get_raw_collection()
    results = collection.get(include=["metadatas"])
    metadatas = results.get("metadatas") or []

    repo_counts: dict[str, int] = {}
    for m in metadatas:
        url = normalize_repo_url((m or {}).get("repo_url", ""))
        if url:
            repo_counts[url] = repo_counts.get(url, 0) + 1

    return [
        {"repo_url": url, "chunk_count": count}
        for url, count in sorted(repo_counts.items())
    ]


async def get_indexed_repos() -> list[dict]:
    """
    Returns all unique repo URLs currently in the vector store, with chunk counts.
    Used by the frontend's active-repo selector.

    Runs the blocking ChromaDB read in a worker thread so the event loop stays
    free to serve other requests.
    """
    return await asyncio.to_thread(get_indexed_repos_sync)
