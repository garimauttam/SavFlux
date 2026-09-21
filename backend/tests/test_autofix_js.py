"""
test_autofix_js.py — Deterministic JS/TS repairs.

WHY THESE TESTS ARE MOSTLY NEGATIVE CASES
-----------------------------------------
A fixer's failure mode is not "it crashed". It is "it rewrote something that was
already correct", or "it rewrote a comment". Both produce a diff that looks like
progress, passes a casual review, and either changes nothing or changes the wrong
line. So most of what follows checks that a fix does NOT happen:

  - a comment that mentions the vulnerable pattern
  - a string literal that contains it
  - code that already has the safe form
  - a sibling call with the same shape but a safe algorithm

The positive cases are fewer and simpler, because a fix that should happen
either happened or did not.

THE FOUR GATES
--------------
`_verify` is tested directly, once per gate, because a gate that never rejects
anything is indistinguishable from a gate that does not exist. The gates are:
parses, finding gone, nothing worse appeared, and the line count is unchanged.
"""

from __future__ import annotations

import pytest

from app.services.code_analysis.autofix_js import (
    FIXABLE_JS_RULES,
    JS_EXTENSIONS,
    _verify,
    autofix_javascript,
)
from app.services.code_analysis.models import Finding, Severity
from app.services.tree_sitter_langs import get_language


@pytest.fixture(autouse=True)
def _require_javascript():
    if get_language("javascript") is None:
        pytest.skip("tree-sitter-javascript is not installed")


def _fix(path: str, source: str, language: str = "") -> str:
    """Run the fixer with findings taken from the real analyzer."""
    from app.services.code_analysis import analyze_file

    lang = language or path.rsplit(".", 1)[-1]
    analysis = analyze_file(source, path, lang)
    return autofix_javascript(source, analysis.findings, path, lang).content


def _result(path: str, source: str, language: str = ""):
    from app.services.code_analysis import analyze_file

    lang = language or path.rsplit(".", 1)[-1]
    analysis = analyze_file(source, path, lang)
    return autofix_javascript(source, analysis.findings, path, lang)


# ── The three transformations ────────────────────────────────────────────────


def test_reject_unauthorized_false_becomes_true():
    """The Node way of skipping certificate checks. One token, one right answer."""
    source = "const agent = new https.Agent({ rejectUnauthorized: false });\n"
    assert _fix("a.ts", source) == (
        "const agent = new https.Agent({ rejectUnauthorized: true });\n"
    )


def test_a_quoted_object_key_is_also_repaired():
    """`{ "rejectUnauthorized": false }` is the same setting, spelled differently."""
    source = "const a = { 'rejectUnauthorized': false };\n"
    assert "'rejectUnauthorized': true" in _fix("a.js", source)


def test_the_tls_environment_variable_is_repaired():
    """The documented Node escape hatch; the quoted form the regex could not see."""
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';\n"
    assert _fix("a.js", source) == "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '1';\n"


def test_the_unquoted_tls_environment_variable_is_repaired():
    """`= 0` as a number is equally wrong and equally mechanical to fix."""
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = 0;\n"
    assert _fix("a.js", source) == "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '1';\n"


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "MD5"])
def test_a_weak_hash_algorithm_becomes_sha256(algorithm):
    source = f"const h = crypto.createHash('{algorithm}').update(x).digest('hex');\n"
    fixed = _fix("h.js", source)
    assert "createHash('sha256')" in fixed
    assert f"'{algorithm}'" not in fixed


def test_quote_style_is_preserved():
    """
    A fix must not fight the repository's linter.

    Switching `'md5'` to `"sha256"` can fail `quotes`/`quote-props` under ESLint
    or Prettier, turning a security fix into a red build — and a developer who
    has to fix the fixer stops trusting it.
    """
    assert 'createHash("sha256")' in _fix("h.js", 'const h = createHash("md5");\n')
    assert "createHash('sha256')" in _fix("h.js", "const h = createHash('md5');\n")
    assert "`sha256`" in _fix("h.js", "const h = createHash(`md5`);\n")


def test_a_multiline_call_is_repaired():
    """The span comes from the tree, so a multi-line call is not a special case."""
    source = (
        "const h = crypto\n"
        "  .createHash('md5')\n"
        "  .update(x)\n"
        "  .digest('hex');\n"
    )
    fixed = _fix("h.js", source)
    assert "createHash('sha256')" in fixed
    assert fixed.count("\n") == source.count("\n"), "line count must not change"


