"""
js_analyzer.py — Structural JS/TS detection for what the regex pipeline cannot see.

THE BUG THIS EXISTS TO FIX
--------------------------
`generic_analyzer` strips comments and blanks every string literal before it
matches any rule:

    pattern.search(line.code)          # `line.code` has string CONTENTS blanked

That is the right default — it is why `"do not use md5()"` in prose does not
raise a finding, and why a commented-out `eval(x)` stays quiet. But two shipped
security patterns depend on the *contents* of a string literal, so they can never
match. They are dead code:

    GEN-SEC-WEAKHASH   createHash\\s*\\(\\s*["'](md5|sha1)["']

        const h = crypto.createHash('md5')...
          → line.code becomes  createHash('   ')
          → no match. MD5 and SHA-1 hashing in Node is not reported at all.

    GEN-SEC-TLS-OFF    NODE_TLS_REJECT_UNAUTHORIZED\\s*=\\s*["']?0

        process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
          → line.code becomes  ... = ' '
          → no match. TLS verification disabled via the documented Node escape
            hatch is not reported. (The *unquoted* `= 0` form does match, which
            is why the gap looks like it works.)

Both were verified by running the analyzer, not by reading the regexes. The
patterns match fine against raw text — they simply never see it.

WHY TREE-SITTER IS THE FIX, AND NOT A LOOSER REGEX
--------------------------------------------------
The tempting repair is to match these two rules against the raw line instead of
the stripped one. That reintroduces exactly the false positives the stripping
exists to prevent: a comment saying "don't use createHash('md5')" or a test
fixture containing the string would both start reporting.

A regex cannot distinguish a string literal used as an argument from a string
literal used as documentation. A parse tree can:

    call_expression
      function:   createHash          ← the callee, not text in a string
      arguments:
        string '"md5"'                ← the argument, with quotes

So this module detects those constructs structurally. It is deliberately narrow:
it reports ONLY the string-content-dependent patterns that the regex pass is
incapable of reaching. Everything else stays with the regexes, so this cannot
shift the existing detection numbers — it only adds.

Findings here carry confidence 0.95 rather than the regex rules' 0.7, because
"a parse tree proves it" is a stronger claim than "a line matched a pattern".
That distinction is the one the Finding model already documents.

COST: no model, no network. One parse per file, already cached per thread by
`tree_sitter_langs`, and ~1ms for a typical source file.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.code_analysis.models import Finding, Severity
from app.services.tree_sitter_langs import get_parser, grammar_for_path

logger = logging.getLogger(__name__)

#: Grammars this module analyses. Other languages keep the regex-only path.
JS_FAMILY_GRAMMARS = frozenset({"javascript", "typescript", "tsx"})

#: Algorithms that are collision-broken for integrity or signature use.
WEAK_HASH_ALGORITHMS = frozenset({"md5", "sha1"})

#: The Node escape hatch that disables TLS certificate checking process-wide.
TLS_ENV_VAR = "NODE_TLS_REJECT_UNAUTHORIZED"


# ── tree helpers ──────────────────────────────────────────────────────────────


def _text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _string_value(node: Any, source_bytes: bytes) -> str | None:
    """
    The contents of a string literal, without its quotes.

    Returns None for anything that is not a plain string literal, so a template
    literal or an identifier argument is skipped rather than guessed at.
    """
    if node is None or node.type not in ("string", "template_string"):
        return None
    raw = _text(node, source_bytes)
    if node.type == "template_string":
        # A template literal is only a fixed value when it has no substitutions.
        if "${" in raw:
            return None
        return raw[1:-1]
    if len(raw) < 2 or raw[0] not in "\"'`":
        return None
    return raw[1:-1]


def _callee_name(node: Any, source_bytes: bytes) -> str:
    """
    The rightmost name of a call target: `crypto.createHash` → `createHash`.

    Matching on the last segment keeps `createHash`, `crypto.createHash` and
    `createHash` destructured from the module all working, which is how the same
    function is called in practice. It also deliberately does not match
    `myCreateHash`, since only a member access or a whole identifier is accepted.
    """
    if node is None:
        return ""
    if node.type == "identifier":
        return _text(node, source_bytes)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return _text(prop, source_bytes) if prop is not None else ""
    return ""


def _walk(node: Any):
    """Depth-first walk, including the root."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def _line_text(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()[:200]
    return ""


