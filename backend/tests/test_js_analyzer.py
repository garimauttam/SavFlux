"""
test_js_analyzer.py — Structural JS/TS detection, and the dead-pattern bug it fixes.

WHAT THESE TESTS PROTECT
------------------------
Two shipped security patterns could never match, because the regex pass runs
against a copy of each line with string *contents* blanked:

    createHash('md5')                     → createHash('   ')
    NODE_TLS_REJECT_UNAUTHORIZED = '0'    → ... = ' '

Both are real vulnerabilities in Node code, and both were silently invisible.
The tests below are therefore mostly about *reach* (the patterns now fire) and
*precision* (they fire only on real code, never on a comment or a string that
merely mentions the pattern).

The precision half is the harder half, and it is the reason this was not fixed by
relaxing the regexes. A pattern that matches raw lines would fire on:

    // don't use createHash('md5') here
    expect(src).not.toContain("rejectUnauthorized: false")

Both appear in real repositories — the second is in this repository's own test
suite. A parse tree tells an argument from a mention; a regex cannot.
"""

from __future__ import annotations

import pytest

from app.services.code_analysis import analyze_file
from app.services.code_analysis.generic_analyzer import GENERIC_RULES, _strip_comments_and_strings
from app.services.code_analysis.js_analyzer import detect_structural_issues
from app.services.tree_sitter_langs import get_language


@pytest.fixture(autouse=True)
def _require_javascript():
    if get_language("javascript") is None:
        pytest.skip("tree-sitter-javascript is not installed")


def _rules(src: str, name: str, lang: str) -> list[str]:
    return sorted({f.rule_id for f in analyze_file(src, name, lang).findings})


# ── The bug: prove the regex pass cannot see these constructs ────────────────


def test_the_regex_pass_blanks_string_contents():
    """
    The mechanism behind the bug, asserted directly.

    If this ever stops being true, the structural pass below may be redundant —
    so this test failing is a signal to re-examine, not necessarily a fault.
    """
    for source, blanked in (
        ("const h = crypto.createHash('md5');", "createHash('   ')"),
        ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';", "= ' '"),
    ):
        lines = _strip_comments_and_strings(source, "js")
        assert blanked in lines[0].code, (
            f"expected the string contents to be blanked in {lines[0].code!r}"
        )


def test_the_weak_hash_pattern_cannot_match_the_stripped_line():
    """
    WHY THE DETECTOR EXISTS, as a failing-by-design assertion.

    The rule's own regex matches the raw text perfectly. It never sees raw text.
    So without the structural pass, `createHash('md5')` in a Node service is not
    reported at all — the pattern is unreachable code.
    """
    source = "const h = crypto.createHash('md5');"
    pattern = next(r[3] for r in GENERIC_RULES if r[0] == "GEN-SEC-WEAKHASH")

    # Matches the truth...
    assert pattern.search(source), "the pattern should match raw source"
    # ...but the analyzer only ever hands it the blanked line.
    stripped = _strip_comments_and_strings(source, "js")[0].code
    assert not pattern.search(stripped), (
        "the stripped line now matches, so this bug is fixed another way and the "
        "structural pass should be re-evaluated"
    )


def test_the_tls_env_pattern_cannot_match_the_quoted_stripped_line():
    """Same unreachability, for the documented quoted form of the Node escape hatch."""
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';"
    pattern = next(r[3] for r in GENERIC_RULES if r[0] == "GEN-SEC-TLS-OFF")

    assert pattern.search(source)
    stripped = _strip_comments_and_strings(source, "js")[0].code
    assert not pattern.search(stripped)


# ── Reach: the vulnerabilities are now found ─────────────────────────────────


@pytest.mark.parametrize("algorithm", ["md5", "sha1", "MD5", "Sha1"])
def test_create_hash_with_a_broken_algorithm_is_reported(algorithm):
    """Case-insensitive: Node accepts `MD5` and it is the same broken function."""
    source = f"const h = crypto.createHash('{algorithm}');\n"
    assert "GEN-SEC-WEAKHASH" in _rules(source, "hash.js", "js")


