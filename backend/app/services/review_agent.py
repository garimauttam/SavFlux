"""
review_agent.py — Fast code review using deterministic tools + one LLM pass.

ARCHITECTURE (fast mode, used for multi-file review):
  1. Run three deterministic tools in parallel (no LLM, instant):
       get_function_list       → structural skeleton
       count_complexity_indicators → metrics
       search_pattern          → security/runtime hot-spots
  2. Inject optional repo_context (cross-file dependency map built once per batch).
  3. One streaming LLM call writes the full structured review.

  Total LLM calls per file: 1  (down from the original 3–9 in the ReAct loop).
  This is the right trade-off for batch review: the deterministic tools already
  provide the structured signal; the LLM synthesises, reasons, and explains.

ARCHITECTURE (agentic mode, used for single-file deep review):
  ReAct loop — LLM decides which tools to call and writes the review itself.
  More thorough but ~3× slower; not suitable for >5 files at a time.
"""

import asyncio
import json
import re
import time
from typing import AsyncGenerator

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.core.config import get_settings
from app.services.llm_factory import get_chat_llm, get_review_llm
from app.services.token_counter import get_token_callback, increment_request


# ── DeepSeek-R1 <think> tag stripper ─────────────────────────────────────────
# DeepSeek-R1 models prefix every response with a chain-of-thought block:
#   <think>\n…reasoning…\n</think>\n\nActual answer
#
# We want to:
#   a) emit a __THINKING__ status token when the block starts (frontend shows spinner)
#   b) suppress all content inside <think>…</think> from the review output
#   c) yield normally once </think> is seen
#
# The filter is a stateful async generator that wraps any token stream, so it
# works for both stream_fast_code_review and stream_code_review identically.

