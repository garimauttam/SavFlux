"""
autofix — Deterministic repairs for findings that have exactly one right answer.

Why not just ask the LLM?
------------------------
Some fixes are mechanical. `hashlib.md5(x)` used as a checksum becomes
`hashlib.md5(x, usedforsecurity=False)`. `requests.get(u, verify=False)` becomes
`requests.get(u)`. `token == secret` becomes `hmac.compare_digest(token, secret)`.
There is one correct transformation, it is decidable from the syntax tree, and a
language model asked to perform it will occasionally reformat the surrounding
code, drop a comment, or hallucinate an import. Worse, it costs a request and a
few seconds per file.

So the rules below are applied by rewriting source text at known offsets. Each
fix is:

  * **Verified** — the result must parse, and re-analysing it must show the
    finding gone and no NEW finding introduced. A fix that trades one bug for
    another is rejected and the file is left alone.
  * **Minimal** — only the offending span is touched. Indentation, comments and
    formatting elsewhere survive byte-for-byte, so the diff a reviewer reads
    contains only the security change.
  * **Free** — pure stdlib `ast`, no model call, ~1ms.

Fixes that require judgement (how to parameterise a particular SQL query, how
to split a 30-branch function) are deliberately NOT here. Those go to the LLM,
which is what it is good at. Mixing the two is how autofix tools earn their
reputation for breaking code.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

from app.services.code_analysis.models import Finding, Severity

logger = logging.getLogger(__name__)

#: Rules this module can repair without human judgement. Anything not listed
#: is left for the LLM or the developer — an autofix that guesses is worse than
#: no autofix, because it gets merged.
FIXABLE_RULES = frozenset(
    {
        "PY-SEC-NOVERIFY",
        "PY-SEC-WEAKHASH",
        "PY-SEC-MKTEMP",
        "PY-SEC-TIMING",
        "PY-SEC-DESERIALIZE",
        "PY-EXC-BARE",
    }
)


@dataclass
class AppliedFix:
    """One repair that was made, for the PR body and the UI."""

    rule_id: str
    line: int
    description: str
    before: str
    after: str


@dataclass
class FixResult:
    """Outcome of running autofix over one file."""

    #: Repaired source. Identical to the input when nothing could be fixed.
    content: str
    fixes: list[AppliedFix]
    #: Findings that were matched but whose repair failed verification.
    rejected: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.fixes)


def _line_span(source_lines: list[str], line: int) -> tuple[int, int]:
    """Absolute character offsets of a 1-indexed line, excluding its newline."""
    start = sum(len(l) for l in source_lines[: line - 1])
    return start, start + len(source_lines[line - 1].rstrip("\n"))


def _find_call_at(tree: ast.AST, line: int, predicate) -> ast.Call | None:
    """First Call node on `line` satisfying `predicate`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.lineno == line and predicate(node):
            return node
    return None


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _segment(source: str, node: ast.AST) -> str:
    """Exact source text of a node, using its recorded offsets."""
    return ast.get_source_segment(source, node) or ""


# ── Individual fixes ──────────────────────────────────────────────────────────
#
# Each returns the new source, or None when it cannot fix this instance safely.