def test_two_problems_in_one_file_are_both_repaired():
    """One unfixable finding must not block the others; nor must one fix hide another."""
    source = (
        "const agent = new Agent({ rejectUnauthorized: false });\n"
        "const h = crypto.createHash('md5');\n"
    )
    fixed = _fix("mixed.js", source)
    assert "rejectUnauthorized: true" in fixed
    assert "createHash('sha256')" in fixed


# ── Negative cases: already-correct code is left alone ───────────────────────


@pytest.mark.parametrize(
    "source, label",
    [
        ("const a = { rejectUnauthorized: true };\n", "TLS already enabled"),
        ("// don't set rejectUnauthorized: false in prod\nconst x = 1;\n", "comment"),
        ('const msg = "rejectUnauthorized: false";\n', "string literal"),
        ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = '1';\n", "env var already enabled"),
        ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = 'true';\n", "env var enabled"),
        ("const h = crypto.createHash('sha256');\n", "strong algorithm"),
        ("const h = crypto.createHash('md5x');\n", "different algorithm name"),
        ("const h = crypto.createHash(algorithm);\n", "computed algorithm"),
        ("const h = myCreateHash('md5');\n", "similarly named function"),
    ],
)
def test_these_are_left_byte_for_byte_unchanged(source, label):
    """
    The whole point. `autofix` must be safe to run over a whole repository, which
    means a file with nothing wrong in it comes back identical.
    """
    assert _fix("sample.js", source) == source, f"rewrote {label}"


def test_a_comment_is_not_rewritten_even_beside_real_code():
    """
    The sharpest version of the previous case: a stub fixer that patched the
    *first* occurrence would hit the comment and leave the vulnerability.
    """
    source = (
        "// rejectUnauthorized: false is never acceptable\n"
        "const agent = new Agent({ rejectUnauthorized: false });\n"
    )
    fixed = _fix("a.js", source)
    assert "// rejectUnauthorized: false is never acceptable" in fixed, "the comment was edited"
    assert fixed.count("rejectUnauthorized: true") == 1


def test_unrelated_lines_are_untouched():
    """Minimality: everything outside the offending token survives byte-for-byte."""
    source = (
        "import https from 'https';\n"
        "\n"
        "// a comment that must survive\n"
        "const TIMEOUT = 5000;\n"
        "\n"
        "export const agent = new https.Agent({ rejectUnauthorized: false });\n"
        "\n"
        "export function other() {\n"
        "  return 42;\n"
        "}\n"
    )
    fixed = _fix("a.ts", source)
    after = fixed.replace("rejectUnauthorized: true", "rejectUnauthorized: false")
    assert after == source, "the fix changed more than the offending token"


# ── The gates, tested one at a time ──────────────────────────────────────────


def _finding(line: int = 1, severity: Severity = Severity.HIGH) -> Finding:
    return Finding(
        rule_id="GEN-SEC-TLS-OFF",
        title="TLS verification disabled",
        severity=severity,
        line=line,
        message="m",
    )


def test_gate_rejects_an_unchanged_file():
    """
    Gate 2. If nothing changed, the finding is by definition still there, and a
    diff that does not fix anything is noise the reviewer has to read.
    """
    source = "const a = { rejectUnauthorized: false };\n"
    ok, reason = _verify(source, source, _finding(), "a.js", "js")
    assert ok is False
    assert "still present" in reason


def test_gate_rejects_a_result_that_does_not_parse():
    """
    Gate 1. A fix that breaks the file is catastrophic, so it is rejected.

    The fixture drops a closing brace, and that matters. The first version of this
    test wrote `tru` in place of `true` and expected a parse failure — but `tru` is
    a perfectly valid identifier in JavaScript, so tree-sitter reports no error and
    the gate correctly accepted. The test was asserting a false premise.
    """
    source = "const a = { rejectUnauthorized: false };\n"
    broken = "const a = { rejectUnauthorized: true ;\n"
    ok, reason = _verify(source, broken, _finding(), "a.js", "js")
    assert ok is False, "a missing brace should not pass the parse gate"
    assert "parse" in reason


def test_gate_rejects_a_fix_that_changes_the_line_count():
    """
    The line invariant, and the reason it matters.

    Findings elsewhere in the file carry line numbers. A fix that inserts or
    removes a line silently invalidates every one below it, so the rest of the
    review starts pointing at the wrong code — a subtle, hard-to-attribute bug.
    """
    source = "const a = { rejectUnauthorized: false };\n"
    added = "const a = { rejectUnauthorized: true };\n\n"
    ok, reason = _verify(source, added, _finding(), "a.js", "js")
    assert ok is False
    assert "number of lines" in reason