async def _strip_think_tags(
    token_stream: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """
    Wrap a streaming token source and strip DeepSeek-R1 <think>…</think> blocks.

    States:
      "before"   — haven't seen <think> yet; yield tokens normally
      "thinking" — inside a <think> block; suppress tokens, emit __THINKING__ once
      "after"    — past </think>; yield tokens normally
    """
    state = "before"
    buffer = ""
    thinking_announced = False

    async for token in token_stream:
        buffer += token

        if state == "before":
            # Check if we're entering a think block
            if "<think>" in buffer:
                # Yield any content before the tag
                pre = buffer[: buffer.index("<think>")]
                if pre.strip():
                    yield pre
                buffer = buffer[buffer.index("<think>") + len("<think>"):]
                state = "thinking"
                if not thinking_announced:
                    yield f"__STATUS__Reasoning...{json.dumps({'step': 'thinking'})}__STATUS_END__\n"
                    thinking_announced = True
            else:
                # Safe to yield once buffer is long enough that <think> can't split
                if len(buffer) > 8:
                    yield buffer[:-7]
                    buffer = buffer[-7:]

        if state == "thinking":
            # Look for closing tag
            if "</think>" in buffer:
                # Drop everything up to and including </think>
                buffer = buffer[buffer.index("</think>") + len("</think>"):]
                state = "after"
                # Emit status: done thinking, now writing
                yield f"__STATUS__Writing review...{json.dumps({'step': 'writing', 'mode': 'fast'})}__STATUS_END__\n"
            else:
                # Still inside think block — discard
                buffer = buffer[-8:] if len(buffer) > 8 else buffer

        if state == "after":
            # Yield everything we have accumulated
            if buffer:
                yield buffer
                buffer = ""

    # Flush remainder
    if buffer and state != "thinking":
        yield buffer

settings = get_settings()


# ── Tool factory ───────────────────────────────────────────────────────────────
# Tools are created per-file with the file content captured in a closure.
# The LLM never receives the full content as an argument — it only receives
# small, focused tool outputs. This halves token usage vs. passing content each call.

def _make_tools(file_content: str):
    @tool
    def search_pattern(pattern: str) -> str:
        """Search for a regex pattern in the code. Returns matching lines with numbers."""
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex: {e}"
        matches = [
            f"Line {i}: {line.rstrip()}"
            for i, line in enumerate(file_content.split("\n"), 1)
            if compiled.search(line)
        ]
        return "\n".join(matches[:30]) if matches else f"No matches for: {pattern}"

    @tool
    def get_function_list() -> str:
        """Extract all function, method, and class definitions with line numbers."""
        PATTERNS = [
            r"^\s*(async\s+def|def|class)\s+\w",
            r"^\s*(export\s+)?(async\s+)?function\s+\w",
            r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?\(",
            r"^\s*(export\s+)?(default\s+)?class\s+\w",
            r"^\s*func\s+(\(\w+\s+\*?\w+\)\s+)?\w+\s*\(",
            r"^\s*(public|private|protected|static|override|abstract).*\s+\w+\s*\(",
            r"^\s*(pub(\(.*\))?\s+)?(async\s+)?fn\s+\w",
            r"^\s*def\s+\w",
        ]
        compiled = [re.compile(p) for p in PATTERNS]
        defs = [
            f"Line {i}: {line.rstrip()[:80]}"
            for i, line in enumerate(file_content.split("\n"), 1)
            if any(p.search(line) for p in compiled)
        ]
        return "\n".join(defs) if defs else "No definitions found."

    @tool
    def count_complexity_indicators() -> str:
        """Return complexity metrics: nested loops, bare excepts, long lines, TODOs."""
        lines = file_content.split("\n")
        metrics: dict[str, int] = {
            "total_lines": len(lines),
            "nested_loops": 0,
            "bare_excepts": 0,
            "long_lines": 0,
            "todo_comments": 0,
            "magic_numbers": 0,
        }
        loop_stack: list[int] = []
        for line in lines:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            while loop_stack and indent <= loop_stack[-1]:
                loop_stack.pop()
            if re.match(r"^(for|while)\b", stripped):
                if loop_stack:
                    metrics["nested_loops"] += 1
                loop_stack.append(indent)
            if stripped == "except:":
                metrics["bare_excepts"] += 1
            if len(line) > 120:
                metrics["long_lines"] += 1
            if re.search(r"\b(TODO|FIXME|HACK)\b", line, re.IGNORECASE):
                metrics["todo_comments"] += 1
            if re.search(r"(?<![=\w])\b[0-9]{2,}\b(?!\s*[=\w])", stripped):
                metrics["magic_numbers"] += 1
        return json.dumps(metrics, indent=2)

    return [search_pattern, get_function_list, count_complexity_indicators]


# ── System prompts ─────────────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
You are CodeSage, a senior software engineer performing a thorough code review.

You have access to these tools:
- get_function_list: understand the file's structure
- count_complexity_indicators: measure complexity metrics
- search_pattern: find specific patterns (SQL, secrets, error handling, etc.)

REVIEW PROCESS:
1. Call get_function_list, then count_complexity_indicators
2. Search for security issues, bare excepts, SQL injection, hardcoded secrets
3. Search for patterns relevant to what the code does

Write a structured review with:
## 📁 File Overview
## 🐛 Bugs & Critical Issues  (cite line numbers; say "None found" if clean)
## 🔒 Security Concerns
## ⚠️ Code Quality Issues
## 💡 Suggestions for Improvement  (show fixed code in blocks)
## ✅ What's Done Well
## 📊 Summary Score  (rate 1–10 on Correctness, Security, Readability, Maintainability)

Be specific. Cite line numbers. Show fixed code in code blocks.\
"""

FAST_REVIEW_SYSTEM_PROMPT = """\
You are CodeSage, a senior software engineer performing a structured code review.

You have been given the file content, a function/class list, complexity metrics,
and security pattern matches. Use ONLY this supplied data — never invent bugs,
line numbers, or behaviours not directly visible in the evidence.

GROUNDING RULES:
- Cite exact line numbers from the "Structure" list. Do not guess line numbers.
- Only flag a bug or security issue if it is present in the supplied code or pattern hits.
- If a section has nothing to report, write "None found." — do not fabricate findings.
- If a potential problem cannot be confirmed from the snippet alone, say "Possibly…"
  and state what additional context is needed to confirm it.

OUTPUT FORMAT — always use these exact headings:
## 🐛 Bugs & Risks
(Concrete logic errors, crashes, wrong behaviour — cite line numbers from the Structure list)

## 🔒 Security
(Hardcoded secrets, injection risks, unsafe deserialization — only report pattern hits shown above)

## ⚠️ Maintainability
(Bare excepts, nested loops, magic numbers, missing error handling — from the complexity metrics)

## 🔗 Cross-file Issues
(Use the Repo Context block. If no cross-file issues are evident, write "None found.")

## ⚡ Fast Fixes
(Top 1–3 highest-impact actionable changes with corrected code snippets where helpful)

## 📊 Score  (1–10)
Overall score with one-line reasoning. Base it on the metrics above.\
"""


# ── Agentic review (single-file deep mode) ────────────────────────────────────

async def stream_code_review(
    file_name: str,
    file_content: str,
    language: str = "",
    repo_context: str = "",
    model_override: str = "",
) -> AsyncGenerator[str, None]:
    """
    ReAct-loop agentic review. Used for single-file deep reviews.
    Each file costs 3–9 LLM calls but produces the most thorough output.
    """
    increment_request("review")
    llm           = get_review_llm(model_override, streaming=False)
    streaming_llm = get_review_llm(model_override, streaming=True)
    tools    = _make_tools(file_content)
    tool_map = {t.name: t for t in tools}

    preview = file_content[:8000]
    trunc   = f"\n[Truncated — {len(file_content)} chars total]" if len(file_content) > 8000 else ""

    context_block = f"\n\n### Repo Context (cross-file dependencies)\n{repo_context}" if repo_context else ""

    messages = [
        SystemMessage(content=REVIEW_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"Please review this {language} file: `{file_name}`\n\n"
            f"```{language}\n{preview}\n```{trunc}"
            f"{context_block}\n\n"
            "Investigate systematically and write a thorough structured review."
        )),
    ]

    yield f"__STATUS__Analyzing `{file_name}`...{json.dumps({'step': 'starting'})}__STATUS_END__\n"

    llm_with_tools = llm.bind_tools(tools)
    max_iters  = 8
    max_secs   = 90
    start_time = time.monotonic()

    try:
        for _ in range(max_iters):
            if time.monotonic() - start_time > max_secs:
                yield "\n\n*Review timed out — partial investigation complete.*"
                return
            response = await llm_with_tools.with_config(callbacks=[get_token_callback()]).ainvoke(messages)
            messages.append(response)
            if response.tool_calls:
                for tc in response.tool_calls:
                    yield f"__STATUS__Tool: `{tc['name']}`...{json.dumps({'step': 'tool', 'tool': tc['name']})}__STATUS_END__\n"
                    fn = tool_map.get(tc["name"])
                    result = await asyncio.to_thread(fn.invoke, tc["args"]) if fn else f"Unknown tool: {tc['name']}"
                    messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            else:
                yield f"__STATUS__Writing review...{json.dumps({'step': 'writing'})}__STATUS_END__\n"
                messages.append(HumanMessage(
                    content="Based on your investigation, write the full structured review."
                ))
                raw_stream = (
                    chunk.content
                    async for chunk in streaming_llm.with_config(callbacks=[get_token_callback()]).astream(messages)
                    if chunk.content
                )
                async for token in _strip_think_tags(raw_stream):
                    yield token
                return
    except Exception as e:
        yield f"__ERROR__{str(e)[:200]}__ERROR_END__\n"

    yield "\n\n*Review incomplete — max iterations reached.*"


# ── Fast review (multi-file batch mode) ───────────────────────────────────────

async def stream_fast_code_review(
    file_name: str,
    file_content: str,
    language: str = "",
    repo_context: str = "",
    model_override: str = "",
) -> AsyncGenerator[str, None]:
    """
    Low-latency review: parallel deterministic tools → single LLM streaming pass.

    The repo_context block is a condensed cross-file dependency map built once
    per batch by multi_review_agent. It tells the LLM which other files import
    this one, what it exports, and which types/services it depends on — enabling
    cross-file findings without reviewing every file with the LLM.

    model_override: if non-empty, routes the LLM call to a specific Ollama model
    (set by the LLM router in multi_review_agent based on the file's triage score).
    """
    increment_request("review")
    streaming_llm = get_review_llm(model_override, streaming=True)
    tools    = _make_tools(file_content)
    tool_map = {t.name: t for t in tools}

    preview = file_content[:9000]
    trunc   = f"\n[Truncated — {len(file_content)} chars total]" if len(file_content) > 9000 else ""

    yield f"__STATUS__Scanning `{file_name}`...{json.dumps({'step': 'starting', 'mode': 'fast'})}__STATUS_END__\n"

    try:
        # Run all three tools in parallel — they are pure CPU, no I/O, no LLM calls.
        fn_list, complexity, security_hits = await asyncio.gather(
            asyncio.to_thread(tool_map["get_function_list"].invoke, {}),
            asyncio.to_thread(tool_map["count_complexity_indicators"].invoke, {}),
            asyncio.to_thread(
                tool_map["search_pattern"].invoke,
                {"pattern": r"(api[_-]?key|password|secret|token|subprocess|os\.system|exec\(|eval\(|SELECT|INSERT|UPDATE|DELETE)"},
            ),
        )

        context_block = (
            f"\n### Cross-file Repo Context\n{repo_context}\n"
            if repo_context else ""
        )

        yield f"__STATUS__Writing review...{json.dumps({'step': 'writing', 'mode': 'fast'})}__STATUS_END__\n"

        messages = [
            SystemMessage(content=FAST_REVIEW_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Review `{file_name}` ({language}).\n\n"
                f"### Code\n```{language}\n{preview}\n```{trunc}\n"
                f"{context_block}"
                f"### Structure\n{fn_list}\n\n"
                f"### Complexity\n{complexity}\n\n"
                f"### Security / runtime pattern hits\n{security_hits}\n\n"
                "Return these sections:\n"
                "## 🐛 Bugs & Risks\n"
                "## 🔒 Security\n"
                "## ⚠️ Maintainability\n"
                "## 🔗 Cross-file Issues  (use the repo context above)\n"
                "## ⚡ Fast Fixes\n"
                "## 📊 Score (1–10)\n\n"
                "Say 'None found' for any section that is clean."
            )),
        ]

        raw_stream = (
            chunk.content
            async for chunk in streaming_llm.with_config(callbacks=[get_token_callback()]).astream(messages)
            if chunk.content
        )
        async for token in _strip_think_tags(raw_stream):
            yield token

    except Exception as e:
        yield f"__ERROR__{str(e)[:200]}__ERROR_END__\n"