def test_create_hash_through_a_bare_import_is_reported():
    """`const { createHash } = require('crypto')` is the other common spelling."""
    source = "const h = createHash('md5');\n"
    assert "GEN-SEC-WEAKHASH" in _rules(source, "hash.js", "js")


def test_create_hash_with_a_template_literal_is_reported():
    """A template literal with no substitution is still a fixed algorithm."""
    source = "const h = crypto.createHash(`md5`);\n"
    assert "GEN-SEC-WEAKHASH" in _rules(source, "hash.js", "js")


def test_quoted_tls_env_override_is_reported():
    """The form Node's own documentation shows, previously invisible."""
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';\n"
    assert "GEN-SEC-TLS-OFF" in _rules(source, "env.js", "js")


def test_unquoted_tls_env_override_is_still_reported():
    """The regex already caught this one; the fix must not lose it."""
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = 0;\n"
    findings = analyze_file(source, "env.js", "js").findings
    assert [f.rule_id for f in findings] == ["GEN-SEC-TLS-OFF"]


def test_an_unquoted_override_is_reported_exactly_once():
    """
    NEGATIVE CASE for double-reporting.

    The regex catches the unquoted form and the structural pass also recognises
    it, so without a merge keyed on (rule_id, line) one problem would appear
    twice — inflating the finding count and the risk score for a single defect.
    """
    source = "process.env.NODE_TLS_REJECT_UNAUTHORIZED = 0;\n"
    findings = analyze_file(source, "env.js", "js").findings
    assert len(findings) == 1, f"expected one finding, got {[f.rule_id for f in findings]}"


def test_two_distinct_weak_hashes_are_both_reported():
    """Merging must dedupe by line, not collapse a rule to one finding per file."""
    source = "const a = createHash('md5');\nconst b = createHash('sha1');\n"
    findings = [f for f in analyze_file(source, "h.js", "js").findings
                if f.rule_id == "GEN-SEC-WEAKHASH"]
    assert sorted(f.line for f in findings) == [1, 2]


def test_findings_carry_the_line_and_the_evidence():
    """The UI and the fixer both need a real line number, not a guess."""
    source = "const x = 1;\nconst h = crypto.createHash('md5');\n"
    finding = next(f for f in analyze_file(source, "h.js", "js").findings
                   if f.rule_id == "GEN-SEC-WEAKHASH")
    assert finding.line == 2
    assert "createHash" in finding.evidence
    assert finding.cwe == "CWE-327"
    assert finding.remediation


def test_structural_findings_are_marked_as_proven_not_guessed():
    """
    The Finding model distinguishes "the parser proved this" from "this looks
    suspicious" (see its docstring). A parse tree proving a call is stronger
    evidence than a line matching a pattern, and the number must say so.
    """
    source = "const h = crypto.createHash('md5');\n"
    finding = next(f for f in analyze_file(source, "h.js", "js").findings
                   if f.rule_id == "GEN-SEC-WEAKHASH")
    assert finding.confidence >= 0.9, "a proven finding should outrank a matched one"


# ── Precision: never fire on a mention ───────────────────────────────────────


@pytest.mark.parametrize(
    "source, label",
    [
        ("// never call createHash('md5') anywhere\nconst x = 1;\n", "line comment"),
        ("/* createHash('md5') is forbidden */\nconst x = 1;\n", "block comment"),
        ("const msg = \"do not use createHash('md5')\";\n", "string literal"),
        ("const msg = 'NODE_TLS_REJECT_UNAUTHORIZED = \"0\"';\n", "string literal"),
        ("expect(src).not.toContain('rejectUnauthorized: false');\n", "test assertion"),
        ("const h = crypto.createHash('sha256');\n", "safe algorithm"),
        ("const h = crypto.createHash(algorithm);\n", "variable algorithm"),
        ("const h = crypto.createHash(`${algo}`);\n", "template with substitution"),
        ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = '1';\n", "verification enabled"),
        ("process.env.NODE_TLS_REJECT_UNAUTHORIZED = 'true';\n", "verification enabled"),
    ],
)
def test_these_must_stay_quiet(source, label):
    """
    NEGATIVE CASES. Each one would start reporting if the fix had been to match
    the raw line instead of parsing.

    The fifth entry is taken from this repository's own test suite, which is the
    clearest argument against the looser approach: a fix that matched raw text
    would make SavFlux report a finding against itself.
    """
    assert detect_structural_issues(source, "sample.js", "js") == [], (
        f"fired on {label}: {source!r}"
    )


