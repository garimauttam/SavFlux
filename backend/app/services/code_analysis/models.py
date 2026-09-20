"""
Shared result types for code analysis.

A Finding is deliberately richer than the old "emoji + line numbers" string:
the review agent needs the confidence and the evidence line so it can decide
what to tell the LLM and what to state as fact to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """
    Ordered severity. `str` mixin keeps it JSON-serialisable without a custom
    encoder, which matters because findings cross the SSE boundary.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Higher is worse. Used for sorting and for score penalties."""
        return {
            Severity.CRITICAL: 5,
            Severity.HIGH: 4,
            Severity.MEDIUM: 3,
            Severity.LOW: 2,
            Severity.INFO: 1,
        }[self]


@dataclass
class Finding:
    """
    One issue located at a specific line.

    `confidence` separates "the parser proved this" from "this looks
    suspicious". Only proven findings are worth blocking a merge over, and the
    UI renders them differently. A regex engine cannot make this distinction,
    which is why the old triage reported a parameterised query as dynamic SQL
    with the same emphasis as a real injection.
    """

    rule_id: str
    title: str
    severity: Severity
    line: int
    message: str
    #: Verbatim source line, so the reader can judge without opening the file.
    evidence: str = ""
    #: 0.0–1.0. Dataflow-proven findings are 0.9+; heuristics sit near 0.5.
    confidence: float = 0.8
    #: Concrete remediation, not "consider reviewing this".
    remediation: str = ""
    #: CWE id where one applies, e.g. "CWE-89". Empty for quality findings.
    cwe: str = ""
    end_line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data


@dataclass
class FunctionMetrics:
    """Per-function complexity, used to point reviewers at the risky parts."""

    name: str
    line: int
    end_line: int
    #: McCabe cyclomatic complexity — decision points + 1.
    complexity: int
    length: int
    #: Positional + keyword parameter count (self/cls excluded).
    params: int
    max_depth: int
    has_docstring: bool = False


@dataclass
class FileAnalysis:
    """Everything the deterministic pass learned about one file."""

    file_name: str
    language: str
    total_lines: int
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    findings: list[Finding] = field(default_factory=list)
    functions: list[FunctionMetrics] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    #: Set when the source could not be parsed (syntax error). The caller
    #: degrades to regex heuristics rather than reporting nothing.
    parse_error: str = ""

    @property
    def max_complexity(self) -> int:
        return max((f.complexity for f in self.functions), default=0)

    @property
    def avg_complexity(self) -> float:
        if not self.functions:
            return 0.0
        return sum(f.complexity for f in self.functions) / len(self.functions)

    def findings_by_severity(self, severity: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity is severity]

    def sorted_findings(self) -> list[Finding]:
        """Worst first; ties broken by line number for stable output."""
        return sorted(
            self.findings,
            key=lambda f: (-f.severity.rank, -f.confidence, f.line),
        )

    def risk_score(self) -> int:
        """
        1–10, where 10 is healthy.

        Severity-weighted rather than count-weighted: one proven SQL injection
        must outrank ten "line too long" notes. The old triage subtracted a
        flat 2 per security finding, so a file with five style nits scored the
        same as a file with a remote-code-execution hole.
        """
        penalty = 0.0
        for finding in self.findings:
            weight = {
                Severity.CRITICAL: 4.0,
                Severity.HIGH: 2.5,
                Severity.MEDIUM: 1.0,
                Severity.LOW: 0.4,
                Severity.INFO: 0.1,
            }[finding.severity]
            # A low-confidence finding should not tank the score on its own.
            penalty += weight * finding.confidence

        # Complexity is a real defect predictor, so it carries weight even when
        # no rule fired. McCabe > 20 is the usual "untestable" threshold.
        if self.max_complexity > 20:
            penalty += 2.0
        elif self.max_complexity > 10:
            penalty += 0.75

        return max(1, min(10, round(10 - penalty)))
