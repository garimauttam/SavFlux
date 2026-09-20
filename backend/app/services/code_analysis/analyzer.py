"""
analyzer — Language dispatch and report rendering.

`analyze_file` picks the strongest analyzer available for a file. Python gets a
real AST with dataflow; everything else gets the comment-aware scanner. The
caller never needs to know which ran, only that findings carry honest
confidence values.
"""

from __future__ import annotations

from app.services.code_analysis.generic_analyzer import analyze_generic
from app.services.code_analysis.models import FileAnalysis, Severity
from app.services.code_analysis.python_analyzer import analyze_python

PYTHON_EXTENSIONS = frozenset({"py", "python", "pyi", "pyw"})

#: Extensions that are data, not code. Reviewing them for cyclomatic complexity
#: produces noise, so only the secret rules are worth running.
DATA_EXTENSIONS = frozenset({"json", "yaml", "yml", "toml", "ini", "cfg", "env", "properties"})


def analyze_file(content: str, file_name: str = "", language: str = "") -> FileAnalysis:
    """
    Analyse one file with the best analyzer for its language.

    `language` is the extension without the dot, matching how the ingestion
    pipeline records it. When it is missing we infer from the filename, because
    the review endpoint accepts pasted code with no metadata at all.
    """
    normalised = (language or "").lower().lstrip(".")
    if not normalised and "." in file_name:
        normalised = file_name.rsplit(".", 1)[-1].lower()

    if normalised in PYTHON_EXTENSIONS:
        analysis = analyze_python(content, file_name)
        if not analysis.parse_error:
            return analysis
        # A file that does not parse still deserves the secret and TLS rules,
        # and a partially-written file is exactly where a leaked key hides.
        fallback = analyze_generic(content, file_name, "py")
        fallback.parse_error = analysis.parse_error
        return fallback

    return analyze_generic(content, file_name, normalised)


def render_findings_markdown(analysis: FileAnalysis, max_findings: int = 12) -> str:
    """
    Render findings as review markdown.

    Grouped by severity and capped, because a 60-item list is not a review — it
    is a wall that gets scrolled past. The cap is applied after sorting, so what
    survives is the worst of what was found.
    """
    if not analysis.findings:
        return "- ✅ No issues detected by static analysis."

    icons = {
        Severity.CRITICAL: "🛑",
        Severity.HIGH: "🔴",
        Severity.MEDIUM: "🟡",
        Severity.LOW: "🔵",
        Severity.INFO: "⚪",
    }

    lines: list[str] = []
    for finding in analysis.sorted_findings()[:max_findings]:
        icon = icons[finding.severity]
        cwe = f" · {finding.cwe}" if finding.cwe else ""
        # Below 0.8 the finding is a heuristic, and saying so protects the
        # credibility of everything reported above that line.
        qualifier = "" if finding.confidence >= 0.8 else " _(likely)_"
        lines.append(
            f"- {icon} **{finding.title}** — `L{finding.line}`{cwe}{qualifier}\n"
            f"  {finding.message}\n"
            + (f"  > `{finding.evidence}`\n" if finding.evidence else "")
            + (f"  **Fix:** {finding.remediation}" if finding.remediation else "")
        )

    remaining = len(analysis.findings) - max_findings
    if remaining > 0:
        lines.append(f"- _…and {remaining} lower-severity finding(s)._")

    return "\n".join(lines)


def build_llm_facts(analysis: FileAnalysis, max_findings: int = 10) -> str:
    """
    Compact, factual brief handed to the model alongside the source.

    This is the piece that makes a small local model competitive. Asking a 7B
    model to *find* a cross-line SQL injection in 400 lines is asking it to do
    dataflow analysis in one forward pass, and it will miss cases and invent
    others. Telling it "SQL injection at line 42, proven by dataflow from line
    38" and asking it to explain the impact plays to what it is good at.

    Kept terse on purpose: every token here competes with the source code for
    the model's context window.
    """
    parts: list[str] = []

    parts.append(
        f"STATIC ANALYSIS ({analysis.language}, {analysis.total_lines} lines, "
        f"{analysis.code_lines} code / {analysis.comment_lines} comment)"
    )

    if analysis.parse_error:
        parts.append(f"! File does not parse: {analysis.parse_error}")

    if analysis.functions:
        worst = sorted(analysis.functions, key=lambda f: -f.complexity)[:5]
        summary = ", ".join(f"{f.name}(L{f.line}, cx={f.complexity})" for f in worst)
        parts.append(f"Functions: {len(analysis.functions)}; most complex: {summary}")

    verified = [f for f in analysis.sorted_findings() if f.confidence >= 0.8][:max_findings]
    if verified:
        parts.append("VERIFIED ISSUES (found by parser — treat as fact, do not re-derive):")
        for f in verified:
            parts.append(f"  L{f.line} [{f.severity.value}] {f.rule_id}: {f.title}")

    heuristic = [f for f in analysis.sorted_findings() if f.confidence < 0.8][:5]
    if heuristic:
        parts.append("POSSIBLE ISSUES (heuristic — confirm against the source before repeating):")
        for f in heuristic:
            parts.append(f"  L{f.line} [{f.severity.value}] {f.title}")

    if not verified and not heuristic:
        parts.append("No issues found by static analysis. Focus on design, naming and correctness.")

    return "\n".join(parts)