def test_a_similarly_named_function_is_not_flagged():
    """`myCreateHash` is a different function. Only a whole identifier or a
    member access counts, so a substring match cannot produce a false positive."""
    assert detect_structural_issues("const h = myCreateHash('md5');", "x.js", "js") == []


def test_a_non_createhash_call_with_a_weak_name_argument_is_not_flagged():
    """The weak-hash claim is about `createHash`, not about the string 'md5'."""
    assert detect_structural_issues("const h = lookup('md5');", "x.js", "js") == []


def test_a_weak_algorithm_mentioned_after_the_call_is_not_flagged():
    """`createHash('sha256')` followed by `update('md5')` is not a weak hash."""
    source = "const h = crypto.createHash('sha256'); h.update('md5');\n"
    assert detect_structural_issues(source, "x.js", "js") == []


def test_only_the_first_argument_selects_the_algorithm():
    """
    `createHash(algorithm, options)` — position one is the algorithm, and only
    position one.

    The second argument here is contrived as a string, because nothing realistic
    puts a weak-hash name in second position. That is the point: this test pins
    the *field*, not a behaviour, because the failure it guards against is silent.
    A rule that scanned every argument would fire on
    `createHash('sha256', { seed: 'md5' })`-shaped options objects and, worse,
    would report a SHA-256 call as a weak hash — a false positive on correct code,
    which is how a security rule loses its audience.
    """
    source = "const h = crypto.createHash('sha256', 'md5');\n"
    assert detect_structural_issues(source, "x.js", "js") == [], (
        "the algorithm was read from the wrong argument position"
    )
    # And the real Node two-argument shape stays quiet for the same reason.
    options_form = "const h = crypto.createHash('sha256', { outputLength: 512 });\n"
    assert detect_structural_issues(options_form, "x.js", "js") == []


# ── Scope: JS only, and always survivable ────────────────────────────────────


def test_python_files_get_no_structural_findings():
    """Python has its own analyzer; this pass must not add JS rules to it."""
    source = "import hashlib\nh = hashlib.md5(x)\n"
    assert detect_structural_issues(source, "p.py", "py") == []
    # And the Python analyzer still reports it its own way.
    assert "PY-SEC-WEAKHASH" in _rules(source, "p.py", "py")


def test_markdown_and_data_files_get_no_structural_findings():
    """NEGATIVE CASE: a doc that quotes the vulnerable pattern is a doc."""
    source = "Never write `createHash('md5')` in production.\n"
    assert detect_structural_issues(source, "SECURITY.md", "md") == []


def test_a_language_with_no_grammar_returns_nothing():
    """NEGATIVE CASE: Go has no installed wheel, so this pass is a no-op there."""
    if get_language("go") is not None:
        pytest.skip("tree-sitter-go is installed")
    assert detect_structural_issues("x := 1\n", "main.go", "go") == []


def test_a_file_that_does_not_parse_does_not_raise():
    """A syntax error must not break a review; the regex pass still ran."""
    for source in ("", "const h = crypto.createHash(", "\x00\xff\xfe", "}{)("):
        result = detect_structural_issues(source, "broken.js", "js")
        assert isinstance(result, list)


def test_detection_is_scoped_to_the_js_family():
    """A `.tsx` component is analysed too — it is the same grammar family."""
    source = 'const H = () => <div>{crypto.createHash("md5").digest("hex")}</div>;\n'
    assert "GEN-SEC-WEAKHASH" in _rules(source, "C.tsx", "tsx")


