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
from app.services.agent_run import summarize_args
from app.services.stream_protocol import error_event, status_event
from app.services.token_counter import get_token_callback, increment_request
from app.services.code_analysis import analyze_file
from app.services.code_analysis.analyzer import build_llm_facts


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
      "thinking" — inside a <think> block; suppress all content
      "after"    — past </think>; yield tokens normally

    Edge cases handled:
      - Unterminated <think> at end of stream: flush buffered content (not discard)
      - </think> without <think>: strip the tag, continue yielding normally
      - Multiple <think>…</think> blocks: each is stripped independently

    WHY THE INNER while LOOP?
    A single token may contain multiple state transitions, e.g.:
      "<think>reasoning</think>answer<think>more</think>done"
    Without re-processing the buffer after each state change, state transitions
    triggered late in the if-chain (e.g. "after"→"thinking") would not be
    handled until the NEXT token. The inner loop re-processes until stable.
    """
    state = "before"
    buffer = ""
    thinking_announced = False

    async for token in token_stream:
        buffer += token

        # Re-process buffer until no further state change occurs in one pass.
        changed = True
        while changed:
            changed = False

            if state == "before":
                # Strip orphaned </think> (no opening tag)
                if "</think>" in buffer and "<think>" not in buffer:
                    buffer = buffer.replace("</think>", "")
                    changed = True
                    continue
                if "<think>" in buffer:
                    pre = buffer[: buffer.index("<think>")]
                    if pre.strip():
                        yield pre
                    buffer = buffer[buffer.index("<think>") + len("<think>"):]
                    state = "thinking"
                    if not thinking_announced:
                        yield status_event("Reasoning…", step="thinking")
                        thinking_announced = True
                    changed = True
                    continue
                # No <think> — safe to yield once buffer can't split a tag
                if len(buffer) > 8:
                    yield buffer[:-7]
                    buffer = buffer[-7:]

            elif state == "thinking":
                if "</think>" in buffer:
                    # Discard all content before (and including) </think>
                    buffer = buffer[buffer.index("</think>") + len("</think>"):]
                    state = "after"
                    yield status_event("Writing review…", step="writing", mode="fast")
                    changed = True
                    continue
                # else: still inside think block.
                # Keep the entire buffer — don't tail-trim. Tokens are small (<100 chars)
                # so memory growth is negligible, and we need the full content preserved
                # in case the stream ends without a closing </think> (unterminated block).

            elif state == "after":
                # Handle a second <think> block (some models reason in multiple steps)
                if "<think>" in buffer:
                    pre = buffer[: buffer.index("<think>")]
                    if pre:
                        yield pre
                    buffer = buffer[buffer.index("<think>") + len("<think>"):]
                    state = "thinking"
                    changed = True
                    continue
                elif buffer:
                    yield buffer
                    buffer = ""

    # Flush remainder — always yield, even if we never saw </think>.
    # Unterminated think block: yield whatever the model buffered — still useful.
    if buffer:
        yield buffer

settings = get_settings()


def _one_line(text: str) -> str:
    """First line of a tool result, whitespace-collapsed, for a status preview."""
    return " ".join((text or "").split())[:160]


def _peek(result: object) -> str:
    """A short, single-line view of a tool result for the finished step."""
    text = " ".join(str(result or "").split())
    return text[:80] + ("…" if len(text) > 80 else "") if text else "no output"


# ── Tool factory ───────────────────────────────────────────────────────────────
# Tools are created per-file with the file content captured in a closure.
# The LLM never receives the full content as an argument — it only receives
# small, focused tool outputs. This halves token usage vs. passing content each call.

def _make_tools(file_content: str, language: str = ""):
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
        lang = language.lower().lstrip(".")
        is_python = lang in ("py", "python", "")

        metrics: dict[str, object] = {
            "total_lines": len(lines),
            "nested_loops": 0 if is_python else "N/A (indent-based detection only works for Python)",
            "bare_excepts": 0 if is_python else "N/A (Python-only metric)",
            "long_lines": 0,
            "todo_comments": 0,
            "magic_numbers": 0,
        }

        loop_stack: list[int] = []
        for line in lines:
            if not line.strip():
                continue
            stripped = line.strip()

            if is_python:
                indent = len(line) - len(line.lstrip())
                while loop_stack and indent <= loop_stack[-1]:
                    loop_stack.pop()
                if re.match(r"^(for|while)\b", stripped):
                    if loop_stack:
                        metrics["nested_loops"] = int(metrics["nested_loops"]) + 1  # type: ignore[arg-type]
                    loop_stack.append(indent)
                # Match "except:" and "except SomeException:" and "except SomeException as e:"
                if re.match(r"^except\s*(\w[\w.]*(\s+as\s+\w+)?)?\s*:", stripped):
                    # Only flag bare "except:" and "except Exception" without re-raise
                    if stripped == "except:" or re.match(r"^except\s+Exception\s*(\s+as\s+\w+)?\s*:", stripped):
                        metrics["bare_excepts"] = int(metrics["bare_excepts"]) + 1  # type: ignore[arg-type]

            if len(line) > 120:
                metrics["long_lines"] = int(metrics["long_lines"]) + 1  # type: ignore[arg-type]
            if re.search(r"\b(TODO|FIXME|HACK)\b", line, re.IGNORECASE):
                metrics["todo_comments"] = int(metrics["todo_comments"]) + 1  # type: ignore[arg-type]
            if re.search(r"(?<![=\w])\b[0-9]{2,}\b(?!\s*[=\w])", stripped):
                metrics["magic_numbers"] = int(metrics["magic_numbers"]) + 1  # type: ignore[arg-type]

        return json.dumps(metrics, indent=2)

    return [search_pattern, get_function_list, count_complexity_indicators]


def _run_security_scan(file_content: str) -> str:
    """
    Categorized security pattern scan with word-boundary anchors.
    Returns a structured report instead of a single mega-regex hit list.

    WHY SEPARATE FROM search_pattern?
    The LangChain tool `search_pattern` is for the LLM to call with arbitrary patterns.
    This function is for the deterministic fast-review pre-pass. Using word boundaries
    and category separation eliminates false positives like:
      - `token` matching `tokenize`
      - `SELECT` matching `selectedFile`
      - `DELETE` matching `deleteUser`
    """
    categories = {
        "hardcoded_secrets": r'(?i)\b(api_?key|password|secret|private_?key)\s*=\s*["\'][^"\']{4,}["\']',
        "shell_exec":        r'\b(subprocess\.(run|call|Popen|check_output)|os\.system|os\.popen)\s*\(',
        "dangerous_eval":    r'\b(eval|exec)\s*\(',
        "sql_injection":     r'\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION)\b.*(%s|\{|\+|format\s*\(|f")',
        "deserialization":   r'\b(pickle\.loads?|yaml\.load\s*\(|marshal\.loads?)\s*\(',
        "path_traversal":    r'(?i)(\.\.\/|\.\.\\|os\.path\.join.*request|open\s*\(.*request)',
    }
    lines = file_content.split("\n")
    report_parts = []
    for category, pattern in categories.items():
        try:
            compiled = re.compile(pattern)
        except re.error:
            continue
        hits = [
            f"  Line {i}: {line.rstrip()[:100]}"
            for i, line in enumerate(lines, 1)
            if compiled.search(line)
        ]
        if hits:
            report_parts.append(f"[{category}] ({len(hits)} hit(s)):\n" + "\n".join(hits[:5]))
    return "\n\n".join(report_parts) if report_parts else "No security pattern hits found."


# ── System prompts ─────────────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
You are SavFlux, a senior software engineer performing a thorough code review.

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
You are SavFlux, a senior software engineer performing a structured code review.

You are given the file content plus a "Verified static analysis" block produced
by a parser that analysed this exact file. Use ONLY this supplied data — never
invent bugs, line numbers, or behaviours not visible in the evidence.

HOW TO USE THE VERIFIED BLOCK:
- Items under "VERIFIED ISSUES" were proven by parsing the code, including
  dataflow across lines. Treat them as established fact. Report every one of
  them, and spend your effort on the part a parser cannot do: what breaks in
  production, which caller is exposed, and what to change first.
- Items under "POSSIBLE ISSUES" are heuristics. Check each against the source
  before repeating it. If the code shows it is a false alarm, drop it silently.
- The parser finds pattern-level defects. You find the ones that need reading
  comprehension: wrong logic, a race, a misused API, an unhandled edge case,
  a contract the caller cannot satisfy. That is where your value is.

GROUNDING RULES:
- Cite exact line numbers. The verified block gives them; do not guess others.
- Never restate a verified finding as uncertain, and never present a heuristic
  as proven.
- If a section has nothing to report, write "None found." — do not fabricate findings.
- If a potential problem cannot be confirmed from the snippet alone, say "Possibly…"
  and state what additional context is needed to confirm it.

OUTPUT FORMAT — always use these exact headings:
## 🐛 Bugs & Risks
(Concrete logic errors, crashes, wrong behaviour — cite line numbers from the Structure list)

## 🔒 Security
(Every VERIFIED security finding, with its impact. Add heuristic ones only after confirming them.)

## ⚠️ Maintainability
(Complexity hotspots named in the verified block, plus design problems you can see)

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
    tools    = _make_tools(file_content, language)
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

    yield status_event(f"Analyzing `{file_name}`…", step="starting", mode="agentic")

    llm_with_tools = llm.bind_tools(tools)
    max_iters  = 8
    max_secs   = 90
    start_time = time.monotonic()

    try:
        for iteration in range(max_iters):
            if time.monotonic() - start_time > max_secs:
                break  # fall through to forced final generation
            response = await llm_with_tools.with_config(callbacks=[get_token_callback()]).ainvoke(messages)
            messages.append(response)
            if response.tool_calls:
                for index, tc in enumerate(response.tool_calls):
                    # Same vocabulary the code agent uses: an id that pairs the
                    # two markers, the arguments the model actually passed, and a
                    # duration. A review that says "Tool: search_pattern..." with
                    # no result and no cost is a log line, not a tool card.
                    step_id = f"{tc['name']}#{index + 1 + iteration * len(response.tool_calls)}"
                    yield status_event(
                        f"Tool: `{tc['name']}`…",
                        step="tool", tool=tc["name"], step_id=step_id,
                        plan_id=f"plan:{tc['name']}", iteration=iteration + 1,
                        args=summarize_args(tc["name"], tc.get("args") or {}),
                    )
                    fn = tool_map.get(tc["name"])
                    called_at = time.monotonic()
                    try:
                        result = await asyncio.to_thread(fn.invoke, tc["args"]) if fn else f"Unknown tool: {tc['name']}"
                        ok = fn is not None
                    except Exception as exc:  # noqa: BLE001 — a failed tool is a fact, not a crash
                        result, ok = f"{tc['name']} failed: {str(exc)[:120]}", False
                    yield status_event(
                        f"{tc['name']}: {_peek(result)}",
                        step="tool_done" if ok else "tool_error",
                        tool=tc["name"], step_id=step_id, ok=ok,
                        elapsed_ms=int((time.monotonic() - called_at) * 1000),
                        preview=[_one_line(line) for line in str(result).splitlines()[:3]],
                        preview_more=max(0, len(str(result).splitlines()) - 3),
                    )
                    messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            else:
                # LLM chose not to call tools — write the review now
                yield status_event("Writing review…", step="writing", mode="agentic",
                                   elapsed_ms=int((time.monotonic() - start_time) * 1000))
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

        # Reached max_iters or timed out — force a final generation pass
        # using the tool results already accumulated in `messages`
        yield status_event("Writing review (forced)…", step="writing", mode="agentic",
                           forced=True, elapsed_ms=int((time.monotonic() - start_time) * 1000))
        messages.append(HumanMessage(
            content=(
                "Time or iteration limit reached. "
                "Using the tool results gathered so far, write the structured review now. "
                "Include all sections even if some data is incomplete."
            )
        ))
        # Use unbound LLM (no tools) so it writes immediately without more tool calls
        raw_stream = (
            chunk.content
            async for chunk in streaming_llm.with_config(callbacks=[get_token_callback()]).astream(messages)
            if chunk.content
        )
        async for token in _strip_think_tags(raw_stream):
            yield token
    except Exception as e:
        yield error_event(str(e))


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
    tools    = _make_tools(file_content, language)
    tool_map = {t.name: t for t in tools}

    preview = file_content[:9000]
    trunc   = f"\n[Truncated — {len(file_content)} chars total]" if len(file_content) > 9000 else ""

    yield status_event(f"Scanning `{file_name}`…", step="starting", mode="fast")

    try:
        # One AST parse replaces three overlapping regex tools. The analyzer
        # returns proven findings with line numbers, so the model is handed
        # facts to explain rather than patterns to re-derive — which is what
        # closes most of the gap between a 7B local model and a hosted one.
        analysed_at = time.monotonic()
        analysis = await asyncio.to_thread(analyze_file, file_content, file_name, language)
        verified_facts = build_llm_facts(analysis)
        analysis_ms = int((time.monotonic() - analysed_at) * 1000)

        context_block = (
            f"\n### Cross-file Repo Context\n{repo_context}\n"
            if repo_context else ""
        )

        # The split the latency work needs: this is the cost of the deterministic
        # pre-pass, and everything after it is the model's. The count is reported
        # without a proven/heuristic split — that threshold belongs to the
        # analyzer, and a second copy here would be a second definition.
        yield status_event(
            f"Writing review… ({len(analysis.findings)} finding(s) from static analysis)",
            step="writing", mode="fast",
            analysis_ms=analysis_ms, findings=len(analysis.findings),
        )

        messages = [
            SystemMessage(content=FAST_REVIEW_SYSTEM_PROMPT),
            HumanMessage(content=(
                f"Review `{file_name}` ({language}).\n\n"
                f"### Code\n```{language}\n{preview}\n```{trunc}\n"
                f"{context_block}"
                f"### Verified static analysis\n{verified_facts}\n\n"
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
        yield error_event(str(e))


# ── Batched multi-file review ─────────────────────────────────────────────────
#
# WHY BATCH THE MIDDLE OF THE REPO
# --------------------------------
# Most files in a repository are unremarkable. They have no security shape, the
# parser found nothing, and their complexity is ordinary — but they are still
# real code that a reviewer would want a second pair of eyes on. Reviewed one at a
# time, each of those files costs a full model call and a slot in the concurrency
# semaphore, which is where a 60-file review spends most of its sixty seconds.
#
# The question asked of such a file ("does anything here look wrong?") does not
# depend on the other files being absent from the prompt. So a group of them is
# asked once, with each file clearly delimited, and the answer is split back into
# per-file sections. Four files, one call, and the model still sees every line.
#
# The delimiter is a machine-readable header rather than prose because a parser
# that guesses where one file's review ends will attribute findings to the wrong
# file — the worst possible failure for a review tool.

BATCH_FILE_MARKER = "=== FILE: {name} ==="
BATCH_END_MARKER = "=== END FILE ==="

#: Per-file preview budget inside a batch prompt. Small enough that four files
#: fit in a comfortable context window, large enough for the model to see the
#: code rather than a summary of it.
BATCH_FILE_CHARS = 4500
#: Per-file share of the verified-facts block.
BATCH_FACTS_CHARS = 900

BATCH_REVIEW_SYSTEM_PROMPT = """\
You are SavFlux, a senior software engineer reviewing several files from one
repository in a single pass.