def error_rows(root: Any) -> list[tuple[int, int]]:
    """
    Row ranges (0-indexed, inclusive) covered by ERROR or missing nodes.

    tree-sitter recovers from a syntax error and still returns a tree, which is
    usually a feature — it is how a half-finished file still gets analysed. But a
    construct *inside* an error region has a span we cannot fully trust, and both
    consumers of this module need to know:

      - detection will not report a finding whose evidence we cannot read
      - autofix will not rewrite code it may have misread

    Children of an ERROR node are not descended into; the error itself is the
    whole story.
    """
    rows: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            rows.append((node.start_point[0], node.end_point[0]))
            continue
        stack.extend(node.children)
    return rows


def rows_overlap(start_row: int, end_row: int, rows: list[tuple[int, int]]) -> bool:
    """
    Whether a row span intersects any error region.

    One helper for two callers with different shapes — detection has a line
    number, autofix has a node's row span — so the overlap rule is written down
    once. Two implementations of it would drift, and the difference would show up
    as a fix being applied to code the detector refused to report.
    """
    if not rows:
        return False
    return any(start_row <= err_end and err_start <= end_row for err_start, err_end in rows)


# ── detectors ─────────────────────────────────────────────────────────────────


def _detect_weak_hash_calls(root: Any, source_bytes: bytes, source: str) -> list[Finding]:
    """
    `createHash('md5' | 'sha1')` — a cryptographic hash from a broken algorithm.

    Only the string-argument form is reported here. The bare-call form
    (`md5(...)`, as Python's `hashlib.md5(x)` writes it) carries no string
    literal, so the existing regex already catches it and reporting it twice
    would double-count one problem.
    """
    findings: list[Finding] = []

    for node in _walk(root):
        if node.type != "call_expression":
            continue
        if _callee_name(node.child_by_field_name("function"), source_bytes) != "createHash":
            continue

        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            continue
        # Only the first argument selects the algorithm.
        first = next((c for c in arguments.children if c.is_named), None)
        algorithm = _string_value(first, source_bytes)
        if algorithm is None or algorithm.lower() not in WEAK_HASH_ALGORITHMS:
            continue

        line = node.start_point[0] + 1
        findings.append(Finding(
            rule_id="GEN-SEC-WEAKHASH",
            title="Weak hash algorithm",
            severity=Severity.MEDIUM,
            line=line,
            message=f"`createHash(\"{algorithm}\")` uses {algorithm.upper()}, which is "
            "collision-broken. A hashing algorithm chosen in code is a decision, not "
            "a coincidence, so this is not a false positive to be tolerated.",
            evidence=_line_text(source, line),
            confidence=0.95,      # proven by the parse tree, not matched by a pattern
            remediation="Use SHA-256 for integrity, or bcrypt/argon2 for passwords.",
            cwe="CWE-327",
        ))

    return findings


def _detect_tls_reject_unauthorized(root: Any, source_bytes: bytes, source: str) -> list[Finding]:
    """
    `{ rejectUnauthorized: false }` — certificate checking turned off for a client.

    This is the second half of the same reachability problem. The regex for it is

        rejectUnauthorized\\s*:\\s*false

    which requires the colon to follow the name directly. A **quoted** key puts a
    quote in between, so

        { 'rejectUnauthorized': false }
        { "rejectUnauthorized": false }

    disables TLS verification and is never reported. Quoted keys are ordinary
    style in config-shaped objects, so this is a bypass, not a curiosity.

    The unquoted form is caught by the regex as well; findings merge on
    (rule_id, line), so reporting it here too does not double-count it.
    """
    findings: list[Finding] = []

    for node in _walk(root):
        if node.type != "pair":
            continue
        key = node.child_by_field_name("key")
        value = node.child_by_field_name("value")
        if key is None or value is None or value.type != "false":
            continue
        # Accept both spellings of the key: bare, or quoted as a string.
        key_text = _string_value(key, source_bytes) or _text(key, source_bytes)
        if key_text != "rejectUnauthorized":
            continue

        line = node.start_point[0] + 1
        findings.append(Finding(
            rule_id="GEN-SEC-TLS-OFF",
            title="TLS verification disabled",
            severity=Severity.HIGH,
            line=line,
            message="`rejectUnauthorized: false` turns off certificate checking for "
            "every request this client makes, so any machine on the network path "
            "can impersonate the server and read the traffic.",
            evidence=_line_text(source, line),
            confidence=0.95,
            remediation="Set it to true. For a private CA, add its root certificate "
            "instead of disabling verification.",
            cwe="CWE-295",
        ))

    return findings


