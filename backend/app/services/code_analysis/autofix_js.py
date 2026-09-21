"""
autofix_js.py — Deterministic repairs for JavaScript and TypeScript.

WHY THIS IS A SEPARATE MODULE FROM autofix.py
---------------------------------------------
`autofix.py` is Python-specific in a way that is not incidental: it holds an
`ast` tree, and its fixers reach for `node.lineno` positions in that tree. Nothing
in it can be reused for JS/TS, because the two stacks have nothing in common.

What IS shared, and shared deliberately, is the *contract*: `AppliedFix` and
`FixResult` are imported from `autofix.py` rather than redefined here. A second
set of result types would drift, and only one of them is covered by the existing
tests. Callers (`fix_service`, the review UI, the agent tools) should not have to
know which language a fix came from to render it.

THE THREE TRANSFORMATIONS
-------------------------
Each has exactly one correct form, decidable from the syntax tree:

    rejectUnauthorized: false                 → rejectUnauthorized: true
    NODE_TLS_REJECT_UNAUTHORIZED = '0' | 0    → NODE_TLS_REJECT_UNAUTHORIZED = '1'
    createHash('md5' | 'sha1')                → createHash('sha256')

Everything else in a JS/TS review is left alone on purpose. `eval(x)`, a
template literal in a SQL string, `innerHTML` with a variable, a hardcoded
secret: each needs a decision about intent that only the author can make. An
autofix that guesses gets merged, and one merged wrong fix costs more trust than
a hundred correct suggestions earn.

WHY EDITS ARE BYTE RANGES FROM A PARSE TREE, NOT REGEX ON LINES
---------------------------------------------------------------
The rule that raises these findings matches stripped source text. A fixer that
did the same could rewrite the wrong thing:

    // don't set rejectUnauthorized: false in prod
    const msg = 'never use createHash("md5")';

A regex fixer would "repair" a comment and a string literal, producing a diff
that changes nothing about the vulnerability while touching two lines a reviewer
must now re-read. Tree-sitter hands back the exact byte span of the *value*
node, so the edit lands on real code and nowhere else.

THE THREE GATES, IDENTICAL IN SPIRIT TO THE PYTHON PATH
-------------------------------------------------------
  1. **It parses.** Re-parse the candidate; it must not introduce a parse error
     that was not there before.
  2. **The finding is gone.** Otherwise the diff is noise.
  3. **Nothing worse appeared.** An autofix that trades a MEDIUM for a HIGH has
     made the codebase worse while looking like an improvement.

Plus one invariant particular to this module, asserted in `_verify`: **a fix
never changes the number of lines.** Every transformation here is an in-place
token replacement. That keeps the other findings' line numbers valid (so the rest
of the review still points at the right code) and keeps the diff minimal enough
to read. A future fix that needs to insert a line must relax this deliberately,
not by accident.

COST: no model, no network, no API key. One parse per file, ~1ms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.code_analysis.autofix import AppliedFix, FixResult
from app.services.code_analysis.js_analyzer import (
    TLS_ENV_VAR,
    WEAK_HASH_ALGORITHMS,
    _callee_name,
    _string_value,
    _text,
    _walk,
    error_rows,
    rows_overlap,
)
from app.services.code_analysis.models import Finding
from app.services.tree_sitter_langs import get_parser, grammar_for_path

logger = logging.getLogger(__name__)

#: Extensions whose findings this module can rewrite. Mirrors the grammars
#: `tree_sitter_langs` can resolve for the JS family; a language with no grammar
#: yields no fix sites, so a stale entry here degrades rather than breaks.
JS_EXTENSIONS = frozenset({"js", "jsx", "mjs", "cjs", "ts", "tsx", "mts", "cts"})

#: Rules this module can repair without human judgement.
FIXABLE_JS_RULES = frozenset({"GEN-SEC-TLS-OFF", "GEN-SEC-WEAKHASH"})

#: The replacement algorithm. SHA-256 is the right answer for integrity and
#: signatures; it is NOT the right answer for password storage, which needs
#: bcrypt/argon2 and a salt. That distinction is reported in the finding's
#: remediation and is why the Python path leaves `hashlib.md5` used on a password
#: to the developer. Here the caller has already been told.
_STRONG_HASH = "sha256"


@dataclass(frozen=True)
class _Site:
    """One place in the source that a fixer may rewrite."""

    rule_id: str
    start_byte: int
    end_byte: int
    replacement: str
    start_row: int
    end_row: int
    before: str
    description: str


def _requote(literal: str, content: str) -> str:
    """
    Re-wrap `content` in the same quote character the original literal used.

    Preserving the quote style keeps the diff to the characters that matter. On a
    Prettier- or ESLint-enforced repository, switching `'md5'` to `"sha256"` can
    fail `quote-props`/`quotes` and turn a security fix into a lint failure.
    """
    if literal[:1] in ("'", '"', "`"):
        return f"{literal[0]}{content}{literal[0]}"
    return f"'{content}'"


# ── Site discovery ────────────────────────────────────────────────────────────


def _find_sites(root: Any, source_bytes: bytes) -> list[_Site]:
    """
    Every rewritable position in the tree, in one pass.

    Sites are discovered independently of the findings that motivated them. A
    fixer that trusted `finding.line` and edited that line by offset would be a
    regex fixer wearing a tree, and would break the moment a file had two
    findings on one line or a multi-line call.
    """
    sites: list[_Site] = []
    errors = error_rows(root)

    for node in _walk(root):
        # Skip any construct inside an unparseable region. tree-sitter recovers
        # from syntax errors, so a node still exists — but its span in a file we
        # could not fully parse may not be the span we think it is, and this
        # module *rewrites* what it finds. The chunker already refuses to cite
        # such a definition for the same reason; a wrong edit is worse than a
        # missed one, because it is merged.
        if rows_overlap(node.start_point[0], node.end_point[0], errors):
            continue

        # ── rejectUnauthorized: false ────────────────────────────────────────
        # An object `pair`. The key may be written bare or quoted:
        #   { rejectUnauthorized: false }   and   { "rejectUnauthorized": false }
        if node.type == "pair":
            key = node.child_by_field_name("key")
            value = node.child_by_field_name("value")
            if key is not None and value is not None and value.type == "false":
                key_text = _string_value(key, source_bytes) or _text(key, source_bytes)
                if key_text == "rejectUnauthorized":
                    sites.append(_Site(
                        rule_id="GEN-SEC-TLS-OFF",
                        start_byte=value.start_byte,
                        end_byte=value.end_byte,
                        replacement="true",
                        start_row=node.start_point[0],
                        end_row=node.end_point[0],
                        before=_text(value, source_bytes),
                        description="Re-enable TLS certificate verification",
                    ))
            continue

        # ── NODE_TLS_REJECT_UNAUTHORIZED = '0' ───────────────────────────────
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is None or right is None or left.type != "member_expression":
                continue
            prop = left.child_by_field_name("property")
            if prop is None or _text(prop, source_bytes) != TLS_ENV_VAR:
                continue

            text = _text(right, source_bytes)
            value = _string_value(right, source_bytes)
            is_disabled = (value is not None and value.strip() == "0") or (
                right.type == "number" and text.strip() == "0"
            )
            if not is_disabled:
                continue

            replacement = _requote(text, "1") if right.type == "string" else "'1'"
            sites.append(_Site(
                rule_id="GEN-SEC-TLS-OFF",
                start_byte=right.start_byte,
                end_byte=right.end_byte,
                replacement=replacement,
                start_row=node.start_point[0],
                end_row=node.end_point[0],
                before=text,
                description="Re-enable TLS certificate verification",
            ))
            continue

        # ── createHash('md5' | 'sha1') ───────────────────────────────────────
        if node.type == "call_expression":
            if _callee_name(node.child_by_field_name("function"), source_bytes) != "createHash":
                continue
            arguments = node.child_by_field_name("arguments")
            if arguments is None:
                continue
            # Only position one selects the algorithm — see _Site discovery note
            # in test_js_analyzer for why scanning all arguments is a false
            # positive against `createHash('sha256', options)`.
            first = next((c for c in arguments.children if c.is_named), None)
            algorithm = _string_value(first, source_bytes)
            if algorithm is None or algorithm.lower() not in WEAK_HASH_ALGORITHMS:
                continue

            literal = _text(first, source_bytes)
            sites.append(_Site(
                rule_id="GEN-SEC-WEAKHASH",
                start_byte=first.start_byte,
                end_byte=first.end_byte,
                replacement=_requote(literal, _STRONG_HASH),
                start_row=node.start_point[0],
                end_row=node.end_point[0],
                before=literal,
                description=f"Replace {algorithm.upper()} with SHA-256",
            ))

    return sites


def _site_for(sites: list[_Site], finding: Finding) -> _Site | None:
    """
    The site a finding refers to: same rule, and a span containing its line.

    Matching on the line rather than on tree identity keeps this working after an
    earlier fix in the same file has been applied and the tree rebuilt.
    """
    for site in sites:
        if site.rule_id != finding.rule_id:
            continue
        if site.start_row <= finding.line - 1 <= site.end_row:
            return site
    return None


# ── Verification ──────────────────────────────────────────────────────────────


def _error_count(root: Any) -> int:
    """Number of ERROR or missing nodes — a parse-quality score for a candidate."""
    count = 0
    for node in _walk(root):
        if node.type == "ERROR" or node.is_missing:
            count += 1
    return count


def _verify(original: str, patched: str, finding: Finding, file_name: str, language: str) -> tuple[bool, str]:
    """
    Confirm a candidate fix is safe to keep. Three gates, plus the line invariant.

    Deliberately mirrors `autofix._verify` in structure and in wording of the
    failures, so a rejected fix reads the same whichever language produced it.
    """
    from app.services.code_analysis.analyzer import analyze_file

    grammar = grammar_for_path(file_name or language)
    parser = get_parser(grammar) if grammar else None
    if parser is None:
        return False, "the grammar is unavailable, so the result cannot be parsed"

    try:
        before_tree = parser.parse(original.encode("utf-8"))
        after_tree = parser.parse(patched.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — a parse failure must reject, not raise
        return False, f"result could not be parsed: {exc}"

    if _error_count(after_tree.root_node) > _error_count(before_tree.root_node):
        return False, "result does not parse"

    # Gate: the invariant that keeps every other finding's line number valid.
    if patched.count("\n") != original.count("\n"):
        return False, "the fix changed the number of lines"

    before = analyze_file(original, file_name, language)
    after = analyze_file(patched, file_name, language)

    still_present = any(
        f.rule_id == finding.rule_id and f.line == finding.line for f in after.findings
    )
    if still_present:
        return False, "finding still present after the fix"

    before_keys = {(f.rule_id, f.severity) for f in before.findings}
    new_serious = [
        f
        for f in after.findings
        if (f.rule_id, f.severity) not in before_keys and f.severity.rank >= finding.severity.rank
    ]
    if new_serious:
        return False, f"introduced a new {new_serious[0].severity.value} finding: {new_serious[0].rule_id}"

    return True, ""


def _line_text(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


# ── Entry point ───────────────────────────────────────────────────────────────


def autofix_javascript(
    source: str,
    findings: list[Finding],
    file_name: str = "",
    language: str = "",
) -> FixResult:
    """
    Apply every safe deterministic fix to a JavaScript/TypeScript file.

    One fix at a time, worst first, re-parsing between each — the same discipline
    as the Python path, for the same reason: a rejected fix must not block the
    others, and a later fix must see accurate positions.

    Returns `FixResult(content=source, fixes=[], rejected=[])` when nothing
    applies, which includes every non-JS file and every file whose grammar is
    unavailable. Callers can treat an empty result as "nothing to do", never as
    an error.
    """
    grammar = grammar_for_path(file_name or language)
    if grammar is None:
        return FixResult(content=source, fixes=[], rejected=[])

    current = source
    applied: list[AppliedFix] = []
    rejected: list[str] = []

    ordered = sorted(
        (f for f in findings if f.rule_id in FIXABLE_JS_RULES),
        key=lambda f: (-f.severity.rank, -f.line),
    )

    for finding in ordered:
        parser = get_parser(grammar)
        if parser is None:
            break
        try:
            tree = parser.parse(current.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("autofix parse failed for %s: %s", file_name, exc)
            break

        site = _site_for(_find_sites(tree.root_node, current.encode("utf-8")), finding)
        if site is None:
            # The finding was reported against an older revision of the file, or
            # the construct is one this module deliberately does not rewrite.
            continue

        patched = (
            current[: site.start_byte] + site.replacement + current[site.end_byte :]
        )
        if patched == current:
            continue

        ok, reason = _verify(current, patched, finding, file_name, language)
        if not ok:
            rejected.append(f"{finding.rule_id} (L{finding.line}): {reason}")
            continue

        applied.append(AppliedFix(
            rule_id=finding.rule_id,
            line=finding.line,
            description=site.description,
            before=_line_text(current, finding.line),
            after=_line_text(patched, finding.line),
        ))
        current = patched

    return FixResult(content=current, fixes=applied, rejected=rejected)