def test_gate_accepts_the_real_fix():
    """Positive control: without this, every rejection test could pass vacuously."""
    source = "const a = { rejectUnauthorized: false };\n"
    patched = "const a = { rejectUnauthorized: true };\n"
    ok, reason = _verify(source, patched, _finding(), "a.js", "js")
    assert ok is True, reason


def test_a_rejected_fix_leaves_the_file_alone_and_is_reported(monkeypatch):
    """
    NEGATIVE CASE for the plumbing: when verification fails, the file must come
    back untouched and the reason must be reported rather than swallowed. A
    silent rejection is indistinguishable from "nothing to fix".
    """
    from app.services.code_analysis import autofix_js

    monkeypatch.setattr(autofix_js, "_verify", lambda *a, **k: (False, "deliberate test failure"))
    source = "const a = { rejectUnauthorized: false };\n"
    result = autofix_javascript(source, [_finding()], "a.js", "js")

    assert result.content == source, "a rejected fix must not be applied"
    assert result.fixes == []
    assert result.changed is False
    assert result.rejected and "deliberate test failure" in result.rejected[0]


def test_the_applied_fix_records_before_and_after_for_the_ui():
    """The PR body and the review panel render these two lines; they must differ."""
    source = "const a = { rejectUnauthorized: false };\n"
    result = _result("a.js", source)
    assert len(result.fixes) == 1
    fix = result.fixes[0]
    assert fix.rule_id in FIXABLE_JS_RULES
    assert fix.before != fix.after
    assert "false" in fix.before and "true" in fix.after
    assert fix.description


# ── Scope and degradation ───────────────────────────────────────────────────


def test_non_js_files_are_not_touched():
    """NEGATIVE CASE: the Python path has its own fixer and must not be shadowed."""
    source = "h = hashlib.md5(x)\n"
    assert autofix_javascript(source, [_finding()], "m.py", "py").content == source


def test_a_language_with_no_grammar_degrades_to_no_fixes():
    """
    NEGATIVE CASE: Go has no installed wheel. The fixer must return the file
    unchanged rather than raising, so one absent dependency cannot fail a review.
    """
    if get_language("go") is not None:
        pytest.skip("tree-sitter-go is installed")
    source = "package main\n"
    result = autofix_javascript(source, [_finding()], "main.go", "go")
    assert result.content == source
    assert result.fixes == []


def test_a_construct_inside_a_parse_error_is_not_rewritten():
    """
    NEGATIVE CASE, and the same rule the chunker applies to citations.

    tree-sitter recovers from a syntax error, so the offending node still exists
    and a fixer that trusted spans alone would rewrite it. But the span of a node
    in a file we could not fully parse is not necessarily the span we think it is,
    and this module *edits* what it finds. Refusing is the honest outcome.
    """
    source = "const a = { rejectUnauthorized: false \n"      # unclosed brace
    assert autofix_javascript(source, [_finding()], "a.js", "js").content == source


def test_a_healthy_construct_before_the_error_is_still_fixed():
    """
    The refusal is scoped to the error region, not the whole file.

    This is the byte-range claim, asserted in the direction where it holds: the
    healthy construct sits *above* the syntax error, so its span is outside the
    error region and the fix applies normally.
    """
    source = (
        "const a = { rejectUnauthorized: false };\n"      # healthy, line 1
        "function broken( {\n"                            # error from here down
        "}\n"
    )
    fixed = autofix_javascript(source, [_finding(line=1)], "a.js", "js").content
    assert "rejectUnauthorized: true" in fixed


def test_recovery_propagates_forward_from_a_syntax_error():
    """
    The limit of the previous test, stated rather than hidden.

    tree-sitter's recovery is not local: an unterminated construct makes the
    parser consume everything after it as part of the ERROR node. Measured, not
    assumed:

        error AFTER the healthy line → error_rows [(1, 2)], line 1 fixable
        error BEFORE it              → error_rows [(0, 2)], line 3 NOT fixable

    So a file with an early syntax error gets no rewrites below it. That is the
    deliberate trade: this module edits code, and a span we cannot trust is a span
    we must not rewrite. The cost is a missed fix in a file that is already
    broken — and a file that is already broken does not compile, so the missing
    fix is not what is blocking anyone.

    If this behaviour changes, it should change on purpose.
    """
    source = (
        "function broken( {\n"                            # error from line 1
        "}\n"
        "const a = { rejectUnauthorized: false };\n"      # healthy, line 3
    )
    assert autofix_javascript(source, [_finding(line=3)], "a.js", "js").content == source


