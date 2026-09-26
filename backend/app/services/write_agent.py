"""
write_agent.py — Code generation service with three modes.

MODES:
  generate  — Write a new file from a natural-language description.
              Context files retrieved semantically (RAG) so only the most
              relevant patterns are injected — not whole files truncated.

  edit      — Improve or extend an existing indexed file.
              The full file content is injected as "current code"; the LLM
              produces a rewritten version with the requested changes applied.
              This is the #1 real-world use case: "add input validation here",
              "refactor this to be async", "fix the bug in process_upload".

  tests     — Generate a test file for an existing indexed file.
              Automatically detects common test frameworks from repo context
              and generates tests that match the project's existing test style.

WHY SEMANTIC RETRIEVAL FOR CONTEXT?
Dumping whole files as context (original approach) had two problems:
  1. The 1500-char truncation cut most files off mid-function — the LLM
     saw incomplete patterns and couldn't replicate the style properly.
  2. Large files wasted tokens on irrelevant sections.

Fix: use the vector store's similarity search to pull the top-K chunks
most semantically related to the user's prompt. The LLM gets dense,
on-topic context within the same token budget.
"""

import logging
from typing import AsyncGenerator, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.stream_protocol import error_event, status_event
from app.core.config import get_settings
from app.services.llm_factory import get_chat_llm
from app.services.token_counter import get_token_callback, increment_request

logger = logging.getLogger(__name__)
settings = get_settings()

WriteMode = Literal["generate", "edit", "tests"]

# ── System prompts ────────────────────────────────────────────────────────────

GENERATE_SYSTEM_PROMPT = """You are SavFlux, a senior software engineer writing production-quality code.

Rules:
1. Write complete, working code — no TODO placeholders or stub bodies.
2. Closely mirror the coding style, naming conventions, and patterns in the provided context.
3. Add concise docstrings/comments explaining non-obvious logic.
4. Use a fenced code block with the correct language tag.
5. After the code, add a brief "## How to use" section with a concrete usage example.
6. If the context shows a specific pattern (Pydantic models, async/await, error handling style),
   follow it exactly — consistency with the existing codebase is the top priority."""

EDIT_SYSTEM_PROMPT = """You are SavFlux, a senior software engineer performing targeted code edits.

You will be given the CURRENT CODE of a file and a description of the requested changes.

Rules:
1. Output the COMPLETE rewritten file — not a diff, not a patch, not a partial snippet.
2. Apply ONLY the requested changes. Do not refactor unrelated code.
3. Preserve all existing comments, docstrings, and formatting that are not affected by the change.
4. Use a fenced code block with the correct language tag.
5. After the code, add a brief "## Changes made" section listing what was modified and why."""

TESTS_SYSTEM_PROMPT = """You are SavFlux, an expert at writing comprehensive test suites.

You will be given source code and must write tests for it.

Rules:
1. Use the test framework shown in the provided context (pytest, jest, go test, etc.).
   If no existing tests are provided, choose the most common framework for the language.
2. Cover happy paths, edge cases, and error/exception paths for every public function.
3. Write self-contained tests — no mocks unless the function itself requires external I/O.
4. Follow the naming and structure conventions shown in the existing test files.
5. Use a fenced code block with the correct language tag.
6. After the code, add a brief "## Coverage summary" section listing what was tested."""