def _fix_verify_false(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """
    `requests.get(url, verify=False)` -> `requests.get(url)`.

    Removing the argument restores the library default (verify=True) rather
    than writing `verify=True` explicitly, which keeps the call site clean.
    """
    call = _find_call_at(
        tree,
        finding.line,
        lambda n: any(
            kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False
            for kw in n.keywords
        ),
    )
    if call is None:
        return None

    call_text = _segment(source, call)
    if not call_text:
        return None

    # Remove the keyword and any comma that becomes redundant. Handled with a
    # regex over the call's own text (not the whole file) so nothing outside
    # the call can be touched.
    patched = re.sub(r",\s*verify\s*=\s*False", "", call_text)
    if patched == call_text:
        patched = re.sub(r"verify\s*=\s*False\s*,\s*", "", call_text)
    if patched == call_text:
        return None

    return source.replace(call_text, patched, 1)


def _fix_weak_hash(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """
    `hashlib.md5(data)` -> `hashlib.md5(data, usedforsecurity=False)`.

    This does NOT claim md5 is now safe. It records that this call site is a
    checksum rather than a security primitive, which is the honest fix when the
    hash feeds a cache key. If it really was security-relevant the algorithm
    must change, and that is a judgement call left to a human.
    """
    call = _find_call_at(
        tree,
        finding.line,
        lambda n: _dotted_name(n.func).startswith("hashlib.")
        and _dotted_name(n.func).rsplit(".", 1)[-1] in ("md5", "sha1")
        and not any(kw.arg == "usedforsecurity" for kw in n.keywords),
    )
    if call is None:
        return None

    call_text = _segment(source, call)
    if not call_text or not call_text.rstrip().endswith(")"):
        return None

    stripped = call_text.rstrip()
    inner = stripped[:-1].rstrip()
    # `md5()` with no arguments takes no leading comma.
    separator = "" if inner.endswith("(") else ", "
    patched = f"{inner}{separator}usedforsecurity=False)"
    return source.replace(call_text, patched, 1)


def _fix_mktemp(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """`tempfile.mktemp()` -> `tempfile.mkstemp()`, which creates atomically."""
    call = _find_call_at(tree, finding.line, lambda n: _dotted_name(n.func) == "tempfile.mktemp")
    if call is None:
        return None
    call_text = _segment(source, call)
    if not call_text:
        return None
    return source.replace(call_text, call_text.replace("mktemp", "mkstemp", 1), 1)


def _fix_timing_comparison(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """
    `token == expected` -> `hmac.compare_digest(token, expected)`.

    Only rewrites a bare `==` between two simple expressions. A comparison
    chained with `and`/`or`, or one inside a larger boolean expression, is left
    alone: the rewrite is still correct there but the diff becomes hard to read,
    and an unreadable security diff does not get merged.
    """
    target: ast.Compare | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and node.lineno == finding.line
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
        ):
            target = node
            break
    if target is None:
        return None

    left = _segment(source, target.left)
    right = _segment(source, target.comparators[0])
    original = _segment(source, target)
    if not (left and right and original):
        return None

    patched = source.replace(original, f"hmac.compare_digest({left}, {right})", 1)
    return _ensure_import(patched, "hmac")


def _fix_yaml_load(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """`yaml.load(x)` -> `yaml.safe_load(x)` when no Loader was specified."""
    call = _find_call_at(
        tree,
        finding.line,
        lambda n: _dotted_name(n.func) == "yaml.load"
        and not any(kw.arg == "Loader" for kw in n.keywords)
        and len(n.args) == 1,
    )
    if call is None:
        return None
    call_text = _segment(source, call)
    if not call_text:
        return None
    return source.replace(call_text, call_text.replace("yaml.load", "yaml.safe_load", 1), 1)


def _fix_bare_except(source: str, tree: ast.AST, finding: Finding) -> str | None:
    """
    `except:` -> `except Exception:`.

    A bare except also catches KeyboardInterrupt and SystemExit, so a Ctrl-C
    gets swallowed by error-handling code. `except Exception:` is the intended
    behaviour in essentially every case this fires on.
    """
    lines = source.splitlines(keepends=True)
    if not (1 <= finding.line <= len(lines)):
        return None

    line = lines[finding.line - 1]
    if not re.match(r"^\s*except\s*:", line):
        return None

    lines[finding.line - 1] = re.sub(r"^(\s*)except\s*:", r"\1except Exception:", line, count=1)
    return "".join(lines)


def _ensure_import(source: str, module: str) -> str:
    """
    Add `import <module>` if absent, below the existing import block.

    Placing it after the last top-level import (rather than at line 1) keeps
    the file's import grouping intact and avoids inserting above a shebang,
    encoding declaration, or module docstring.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == module for a in node.names):
            return source
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return source

    last_import_line = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_line = max(last_import_line, getattr(node, "end_lineno", node.lineno))

    lines = source.splitlines(keepends=True)
    if last_import_line == 0:
        # No imports: insert after the docstring if there is one.
        insert_at = 0
        if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
            insert_at = getattr(tree.body[0], "end_lineno", 1)
        lines.insert(insert_at, f"import {module}\n")
    else:
        lines.insert(last_import_line, f"import {module}\n")

    return "".join(lines)


#: rule_id -> fixer. Keys must be a subset of FIXABLE_RULES.
_FIXERS = {
    "PY-SEC-NOVERIFY": _fix_verify_false,
    "PY-SEC-WEAKHASH": _fix_weak_hash,
    "PY-SEC-MKTEMP": _fix_mktemp,
    "PY-SEC-TIMING": _fix_timing_comparison,
    "PY-SEC-DESERIALIZE": _fix_yaml_load,
    "PY-EXC-BARE": _fix_bare_except,
}


# ── Verification ──────────────────────────────────────────────────────────────


def _verify(original: str, patched: str, finding: Finding, file_name: str) -> tuple[bool, str]:
    """
    Confirm a candidate fix is safe to keep.

    Three conditions, all necessary:
      1. It parses. A fix that breaks the file is catastrophic.
      2. The targeted finding is gone. Otherwise the fix did not work and the
         diff is noise.
      3. No new finding of equal or higher severity appeared. This is the one
         that matters most — an autofix which trades a MEDIUM for a HIGH has
         made the codebase worse while appearing to improve it.
    """
    # Imported here to avoid a circular import at module load.
    from app.services.code_analysis.python_analyzer import analyze_python

    try:
        ast.parse(patched)
    except SyntaxError as exc:
        return False, f"result does not parse: {exc.msg}"

    before = analyze_python(original, file_name)
    after = analyze_python(patched, file_name)

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


# ── Entry point ───────────────────────────────────────────────────────────────


def autofix_python(source: str, findings: list[Finding], file_name: str = "") -> FixResult:
    """
    Apply every safe deterministic fix to a Python file.

    Fixes are applied one at a time, worst first, re-parsing between each so
    later fixes see accurate line numbers. Any fix that fails verification is
    discarded and the rest continue — one unfixable finding must not block the
    others.
    """
    current = source
    applied: list[AppliedFix] = []
    rejected: list[str] = []

    # Worst first so that if two fixes touch the same line, the more important
    # one wins. Descending line order within a severity keeps earlier offsets
    # valid for longer, though re-parsing makes that a belt-and-braces measure.
    ordered = sorted(
        (f for f in findings if f.rule_id in FIXABLE_RULES),
        key=lambda f: (-f.severity.rank, -f.line),
    )

    for finding in ordered:
        fixer = _FIXERS.get(finding.rule_id)
        if fixer is None:
            continue

        try:
            tree = ast.parse(current)
        except SyntaxError:
            break  # a previous fix broke it; stop rather than compound the damage

        # Re-locate the finding in the current text: earlier fixes may have
        # inserted an import and shifted every line below it.
        relocated = _relocate(current, finding, file_name)
        if relocated is None:
            continue

        try:
            patched = fixer(current, tree, relocated)
        except Exception as exc:  # noqa: BLE001 — a fixer bug must not 500 the request
            logger.warning("autofix %s raised on %s: %s", finding.rule_id, file_name, exc)
            rejected.append(f"{finding.rule_id} (L{finding.line}): internal error")
            continue

        if patched is None or patched == current:
            continue

        ok, reason = _verify(current, patched, relocated, file_name)
        if not ok:
            rejected.append(f"{finding.rule_id} (L{relocated.line}): {reason}")
            continue

        applied.append(
            AppliedFix(
                rule_id=finding.rule_id,
                line=relocated.line,
                description=finding.title,
                before=_line_text(current, relocated.line),
                after=_line_text(patched, relocated.line),
            )
        )
        current = patched

    return FixResult(content=current, fixes=applied, rejected=rejected)


def _relocate(source: str, finding: Finding, file_name: str) -> Finding | None:
    """
    Find the current line of a finding after earlier edits shifted the file.

    Re-analysing is cheap (~1ms) and exact, which beats tracking offset deltas
    through every fixer.
    """
    from app.services.code_analysis.python_analyzer import analyze_python

    matches = [f for f in analyze_python(source, file_name).findings if f.rule_id == finding.rule_id]
    if not matches:
        return None
    # Prefer an exact line match; otherwise the nearest one below it.
    exact = next((f for f in matches if f.line == finding.line), None)
    return exact or min(matches, key=lambda f: abs(f.line - finding.line))


def _line_text(source: str, line: int) -> str:
    lines = source.splitlines()
    return lines[line - 1].strip() if 1 <= line <= len(lines) else ""


def summarise_fixes(result: FixResult) -> str:
    """
    One-line-per-fix markdown for the PR body.

    Rejections are reported even when nothing was fixed. An earlier version
    returned "no fixes were applicable" whenever `fixes` was empty, which
    swallowed the rejection list entirely — so a fix that failed verification
    looked identical to a rule that never matched, and a user could believe a
    vulnerability had been considered and dismissed when it had actually been
    attempted and abandoned.
    """
    lines = [f"- `L{f.line}` **{f.description}** ({f.rule_id})" for f in result.fixes]

    if not result.fixes:
        lines.append("_No automatic fixes were applicable._")

    if result.rejected:
        if result.fixes:
            lines.append("")
        lines.append("_Skipped (failed verification — fix these by hand):_")
        lines.extend(f"- {reason}" for reason in result.rejected)

    return "\n".join(lines)