def test_empty_source_is_handled():
    assert autofix_javascript("", [_finding()], "a.js", "js").content == ""


def test_an_unfixable_rule_is_ignored():
    """NEGATIVE CASE: a rule outside the fixable set must produce no edit."""
    source = "eval(x);\n"
    finding = Finding(
        rule_id="JS-SEC-EVAL", title="eval", severity=Severity.HIGH,
        line=1, message="m",
    )
    assert autofix_javascript(source, [finding], "a.js", "js").content == source


def test_js_extensions_cover_the_family():
    """The dispatch set and the grammar registry must agree, or a file silently
    stops being fixable."""
    from app.services.tree_sitter_langs import grammar_for_path

    for ext in JS_EXTENSIONS:
        assert grammar_for_path(f"f.{ext}") is not None, (
            f"{ext} is listed as fixable but has no grammar"
        )


# ── Through the service, end to end ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fix_service_produces_a_patch_for_typescript():
    """
    End to end through `apply_fixes`, which is what the API calls.

    The unit tests above prove the transformation; this proves the wiring — the
    routing, the re-analysis, and the `git apply`-ready patch.
    """
    from app.services.fix_service import apply_fixes

    source = 'import https from "https";\nconst a = new https.Agent({ rejectUnauthorized: false });\n'
    outcome = await apply_fixes("src/tls.ts", content=source)

    assert outcome.findings_before == 1
    assert outcome.findings_after == 0
    assert outcome.score_after > outcome.score_before
    assert len(outcome.fixes) == 1
    assert outcome.patch is not None
    assert "rejectUnauthorized: true" in outcome.patch["diff"]
    assert "rejectUnauthorized: false" in outcome.patch["diff"]


@pytest.mark.asyncio
async def test_fix_service_reports_a_clean_file_as_clean():
    """No finding means no patch — an empty result, not an error."""
    from app.services.fix_service import apply_fixes

    source = "const a = new Agent({ rejectUnauthorized: true });\n"
    outcome = await apply_fixes("src/safe.ts", content=source)
    assert outcome.fixes == []
    assert outcome.patch is None
    assert outcome.content == source


@pytest.mark.asyncio
async def test_an_unsupported_language_still_raises():
    """NEGATIVE CASE: the guard must survive the change to language routing."""
    from app.services.fix_service import UnsupportedLanguageError, apply_fixes

    with pytest.raises(UnsupportedLanguageError):
        await apply_fixes("main.go", content="package main\n")


@pytest.mark.asyncio
async def test_python_fixes_still_work():
    """
    NEGATIVE CASE, most important of all: adding a JS branch must not disturb the
    Python path, which was working before this change.
    """
    from app.services.fix_service import apply_fixes

    source = "import requests\nr = requests.get(url, verify=False)\n"
    outcome = await apply_fixes("src/net.py", content=source)
    assert outcome.findings_after == 0
    assert outcome.fixes, "the Python fixer stopped working"


def test_a_candidate_that_changes_nothing_is_skipped_silently(monkeypatch):
    """
    A defensive branch, pinned by its observable effect.

    None of the three fixers can currently produce a replacement identical to the
    text it replaces, so this guard is unreachable today. Mutation testing found
    that: deleting the guard broke no test, because `_verify` would reject the
    candidate anyway and the file still comes back unchanged.

    The difference it makes is what the *user* sees. Without the guard the no-op
    lands in `rejected`, and the review panel reports a failed repair for
    something that was never a repair — a spurious warning that erodes trust in
    the warnings that matter. So the contract is: skip silently, report nothing.
    """
    from app.services.code_analysis import autofix_js

    source = "const a = { rejectUnauthorized: false };\n"
    noop = autofix_js._Site(
        rule_id="GEN-SEC-TLS-OFF",
        start_byte=0,
        end_byte=0,
        replacement="",          # inserts nothing, replaces nothing
        start_row=0,
        end_row=0,
        before="",
        description="no-op",
    )
    monkeypatch.setattr(autofix_js, "_find_sites", lambda root, source_bytes: [noop])

    result = autofix_javascript(source, [_finding()], "a.js", "js")
    assert result.content == source
    assert result.fixes == []
    assert result.rejected == [], "a no-op candidate must not be reported as a rejection"