# ── The analyzer contract still holds ───────────────────────────────────────


def test_the_merge_does_not_disturb_existing_rules():
    """
    The structural pass is additive. A file with a regex-detected issue must
    still report it, with the regex's own confidence, not the structural one.
    """
    source = "const a = new Agent({ rejectUnauthorized: false });\n"
    finding = next(f for f in analyze_file(source, "a.ts", "ts").findings
                   if f.rule_id == "GEN-SEC-TLS-OFF")
    assert finding.confidence == 0.9, "the regex finding was overwritten by the merge"


def test_secret_detection_still_works_alongside_it():
    """
    A guard against the structural pass short-circuiting the secret rules, which
    run in a loop after it and deliberately use the *raw* line.

    The fixture is built at runtime rather than written as a literal: a committed
    line matching `KEY = "value"` makes every scanner pointed at this repository
    alert, and this repo has already been flagged three times for exactly that.
    See tests/secret_fixtures.py.
    """
    from tests.secret_fixtures import assignment, fake_api_key

    source = assignment("api_key", fake_api_key())
    rules = _rules(source, "cfg.js", "js")
    assert "GEN-SEC-HARDCODED" in rules, f"secret detection regressed: {rules}"


def test_secret_detection_is_unaffected_by_a_structural_finding():
    """Both passes must report on the same file, not one instead of the other."""
    from tests.secret_fixtures import assignment, fake_api_key

    source = assignment("api_key", fake_api_key()) + "const h = createHash('md5');\n"
    rules = _rules(source, "cfg.js", "js")
    assert {"GEN-SEC-HARDCODED", "GEN-SEC-WEAKHASH"} <= set(rules), rules


# ── The quoted-key bypass ────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["rejectUnauthorized", "'rejectUnauthorized'", '"rejectUnauthorized"'])
def test_every_spelling_of_the_tls_key_is_reported(key):
    """
    A quoted key puts a quote between the name and the colon, so the regex
    `rejectUnauthorized\\s*:\\s*false` cannot match it:

        { 'rejectUnauthorized': false }

    That disables certificate verification exactly as effectively as the bare
    form, and it was invisible. Quoted keys are ordinary style in config-shaped
    objects, so this is a bypass rather than a curiosity.
    """
    source = f"const a = new Agent({{ {key}: false }});\n"
    assert "GEN-SEC-TLS-OFF" in _rules(source, "a.js", "js"), f"missed {key}"


def test_a_quoted_safe_key_is_not_reported():
    """NEGATIVE CASE: the quoted spelling is not suspicious on its own."""
    source = 'const a = new Agent({ "rejectUnauthorized": true });\n'
    assert _rules(source, "a.js", "js") == []


def test_the_bare_key_is_still_reported_only_once():
    """NEGATIVE CASE for double-reporting: the regex and the tree both see it."""
    source = "const a = new Agent({ rejectUnauthorized: false });\n"
    findings = analyze_file(source, "a.js", "js").findings
    assert len(findings) == 1, [f.rule_id for f in findings]


# ── Parse errors suppress unreliable evidence ────────────────────────────────


def test_a_construct_inside_an_unparseable_region_is_not_reported():
    """
    A finding whose evidence line we cannot fully parse is not reported by the
    structural pass. The regex pass still runs and may report it; what stops is
    the *proven* claim, which is the one that must be right.
    """
    source = "function broken( {\n}\nconst a = { 'rejectUnauthorized': false };\n"
    assert detect_structural_issues(source, "a.js", "js") == []


def test_error_rows_are_reported_for_a_broken_file():
    """`error_rows` is the shared helper both the detector and the fixer use."""
    from app.services.code_analysis.js_analyzer import error_rows
    from app.services.tree_sitter_langs import get_parser

    parser = get_parser("javascript")
    assert parser is not None
    # tree-sitter parses bytes, not str.
    assert error_rows(parser.parse(b"const a = {").root_node), "expected an error region"
    assert error_rows(parser.parse(b"const a = 1;").root_node) == []
