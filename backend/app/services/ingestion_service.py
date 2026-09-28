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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import git
from langchain.text_splitter import Language, RecursiveCharacterTextSplitter
from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_chroma import Chroma
from chromadb.config import Settings as ChromaSettings

from app.core.config import get_settings
from app.core.paths import data_dir
from app.services.llm_factory import LOCAL_EMBEDDING_PROVIDERS, get_embedding_fn
from app.services.code_chunker import chunk_code_file
from app.services.parent_child import children_of_all

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
        path=str(data_dir()),
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


# ── What a stored vector is a function of ──────────────────────────────────────
def index_build_id() -> str:
    """
    Fingerprint of everything that decides what the vectors in this index MEAN.

    `content_hash` answers "did the file change?", and that is not the only question: a
    vector is the product of the file *and* the pipeline that read it. Skipping
    re-embedding because the bytes match, while the embedder or the windowing has changed,
    leaves one collection holding two incompatible vector spaces — a query embedded by
    model B scored against vectors from model A is not stale retrieval but incoherent
    retrieval. Worse, it stays that way forever: every later run sees matching hashes and
    skips again. `get_embedding_fn`'s "re-index after switching" warning is only
    enforceable if the index remembers what it was indexed with, which is what this id
    is stamped into every chunk for.

    The windowing constants are part of the identity for the same reason: the text handed
    to the embedder is what the vector describes, and `children_of_all` is called with
    these defaults, so a changed constant changes the index even when no file did.

    Read from the live module attributes, and 16 hex chars like content_hash. No fallback
    value on purpose: settings that cannot be read should stop the ingest, not have an
    identity invented for them.
    """
    from app.services import parent_child

    s = get_settings()
    # Which embedder *implementation* reads the file, not which chat provider is
    # configured. `ollama` and `deepseek` both embed with the local model named below, so
    # moving between them must not cost a re-index — the README promises that in so many
    # words, and a repo pays minutes per thousand files for a switch that changes no
    # vector. A paid provider is a different embedder whatever its model is named, so the
    # path is what belongs here, and `openai` vs a local model called the same thing stay
    # distinguishable.
    embedder_path = (
        "local" if s.llm_provider in LOCAL_EMBEDDING_PROVIDERS else "api"
    )
    embedder = (
        s.openai_embedding_model if embedder_path == "api" else s.embedding_model
    )
    parts = [
        f"embedder_path={embedder_path}",
        f"embedder={embedder}",
        f"chunk_size={s.chunk_size}",
        f"chunk_overlap={s.chunk_overlap}",
        f"window={parent_child.EMBED_WINDOW_CHARS}",
        f"window_overlap={parent_child.CHILD_OVERLAP_CHARS}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class DeltaPlan:
    """
    What the delta pass decided, in the units the progress messages speak.

    The counts are per *file*, not per chunk, because that is the unit the skip decision is
    made in and the unit a user can check: "12 chunks" tells you nothing about whether the
    4 minutes of embedding you just waited for were needed.
    """

    new_docs: list[Document] = field(default_factory=list)
    stale_ids: list[str] = field(default_factory=list)
    sources_indexed: set[str] = field(default_factory=set)
    files_skipped: int = 0
    files_changed: int = 0
    files_new: int = 0
    #: Re-embedded although their bytes did not change, because the pipeline did.
    files_rebuilt: int = 0

    @property
    def files_touched(self) -> int:
        """Files whose vectors this run wrote — the honest denominator for a summary."""
        return len(self.sources_indexed)


def build_indexed_map(ids, metadatas, repo_url: str) -> dict[str, dict]:
    """
    Group what the collection already holds by file, for one repo.

    Split out of the ingest path so the read half of delta indexing is testable without a
    Chroma. Repo URLs are normalised on the way in, so `repo/` and `repo.git` are the same
    repo and get cleaned up rather than duplicated.

    A file's identity is collected as a SET of hashes and a set of build ids rather than
    "the first chunk's", because every chunk of one file is supposed to carry the same
    pair — so more than one value is not a detail to pick a winner from, it is evidence
    that the file's rows were written across two different runs (an ingest that died
    halfway through a rewrite). Trusting one of the two would then declare a half-updated
    file unchanged forever. `plan_delta` re-embeds any file whose stored rows disagree.

    Entries written before `index_build_id` existed carry no stamp. They read as "", which
    is what makes the first run after the upgrade rebuild them instead of trusting a vector
    nobody can attribute.
    """
    indexed_map: dict[str, dict] = {}
    for eid, emeta in zip(ids or [], metadatas or []):
        if normalize_repo_url(emeta.get("repo_url", "")) != repo_url:
            continue
        src = emeta.get("source", "")
        if src not in indexed_map:
            indexed_map[src] = {"hashes": set(), "builds": set(), "ids": []}
        entry = indexed_map[src]
        entry["hashes"].add(emeta.get("content_hash", ""))
        entry["builds"].add(emeta.get("index_build", ""))
        entry["ids"].append(eid)
    return indexed_map


def plan_delta(
    *,
    indexed_map: dict[str, dict],
    documents: list[Document],
    current_sources: set[str],
    build_id: str,
) -> DeltaPlan:
    """
    Decide which files need vectors again, and which ids have to go.

    A file is skipped only when its bytes AND the pipeline that produced its vectors match.
    Anything else re-embeds the whole file — a chunk that keeps an old vector while its
    siblings get new ones is the same incoherence in a subtler form, and harder to notice.

    `stale_ids` therefore covers three cases, and the reason each one matters is different:
    a changed file's old rows (they describe old bytes), a rebuilt file's old rows (they
    describe the old *pipeline*), and a removed file's rows (nothing describes them).
    Deleting is not optional in any of them: an orphan row stays searchable forever and
    shows up as a citation to a file the repo no longer has.
    """
    plan = DeltaPlan()
    decided: dict[str, bool] = {}  # source -> needs embedding; the first chunk decides

    for doc in documents:
        src = doc.metadata["source"]
        chash = doc.metadata["content_hash"]
        if src not in decided:
            known = indexed_map.get(src)  # {"hashes": set, "builds": set, "ids": list}
            if known is None:
                decided[src] = True
                plan.files_new += 1
            elif known["hashes"] != {chash} or known["builds"] != {build_id}:
                # Two reasons for the same action, counted apart because the user needs to
                # know which one they caused: "you changed this file" versus "we changed
                # how we read files". A file whose stored rows disagree with each other
                # counts as changed rather than rebuilt — from here it looks exactly like
                # that, since the bytes on disk match only one of the two hashes.
                decided[src] = True
                if known["hashes"] != {chash}:
                    plan.files_changed += 1
                else:
                    plan.files_rebuilt += 1
                plan.stale_ids.extend(known["ids"])
            else:
                decided[src] = False
                plan.files_skipped += 1
        if decided[src]:
            plan.new_docs.append(doc)
            plan.sources_indexed.add(src)

    for src, info in indexed_map.items():
        if src not in current_sources:
            plan.stale_ids.extend(info["ids"])

    return plan


def _stamp_build(docs: list[Document], build_id: str) -> list[Document]:
    """
    Record which pipeline produced these chunks, on the chunks themselves.

    One call site, at the return of `_load_and_split`, rather than one per chunker: the
    function has two exits (AST chunks and character-split chunks) and a stamp that only
    one of them applies says "unattributed" about the other, which is a silent hole in the
    exact guarantee the stamp exists to provide. Stamping at the exit covers whatever the
    routing turns out to be, including a third chunker added later.

    `plan_delta` treats an unattributed chunk as needing a rebuild — the safe direction to
    be wrong in, since a rebuild costs embedding and a false skip costs correctness.
    """
    for doc in docs:
        doc.metadata["index_build"] = build_id
    return docs


def _load_and_split(
    files: list[Path],
    repo_url: str,
    source_root: Path | None = None,
    build_id: str = "",
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

    return _stamp_build(documents, build_id) if build_id else documents


def _to_index_rows(documents: list[Document]) -> list[Document]:
    """
    The rows Chroma actually stores: one per embed window, not one per chunk.

    Chroma holds what the *retriever* searches, and the retriever searches windows
    because the embedder silently truncates at 256 tokens. The whole chunk travels
    in each row's metadata instead and is what the LLM is handed at read time by
    `parent_context()`.

    Everything else in ingestion keeps working on parents. The delta logic compares
    one content_hash per file, and the progress messages count chunks, because that
    is the unit a user can reason about ("12 chunks from 3 files"). Only the rows
    handed to Chroma change, which is why this is a function with a name rather
    than the same one-line call pasted at two call sites — the third call site is
    the one that gets missed.

    A chunk that already fits its window comes back as a single row that is a copy
    of it, so there is no "sometimes a parent, sometimes a child" branch anywhere
    downstream for `parent_context` to be forgotten on.
    """
    return children_of_all(documents)


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

        # Private repositories need the connected account's credentials. They
        # arrive as a git config option rather than a token in the URL, so the
        # secret never lands in the clone's .git/config or in a `git remote -v`
        # that someone pastes into a bug report. With nothing connected this is
        # an empty dict and the clone behaves exactly as it did before.
        from app.services.github_service import auth_clone_kwargs

        clone_kwargs.update(auth_clone_kwargs())

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
        # Computed once, per ingest, and stamped on every chunk: "we already have vectors
        # for this file" is only a claim about the same embedding pipeline if the index
        # says which pipeline it holds. See `index_build_id`.
        build_id = index_build_id()
        documents = await asyncio.to_thread(
            _load_and_split, files, repo_url, Path(tmp_dir), build_id
        )

        # ── Step 4: Incremental delta embed + store in ChromaDB ───────────────
        # Only re-embed files whose bytes changed, or whose vectors came from a different
        # pipeline. No progress message here: "Embedding 1,844 chunks (this may take a
        # minute)" was printed before the delta pass had decided anything, so it announced
        # the whole corpus on a run that skipped all of it. The one below reports the number
        # that is actually about to be embedded.
        vectorstore = _get_vectorstore()

        indexed_map: dict[str, dict] = {}
        try:
            existing = vectorstore._collection.get(include=["metadatas"])
            indexed_map = build_indexed_map(
                existing.get("ids"), existing.get("metadatas"), repo_url
            )
        except Exception:
            pass  # Collection may not exist yet on first run — fine

        # Repo-relative source IDs, as the index stores them — not temp paths. Taken from
        # `files` rather than from `documents` deliberately: a file that exists but produced
        # no chunks this time (empty, or unreadable) still exists, and treating it as
        # removed would delete good vectors over a transient read failure.
        current_sources = {
            f"{repo_url}::{f.relative_to(Path(tmp_dir)).as_posix()}"
            for f in files
        }
        plan = plan_delta(
            indexed_map=indexed_map,
            documents=documents,
            current_sources=current_sources,
            build_id=build_id,
        )
        new_docs = plan.new_docs
        stale_ids = plan.stale_ids

        if progress_callback:
            skipped_note = (
                f" ({plan.files_skipped} files unchanged, skipped)" if plan.files_skipped else ""
            )
            # Worth a message of its own because it is the one surprise a user cannot
            # derive from their own diff: the files did not change, the reader of them did.
            rebuild_note = (
                f" ({plan.files_rebuilt} re-embedded: embedding model or chunking changed)"
                if plan.files_rebuilt else ""
            )
            await progress_callback({
                "step": "embedding",
                "message": (
                    f"Embedding {len(new_docs)} new/changed chunks{rebuild_note}"
                    f"{skipped_note}..."
                    if new_docs else
                    "Nothing to embed — every chunk is already in the index"
                    f"{skipped_note}."
                ),
            })

        # Purge stale vectors (changed + rebuilt + removed files)
        if stale_ids:
            try:
                vectorstore._collection.delete(ids=stale_ids)
            except Exception:
                pass

        # The skipped count used to be derived here as `len(files) - len(seen_sources)`,
        # with `seen_sources` populated for every file seen — including the skipped ones,
        # because the `continue` happened after the add. For any repo where each file
        # produced at least one chunk that is always 0: the "M files unchanged, skipped"
        # message could never print, and a file that produced no chunks at all was counted
        # as skipped-for-reuse, which is a different fact. The count now comes from the
        # decision itself (`plan.files_skipped`), reported once, above.

        # Embed and insert in batches to provide incremental progress updates
        # and prevent reverse-proxy timeout (SSE keepalive).
        #
        # BATCH_SIZE stays on parents for progress granularity. One batch can
        # therefore hand Chroma several hundred rows; that is not what limits
        # embedding throughput — EMBEDDING_BATCH_SIZE is.
        BATCH_SIZE = 100
        rows_indexed = 0
        for i in range(0, len(new_docs), BATCH_SIZE):
            batch = new_docs[i:i + BATCH_SIZE]
            rows = await asyncio.to_thread(_to_index_rows, batch)
            await asyncio.to_thread(
                vectorstore.add_documents,
                documents=rows,
            )
            rows_indexed += len(rows)
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
        # A file collected but yielding no chunks is neither "indexed" nor "skipped for
        # reuse". It gets its own number because the two it used to be folded into are the
        # ones users compare: without it, "indexed 0 chunks from 3 files" and "12 files
        # skipped" can both be true of a repo whose 12 files simply did not parse.
        files_with_chunks = len({doc.metadata["source"] for doc in documents})
        files_without_chunks = max(0, len(files) - files_with_chunks)
        if progress_callback:
            skip_note = f" ({plan.files_skipped} unchanged)" if plan.files_skipped else ""
            chunk_note = (
                f" ({files_without_chunks} produced no chunks)" if files_without_chunks else ""
            )
            # Report vectors only when they differ from chunks. "Indexed 12 chunks
            # (31 vectors)" is honest but reads as jargon when both numbers are
            # 12; the distinction only earns its words when there is one.
            vectors_note = (
                f" ({rows_indexed} vectors)" if rows_indexed != chunks_added else ""
            )
            await progress_callback({
                "step": "done",
                "message": (
                    f"✅ Indexed {chunks_added} chunks{vectors_note} from "
                    f"{plan.files_touched} files{skip_note}{chunk_note}."
                ),
            })

        # Trust ledger: record the exact upstream commit this index was
        # built from, so answers can be verified against a known revision.
        # Best-effort — verification must never break ingestion.
        try:
            head_sha = git.Repo(tmp_dir).head.commit.hexsha
            from app.services.trust_service import record_index
            record_index(
                repo_url,
                head_sha,
                # Files this repo has vectors for after this run — written now or carried
                # over unchanged. Not `len(files)`, which also counts files that produced
                # no chunks and files whose read failed.
                files_indexed=plan.files_touched + plan.files_skipped,
            )
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
            # Files this repo has vectors for once this run is done: written now plus
            # carried over unchanged. `len(files)`, the old value, also counted files that
            # produced no chunks and files whose read failed — so it said "40 files
            # indexed" about an index holding 12, and the trust ledger repeated it.
            "files_indexed": plan.files_touched + plan.files_skipped,
            "files_skipped": plan.files_skipped,
            # Re-embedded with matching bytes because the pipeline changed, and files that
            # were collected but yielded nothing. Both are the kind of number that explains
            # a surprise, so both travel in the response rather than only in the message.
            "files_rebuilt": plan.files_rebuilt,
            "files_without_chunks": files_without_chunks,
            "index_build": build_id,
            # chunks_created keeps meaning chunks. vectors_created is additive, so
            # a caller already parsing chunks_created does not start seeing a
            # different number under the same key.
            "chunks_created": chunks_added,
            "vectors_created": rows_indexed,
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

        # Stamped like the GitHub path, because both write to one collection: "every row
        # says what built it" is only a guarantee if it holds for whoever wrote the row.
        build_id = index_build_id()
        documents = await asyncio.to_thread(
            _load_and_split, paths, "uploaded_files", None, build_id
        )

        # Guard: if every file failed to parse, documents will be empty.
        if not documents:
            return {
                "status": "error",
                "message": "No content could be extracted from the uploaded files. "
                           "Check that the files are valid text/code files.",
            }

        # Use the same PersistentClient-backed store as ingest_github_repo.
        #
        # Same row shape as the GitHub path on purpose. This writes to the same
        # collection, so indexing whole chunks here would leave two shapes in one
        # index and make `parent_context` correct on only one of them.
        vectorstore = _get_vectorstore()
        BATCH_SIZE = 100
        rows_indexed = 0
        for i in range(0, len(documents), BATCH_SIZE):
            batch = documents[i:i + BATCH_SIZE]
            rows = await asyncio.to_thread(_to_index_rows, batch)
            await asyncio.to_thread(vectorstore.add_documents, documents=rows)
            rows_indexed += len(rows)

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
            "vectors_created": rows_indexed,
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


def friendly_ingest_error(exc: Exception) -> str:
    """
    Turn a failure into a sentence a person can act on.

    WHY THIS EXISTS
    ---------------
    Ingestion used to return `str(e)`, so the most common failure on a fresh
    machine — the embedding model cannot be downloaded — reached the UI as a
    wall of Python:

        (MaxRetryError("HTTPSConnectionPool(host='huggingface.co', port=443):
        Max retries exceeded with url: /sentence-transformers/...

    That is not an error message, it is a stack trace that escaped. Nothing in
    it says what failed, nothing in it says what to do, and it is the *first*
    thing a new user sees after following the quickstart. The trace is still
    logged; the log is where a trace belongs.

    The mapping is deliberately narrow — only failures that have a known cause
    and a known fix are translated. Anything unrecognised keeps its original
    text, because a rewritten error that hides the real one is worse than an
    ugly one.
    """
    text = str(exc)
    lowered = f"{type(exc).__name__} {text}".lower()

    # Embedding weights could not be fetched. By far the most common first-run
    # failure, and the one with a one-line fix.
    if "huggingface" in lowered and (
        "max retries" in lowered or "connection" in lowered or "ssl" in lowered
        or "couldn't connect" in lowered or "not found" in lowered
    ):
        model = get_settings().embedding_model
        return (
            f"Could not download the embedding model '{model}'. This is the model that "
            f"decides which code the agent is allowed to read, so indexing cannot start "
            f"without it. It is a one-time ~80 MB download: check the machine's network "
            f"access to huggingface.co and try again."
        )

    # Private repository, no usable credentials.
    if "authentication failed" in lowered or "could not read username" in lowered:
        return (
            "GitHub would not serve this repository. If it is private, connect your GitHub "
            "account from the Repositories panel and index it from there."
        )
    if "repository not found" in lowered or "not found" in lowered and "repository" in lowered:
        return (
            "GitHub could not find that repository. Check the URL, and connect your account "
            "if it is private."
        )

    if "disk" in lowered and "full" in lowered:
        return "The disk is full — the index could not be written. Free some space and try again."

    if isinstance(exc, MemoryError):
        return "Ran out of memory while indexing. Close other programs, or index a smaller repository."

    return text


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
        path=str(data_dir()),
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
