"""
grounding_check — does the answer cite what it was actually given?

WHY THIS EXISTS
---------------
`SYSTEM_PREFIX` tells the model to cite only the line ranges in the snippet
headers. That instruction is a request, not a guarantee, and nothing in the
chat path was checking the reply. A model that cites a file it was never shown
renders in the UI exactly like one that did not: same weight, same backticks,
same apparent authority, with a row of trustworthy-looking source chips beside
it. The reader has no way to tell the difference without opening every file.

Observed on a local `qwen2.5-coder:14b`: asked what would break if the
embedding model changed, it returned the `## Bugs & Issues` review template for
a question that was not a review, declared the snippets insufficient *while
citing files that were in them*, and then cited `test_tree_sitter_langs.py`
and `review_agent.py` — neither of which was in its context.

This module is the check that was missing. It is deliberately pure and
deterministic: no model call, no network, no heuristics that need a second model
to grade. It reports what the answer *claims* against what it was *given*, and
lets the UI say so.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not suppress the answer, score it, or refuse to render it. A grounded
but wrong answer is still information the reader can use, and hiding it would
trade an honest-but-wrong answer — which a careful reader can catch — for a
silent one, which they cannot. The finding is attached to the answer, loudly.

It also does not verify that a cited line *supports* the claim made about it.
That needs a judgement about meaning, not a range comparison, and a cheap model
approximation of it is worse than nothing: it would manufacture false
accusations of hallucination. Checking whether a citation survives review is
recorded separately, against the answer, once a person has looked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# A citation as the model writes it: `auth.py:42`, `auth.py:42-58`,
# `app/api/auth.py:42`. The lookbehind keeps it from matching the tail of a URL
# path (`https://x.dev/a:1`) and the leading `[\w/]` keeps the filename anchored
# to a word boundary, so `foo.py:1` inside `` `foo.py:1` `` still matches while
# `xfoo.py:1` is not read as a reference to `foo.py`.
_CITATION_RE = re.compile(
    r"(?<![\w/])([A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)*\.[A-Za-z0-9_]+)"
    r":(\d+)(?:\s*[-–—]\s*(\d+))?"
)

# The same claim written the other way round: a range in words, the file in a
# separate span. Seen in a real 14B answer as "**Line 76-83** in
# `test_tree_sitter_langs.py`" — a precise, checkable citation that a pattern
# matching only `file:line` walks straight past. Requiring the literal word
# "Line"/"Lines" keeps "version 3 in config.py" from being read as one.
_RANGE_REF_RE = re.compile(
    r"\bLines?\s+(\d+)(?:\s*[-–—]\s*(\d+))?[*_\s]*\s*(?:in|from|of)\s+"
    r"`?([A-Za-z0-9_][A-Za-z0-9_.\-]*(?:/[A-Za-z0-9_.\-]+)*\.[A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)

# Phrases with which a model declines to answer for want of evidence. The system
# prompt *asks* for this when the evidence is thin, so seeing one is not a fault
# — which is why these produce a finding about retrieval, not an accusation
# about the model. What matters is whether anything was delivered anyway.
_INSUFFICIENT_PHRASES = (
    "do not contain enough information",
    "doesn't contain enough information",
    "does not contain enough information",
    "not contain enough information",
    "not enough information",
    "insufficient information",
    "insufficient context",
    "unable to determine",
    "cannot determine",
    "can't determine",
    "could not determine",
    "no relevant code",
    "not enough context",
)

# A refusal is a *finding about retrieval* only when nothing was delivered with
# it. A model that says the evidence is thin and then answers anyway has done
# the right thing, so the test for "substantive" is what the answer contains,
# not which phrase introduced it.
_SUBSTANTIVE_RE = re.compile(
    r"```"                                  # a code block
    r"|(?<![\w/])[\w.\-/]+\.\w+:\d+"        # any citation at all
    r"|^\s{0,3}[-*+]\s"                     # a bullet
    r"|^\s{0,3}\d+[.)]\s"                   # a numbered item
    r"|^\s{0,3}#{1,6}\s",                   # a heading
    re.MULTILINE,
)


@dataclass
class UnverifiedCitation:
    """One `file:line` the answer claims that the evidence does not contain."""

    reference: str
    file_name: str
    line_start: int
    line_end: int
    reason: str  # "not_in_context" | "lines_outside_context"
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "file_name": self.file_name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class GroundingReport:
    """What the answer claimed, what it was given, and where they disagree."""

    citations_claimed: int = 0
    unverified: list[UnverifiedCitation] = field(default_factory=list)
    insufficient_evidence: bool = False
    delivered_anyway: bool = True
    matched_phrase: str = ""

    @property
    def is_clean(self) -> bool:
        return not self.unverified and not (
            self.insufficient_evidence and not self.delivered_anyway
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "citations_claimed": self.citations_claimed,
            "unverified": [c.to_dict() for c in self.unverified],
            "unverified_count": len(self.unverified),
            "insufficient_evidence": self.insufficient_evidence,
            "delivered_anyway": self.delivered_anyway,
            "matched_phrase": self.matched_phrase,
            "is_clean": self.is_clean,
        }


def _basename(reference: str) -> str:
    """`app/api/auth.py` -> `auth.py`.

    The model usually cites the bare filename even when the snippet header
    carried a path, so the comparison has to be on the tail. Two files with the
    same basename in different directories would collide here; that is accepted
    deliberately rather than guessed at, because the alternative is refusing to
    check citations for the common case to protect a rare one. A collision can
    only ever make the check *more* permissive, never falsely accusatory.
    """
    return reference.rsplit("/", 1)[-1]


def _covered_ranges(citation: dict[str, Any]) -> list[tuple[int, int]]:
    """The exact line spans of one served source, best representation first.

    A source may carry `line_ranges` (discontinuous evidence, e.g. a module
    chunk that gathered imports plus scattered constants) or a single
    `start_line`/`end_line` hull. Using the hull when the exact ranges are known
    would report a citation inside a real region as out of context.
    """
    from app.services.ast_chunker import parse_line_ranges

    encoded = citation.get("line_ranges")
    if encoded:
        parsed = parse_line_ranges(encoded)
        if parsed:
            return parsed

    start = citation.get("start_line")
    end = citation.get("end_line")
    if isinstance(start, int) and isinstance(end, int):
        return [(start, end)]
    # No line information at all. The model was told to cite the file name alone
    # in that case, so a bare `file.py:10` cannot be checked — and guessing a
    # verdict here is how a check like this starts crying wolf.
    return []


def _claimed_references(answer: str) -> list[tuple[str, str, int, int]]:
    """
    Every `file`/`line` claim in the answer, in the order it was written.

    Returns `(reference, basename, start, end)` for each. The two citation
    forms overlap in meaning, so an answer that writes the same claim both ways
    yields one entry with the later occurrence ignored by the caller's dedupe —
    counting "this file is cited twice" when it is cited once in two notations
    would report a model defect that does not exist.
    """
    found: list[tuple[int, str, int, int]] = []

    for match in _CITATION_RE.finditer(answer):
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else start
        name = _basename(match.group(1))
        found.append((match.start(), name, start, end))

    for match in _RANGE_REF_RE.finditer(answer):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        found.append((match.start(), _basename(match.group(3)), start, end))

    found.sort(key=lambda item: item[0])
    # Both notations normalise to the same `file:line` form, so the dedupe in
    # the caller and everything the reader sees are the same whichever way the
    # model chose to write it.
    return [
        (f"{name}:{start}-{end}" if end != start else f"{name}:{start}", start, end)
        for _, name, start, end in found
    ]


def check_grounding(
    answer: str,
    sources: Sequence[dict[str, Any]],
) -> GroundingReport:
    """
    Compare the citations in `answer` against the sources it was given.

    `sources` is the same list streamed to the UI as `__SOURCES__`, so what is
    checked and what is displayed cannot drift apart.
    """
    report = GroundingReport()

    if not answer or not answer.strip():
        return report

    by_name: dict[str, list[dict[str, Any]]] = {}
    for citation in sources or ():
        name = citation.get("file_name")
        if name:
            by_name.setdefault(name, []).append(citation)

    seen: set[tuple[str, int, int]] = set()
    for reference, start, end in _claimed_references(answer):
        if end < start:
            start, end = end, start
        name = reference.rsplit(":", 1)[0]
        key = (name, start, end)
        if key in seen:
            continue
        seen.add(key)
        report.citations_claimed += 1

        candidates = by_name.get(name)
        if not candidates:
            # The snippet header may have carried a path (`app/api/auth.py`)
            # while the model cited the bare name, or the reverse. Matching on
            # the tail of `source` covers both directions.
            #
            # This is deliberately an `endswith` on the name ALREADY derived
            # above. An earlier version also compared against text stripped out
            # of the matched reference, which ended in a backtick for every
            # backticked citation — and `"anything".endswith("")` is True, so
            # the fallback matched every source and the not-in-context check
            # silently never fired.
            candidates = [
                c for c in sources or ()
                if str(c.get("source", "")).endswith(name)
            ]
        if not candidates:
            report.unverified.append(
                UnverifiedCitation(
                    reference=reference,
                    file_name=name,
                    line_start=start,
                    line_end=end,
                    reason="not_in_context",
                    detail=(
                        f"`{name}` was cited but is not among the "
                        f"{len(by_name)} file(s) this answer was given"
                    ),
                )
            )
            continue

        ranges = [r for c in candidates for r in _covered_ranges(c)]
        if not ranges:
            # The file was served without line numbers, so there is nothing to
            # compare against. Counting it as unverified would report a gap in
            # our own metadata as a fabrication by the model.
            continue

        if not any(
            low <= start and end <= high or low <= start <= high
            for low, high in ranges
        ):
            spans = ", ".join(f"{low}-{high}" for low, high in ranges)
            report.unverified.append(
                UnverifiedCitation(
                    reference=reference,
                    file_name=name,
                    line_start=start,
                    line_end=end,
                    reason="lines_outside_context",
                    detail=(
                        f"`{name}` was cited at lines {start}-{end}, but the "
                        f"evidence for it covers {spans}"
                    ),
                )
            )

    lowered = answer.lower()
    for phrase in _INSUFFICIENT_PHRASES:
        if phrase in lowered:
            report.insufficient_evidence = True
            report.matched_phrase = phrase
            break
    report.delivered_anyway = bool(_SUBSTANTIVE_RE.search(answer))

    return report


def format_grounding_notice(report: GroundingReport) -> str:
    """A one-line, plain-language rendering of the report for the UI.

    Says what was found, not what it implies. The reader decides what an
    out-of-context citation means for the answer they just read.
    """
    if report.is_clean:
        return ""

    parts: list[str] = []
    if report.unverified:
        count = len(report.unverified)
        names = ", ".join(dict.fromkeys(c.file_name for c in report.unverified))
        parts.append(
            f"{count} citation{'s' if count > 1 else ''} point"
            f"{'s' if count > 1 else ''} outside the evidence shown "
            f"({names})"
        )
    if report.insufficient_evidence and not report.delivered_anyway:
        parts.append(
            "the model said the retrieved code was not enough to answer"
        )

    if not parts:
        return ""
    return "Answer check: " + "; ".join(parts) + "."