async def stream_code_write(
    prompt: str,
    language: str,
    file_name: str,
    context_sources: list[str],
    mode: WriteMode = "generate",
) -> AsyncGenerator[str, None]:
    """
    Stream generated/edited/tested code.

    Args:
        prompt:          User's description of what to build/change/test.
        language:        Target language (for the fenced code block tag).
        file_name:       Output filename (used in the prompt for context).
        context_sources: Source paths of files selected as context.
                         For "edit" and "tests" modes the FIRST source is the
                         target file; remaining are style references.
        mode:            "generate" | "edit" | "tests"
    """
    increment_request("write")
    llm = get_chat_llm(streaming=True).with_config(callbacks=[get_token_callback()])

    # ── Step 1: Fetch file content from ChromaDB ──────────────────────────────
    target_content  = ""   # full content of the file being edited/tested
    context_text    = ""   # style-reference snippets for generate mode

    if context_sources:
        yield status_event(f"Fetching context from {len(context_sources)} file(s)…", step="context")

        try:
            from app.services.ingestion_service import _get_vectorstore
            from app.services.chunk_reconstruction import reconstruct_chunks
            from collections import defaultdict
            vs = _get_vectorstore()
            collection = vs._collection

            # ── Batch-fetch all required sources in one ChromaDB call ─────────
            # The original code called collection.get() once per source in a loop
            # (N sequential round-trips ~50ms each). A single $in query fetches
            # all chunks for all sources at once and we group them in Python.
            all_sources = list(dict.fromkeys(context_sources))  # dedup, preserve order
            where_filter = (
                {"source": {"$in": all_sources}}
                if len(all_sources) > 1
                else {"source": all_sources[0]}
            )
            batch = collection.get(where=where_filter, include=["documents", "metadatas"])
            all_docs  = batch.get("documents") or []
            all_metas = batch.get("metadatas") or []

            # Group chunks by source
            grouped: dict[str, list[tuple[dict, str]]] = defaultdict(list)
            for meta, doc in zip(all_metas, all_docs):
                src = meta.get("source", "")
                if src:
                    grouped[src].append((meta, doc))

            def _reconstruct_from_group(src: str) -> tuple[str, str, str]:
                """Reconstruct a file from its pre-fetched chunks."""
                pairs = grouped.get(src, [])
                if not pairs:
                    return "", "", ""
                content = reconstruct_chunks(pairs)
                fname   = pairs[0][0].get("file_name", src.split("/")[-1])
                lang    = pairs[0][0].get("language", "")
                return content, fname, lang

            if mode in ("edit", "tests"):
                # First source = the target file to edit or test
                target_content, target_fname, target_lang = _reconstruct_from_group(context_sources[0])
                # Remaining sources = style references (same as generate mode below)
                style_sources = context_sources[1:5]
            else:
                style_sources = context_sources[:5]
                target_fname  = file_name
                target_lang   = language

            # ── Semantic retrieval for style context ──────────────────────────
            # Instead of dumping whole files (which gets truncated), query the
            # vector store for chunks most semantically related to the prompt.
            # This gives the LLM dense, on-topic style examples.
            if style_sources and mode == "generate":
                try:
                    from app.services.retrieval_service import _get_vectorstore as _rv
                    retrieval_vs = _rv()
                    # Filter to only the selected style files
                    style_docs = retrieval_vs.similarity_search(
                        query=prompt,
                        k=8,
                        filter={"source": {"$in": style_sources}},
                    )
                    if style_docs:
                        snippets = []
                        seen_files: set[str] = set()
                        for doc in style_docs:
                            fname = doc.metadata.get("file_name", "")
                            lang  = doc.metadata.get("language", "")
                            header = f"### {fname}" if fname not in seen_files else None
                            if header:
                                seen_files.add(fname)
                                snippets.append(f"{header}\n```{lang}\n{doc.page_content}\n```")
                            else:
                                # Append chunk to existing file block
                                snippets.append(f"```{lang}\n{doc.page_content}\n```")
                        context_text = "\n\n".join(snippets)
                    else:
                        raise ValueError("no results from similarity search")
                except Exception:
                    # Fallback: reconstruct from pre-fetched groups (no extra DB calls)
                    snippets = []
                    for src in style_sources:
                        content, fname, lang = _reconstruct_from_group(src)
                        if content:
                            snippets.append(f"### {fname}\n```{lang}\n{content[:3000]}\n```")
                    context_text = "\n\n".join(snippets)

            elif style_sources:
                # For edit/tests modes: include style references as full reconstructed files
                snippets = []
                for src in style_sources:
                    content, fname, lang = _reconstruct_from_group(src)
                    if content:
                        snippets.append(f"### {fname}\n```{lang}\n{content[:2000]}\n```")
                context_text = "\n\n".join(snippets)

        except Exception as ctx_exc:
            # Non-fatal — the write can proceed without style context.
            # Emit a diagnostic so the user knows context was unavailable
            # rather than silently receiving context-free generated code.
            logger.warning("Context fetch failed (proceeding without style context): %s", ctx_exc)
            yield (
                f"__DIAGNOSTIC__⚠️ **Context unavailable**: Could not retrieve style context "
                f"from the vector store ({str(ctx_exc)[:120]}). "
                f"Generated code may not match your codebase style.__DIAGNOSTIC_END__\n"
            )

    # ── Step 2: Build mode-specific prompt ────────────────────────────────────

    if mode == "edit":
        if not target_content:
            yield status_event("Error: could not read target file", step="error")
            yield "❌ Could not retrieve the file content from the vector store. Please re-index the repo."
            return

        yield status_event(f"Editing `{target_fname}`…", step="generating", mode="edit")

        context_section = (
            f"\n\n## Style reference from the codebase:\n\n{context_text}"
            if context_text else ""
        )
        user_message = (
            f"Here is the CURRENT CODE of `{target_fname}` ({target_lang}):\n\n"
            f"```{target_lang}\n{target_content}\n```\n\n"
            f"## Requested changes:\n{prompt}"
            f"{context_section}\n\n"
            f"Produce the complete rewritten file now."
        )
        system = EDIT_SYSTEM_PROMPT

    elif mode == "tests":
        if not target_content:
            yield status_event("Error: could not read target file", step="error")
            yield "❌ Could not retrieve the file content from the vector store. Please re-index the repo."
            return

        yield status_event(f"Generating tests for `{target_fname}`…", step="generating", mode="tests")

        style_section = (
            f"\n\n## Existing test files (match this style):\n\n{context_text}"
            if context_text else ""
        )
        # Derive the test file extension from the target file's own extension,
        # not from the user-supplied `language` string.
        # `language` is a human-readable name like "python" or "typescript";
        # `target_lang` is the short extension stored in ChromaDB metadata ("py", "ts").
        # Using `language` would produce "test_auth.python" instead of "test_auth.py".
        target_ext = target_lang if target_lang else (target_fname.rsplit(".", 1)[-1] if "." in target_fname else language)
        base = target_fname.rsplit(".", 1)[0]
        test_file_name = f"test_{base}.{target_ext}" if not file_name.startswith("test_") else file_name
        user_message = (
            f"Write a complete test file `{test_file_name}` for the following {target_lang} code:\n\n"
            f"```{target_lang}\n{target_content}\n```\n\n"
            f"## Additional instructions:\n{prompt if prompt.strip() else 'Cover all public functions with happy path, edge cases, and error cases.'}"
            f"{style_section}\n\n"
            f"Write the complete test file now."
        )
        system = TESTS_SYSTEM_PROMPT

    else:
        # generate mode
        yield status_event(f"Generating {language} code…", step="generating", mode="generate")

        context_section = (
            f"\n\n## Codebase context — match this style:\n\n{context_text}"
            if context_text else ""
        )
        user_message = (
            f"Write a `{file_name}` file in {language}.\n\n"
            f"## Requirements:\n{prompt}"
            f"{context_section}\n\n"
            f"Write the complete implementation now."
        )
        system = GENERATE_SYSTEM_PROMPT

    # ── Step 3: Stream the response ───────────────────────────────────────────
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=user_message),
    ]

    try:
        async for chunk in llm.astream(messages):
            if chunk.content:
                yield chunk.content
    except Exception as e:
        yield error_event(str(e))
