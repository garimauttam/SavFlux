"""
code_analysis — Deterministic program analysis for the review pipeline.

Everything here is CPU-only, offline, and free. The goal is to make a small
local model behave like a large one by handing it *facts* instead of asking it
to infer them: an AST-verified list of real vulnerabilities, cyclomatic
complexity per function, and exact line numbers. The LLM then spends its
limited reasoning budget on explanation and prioritisation rather than on
pattern-matching that a parser does perfectly.
"""

from app.services.code_analysis.models import Finding, FileAnalysis, Severity
from app.services.code_analysis.python_analyzer import analyze_python
from app.services.code_analysis.generic_analyzer import analyze_generic
from app.services.code_analysis.analyzer import analyze_file

__all__ = [
    "Finding",
    "FileAnalysis",
    "Severity",
    "analyze_file",
    "analyze_python",
    "analyze_generic",
]