You are given each file's content plus a "Verified static analysis" block
produced by a parser that analysed that exact file. Use ONLY this supplied data —
never invent bugs, line numbers, or behaviours not visible in the evidence.

RULES:
- Review each file independently. A finding must belong to the file it is
  written under, and line numbers must come from that file.
- Items under "VERIFIED ISSUES" were proven by parsing: treat them as fact and
  explain impact rather than re-deriving them.
- Items under "POSSIBLE ISSUES" are heuristics: confirm against the source shown
  before repeating them.
- Be terse. A clean file gets "None found" under each heading. Do not pad.
"""


def _batch_prompt(files_slice: list[dict], repo_context_map: dict[str, str]) -> str:
    """One prompt holding several files, each behind an unambiguous header."""
    from app.services.code_analysis.analyzer import build_llm_facts

    blocks: list[str] = []
    for file_info in files_slice:
        name = file_info.get("file_name", "unknown")
        language = file_info.get("language", "")
        content = file_info.get("content", "")
        preview = content[:BATCH_FILE_CHARS]
        truncation = (
            f"\n[Truncated — showing {BATCH_FILE_CHARS} of {len(content)} chars]"
            if len(content) > BATCH_FILE_CHARS else ""
        )

        try:
            analysis = analyze_file(content, name, language)
            facts = build_llm_facts(analysis)[:BATCH_FACTS_CHARS]
        except Exception:  # noqa: BLE001 — facts are an aid, not a precondition
            facts = "Static analysis unavailable for this file."

        context = repo_context_map.get(name, "")
        context_block = f"\n### Cross-file Repo Context\n{context}\n" if context else ""

        blocks.append(
            f"{BATCH_FILE_MARKER.format(name=name)}\n"
            f"language: {language}\n\n"
            f"### Code\n```{language}\n{preview}\n```{truncation}\n"
            f"{context_block}"
            f"### Verified static analysis\n{facts}\n"
        )

    headings = ", ".join(f"`{f.get('file_name')}`" for f in files_slice)
    return (
        f"Review these {len(files_slice)} files: {headings}.\n\n"
        + "\n".join(blocks)
        + "\n\nFor EACH file above, emit exactly this structure and nothing else:\n"
        + BATCH_FILE_MARKER.format(name="<filename as given above>") + "\n"
        "## 🐛 Bugs & Risks\n## 🔒 Security\n## ⚠️ Maintainability\n"
        "## 🔗 Cross-file Issues\n## ⚡ Fast Fixes\n## 📊 Score (1–10)\n"
        + BATCH_END_MARKER + "\n\n"
        "The header and footer lines must appear verbatim — an automated parser "
        "splits your answer with them, and a file left without a block is shown to "
        "the user as not reviewed.\n"
        "Write 'None found' for any heading that is clean."
    )


async def stream_batch_code_review(
    files_slice: list[dict],
    repo_context_map: dict[str, str] | None = None,
    model_override: str = "",
) -> AsyncGenerator[str, None]:
    """
    Stream one review covering several files, delimited by BATCH_FILE_MARKER.

    Yields raw text; the caller splits it with `split_batch_review`. Streaming is
    kept (rather than returning a string) so the caller can bound the whole batch
    with a timeout and so a partially produced answer is still usable.
    """
    increment_request("review")
    streaming_llm = get_review_llm(model_override, streaming=True)
    prompt = _batch_prompt(files_slice, repo_context_map or {})

    messages = [
        SystemMessage(content=BATCH_REVIEW_SYSTEM_PROMPT),
        HumanMessage(content=prompt),
    ]

    raw_stream = (
        chunk.content
        async for chunk in streaming_llm.with_config(callbacks=[get_token_callback()]).astream(messages)
        if chunk.content
    )
    async for token in _strip_think_tags(raw_stream):
        yield token


def split_batch_review(text: str, expected_names: list[str]) -> dict[str, str]:
    """
    Split a batched review into {file_name: review_text}.

    Matching is by exact name first, then by basename, because a model that was
    given `src/auth/tokens.py` will sometimes echo `tokens.py`. A file with no
    block is simply absent from the result — the caller falls back to the
    deterministic report for it and says so, rather than attributing one file's
    findings to another.

    Pure function: no I/O, no globals, so the parsing can be tested directly.
    """
    if not text.strip():
        return {}

    pattern = re.compile(
        r"^[=\s]*FILE:\s*(?P<name>[^\n=]+?)\s*[=]*\s*$"
        r"(?P<body>.*?)"
        r"^[=\s]*END FILE[=\s]*$",
        re.MULTILINE | re.DOTALL,
    )

    by_name: dict[str, str] = {}
    by_basename: dict[str, str] = {}
    for match in pattern.finditer(text):
        name = match.group("name").strip()
        body = match.group("body").strip()
        if not name:
            continue
        by_name[name] = body
        by_basename.setdefault(name.rsplit("/", 1)[-1], body)

    result: dict[str, str] = {}
    for expected in expected_names:
        body = by_name.get(expected)
        if body is None:
            body = by_basename.get(expected.rsplit("/", 1)[-1])
        if body:
            result[expected] = body
    return result