def _detect_tls_env_override(root: Any, source_bytes: bytes, source: str) -> list[Finding]:
    """
    `process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'` — certificate checks off.

    Node reads this variable at connection time, so setting it disables
    verification for every outgoing request in the process, not just one. The
    unquoted `= 0` form is already caught by the regex; the quoted form is not,
    and the quoted form is the one Node's own documentation shows.
    """
    findings: list[Finding] = []

    for node in _walk(root):
        if node.type != "assignment_expression":
            continue

        left = node.child_by_field_name("left")
        if left is None or left.type != "member_expression":
            continue
        prop = left.child_by_field_name("property")
        if prop is None or _text(prop, source_bytes) != TLS_ENV_VAR:
            continue

        right = node.child_by_field_name("right")
        if right is None:
            continue
        # `'0'` and `0` both disable verification; the string form is the one the
        # regex cannot reach, and a number literal is included so a file using
        # both spellings reports both lines rather than one.
        value = _string_value(right, source_bytes)
        disabled = (value is not None and value.strip() == "0") or (
            right.type == "number" and _text(right, source_bytes).strip() == "0"
        )
        if not disabled:
            continue

        line = node.start_point[0] + 1
        findings.append(Finding(
            rule_id="GEN-SEC-TLS-OFF",
            title="TLS verification disabled",
            severity=Severity.HIGH,
            line=line,
            message=f"`{TLS_ENV_VAR}` is set to 0, which turns off certificate "
            "verification for every outgoing request in this process — not just "
            "one client. Any machine on the network path can impersonate the server.",
            evidence=_line_text(source, line),
            confidence=0.95,
            remediation="Delete the assignment. For a private CA, install its root "
            "certificate and set NODE_EXTRA_CA_CERTS instead.",
            cwe="CWE-295",
        ))

    return findings


# ── entry point ───────────────────────────────────────────────────────────────


def detect_structural_issues(
    source: str, file_name: str = "", language: str = ""
) -> list[Finding]:
    """
    Findings that require literal string contents, found by parsing.

    Returns `[]` — never raises — for non-JS files, for an unavailable grammar,
    and for a file that does not parse. The regex pass already ran and reported
    whatever it could; this only adds.
    """
    grammar = grammar_for_path(file_name or language)
    if grammar not in JS_FAMILY_GRAMMARS:
        return []
    parser = get_parser(grammar)
    if parser is None:
        return []

    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception as exc:  # noqa: BLE001 - detection must never break a review
        logger.warning("structural detection failed to parse %s: %s", file_name, exc)
        return []

    root = tree.root_node
    findings: list[Finding] = []

    # A construct inside an error region has a span we cannot fully trust, so its
    # evidence is not trustworthy either. Skipping is the honest outcome: the
    # regex pass already reported whatever it could, and a finding whose quoted
    # evidence line is wrong is worse than a missed one.
    errors = error_rows(root)

    for detector in (
        _detect_weak_hash_calls,
        _detect_tls_reject_unauthorized,
        _detect_tls_env_override,
    ):
        for finding in detector(root, source_bytes, source):
            # Rows are 0-indexed; finding.line is 1-indexed.
            if rows_overlap(finding.line - 1, finding.line - 1, errors):
                continue
            findings.append(finding)

    return findings
