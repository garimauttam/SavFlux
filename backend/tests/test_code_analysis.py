"""
Tests for the AST-based code analyzer.

The bar these tests defend is *precision*, not just recall. The regex pass this
replaces found 2 of 3 real injections while also reporting a correctly
parameterised query as vulnerable. A reviewer who is told safe code is
dangerous stops reading the tool's output, so every rule here has a paired
negative case proving it stays quiet on the safe form of the same construct.
"""

from __future__ import annotations

import pytest

from app.services.code_analysis import analyze_file
from app.services.code_analysis.analyzer import build_llm_facts, render_findings_markdown
from app.services.code_analysis.generic_analyzer import (
    _strip_comments_and_strings,
    analyze_generic,
)
from app.services.code_analysis.models import Finding, Severity
from app.services.code_analysis.python_analyzer import analyze_python
from tests.secret_fixtures import (
    assignment,
    fake_api_key,
    fake_password,
    js_comment_with_key,
    many_assignments,
)


def rule_ids(analysis) -> set[str]:
    return {f.rule_id for f in analysis.findings}


def lines_for(analysis, rule_id: str) -> set[int]:
    return {f.line for f in analysis.findings if f.rule_id == rule_id}


# ── SQL injection: the dataflow cases ─────────────────────────────────────────


def test_parameterised_query_is_not_flagged():
    """
    The regression that motivated this module.

    `execute(sql, params)` with a `?` placeholder is the textbook safe form.
    The previous regex pass reported it as "Dynamic SQL — use parameterised
    queries", i.e. it told the developer to do the thing they had already done.
    """
    source = """
def query_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
"""
    analysis = analyze_python(source, "safe.py")
    assert "PY-SEC-SQLI" not in rule_ids(analysis)


@pytest.mark.parametrize(
    "placeholder,params",
    [
        ('"SELECT * FROM t WHERE a = ?"', "(a,)"),
        ('"SELECT * FROM t WHERE a = %s"', "(a,)"),
        ('"SELECT * FROM t WHERE a = :a"', "{'a': a}"),
    ],
)
def test_placeholder_styles_are_all_recognised_as_safe(placeholder, params):
    """sqlite3, psycopg2 and SQLAlchemy each use a different placeholder."""
    source = f"""
def q(conn, a):
    return conn.execute({placeholder}, {params})
"""
    analysis = analyze_python(source, "safe.py")
    assert "PY-SEC-SQLI" not in rule_ids(analysis)


def test_fstring_interpolated_into_execute_is_flagged():
    """
    The case the regex pass missed entirely.

    No single line matches "execute(" plus a concatenation, because the table
    and predicate are interpolated by an f-string. A parser sees the JoinedStr.
    """
    source = """
def build_query(conn, table, where):
    conn.execute(f"SELECT * FROM {table} WHERE {where}")
"""
    analysis = analyze_python(source, "vuln.py")
    assert "PY-SEC-SQLI" in rule_ids(analysis)
    finding = next(f for f in analysis.findings if f.rule_id == "PY-SEC-SQLI")
    assert finding.severity is Severity.CRITICAL
    assert finding.cwe == "CWE-89"


def test_sql_built_on_an_earlier_line_is_traced_to_the_sink():
    """
    Cross-line dataflow: the query is assembled on one line and executed on
    another. Line-by-line matching cannot connect them; the taint tracker can,
    and the message must name the line where the string was built so the
    reviewer can see the whole flow.
    """
    source = """
def delete_user(conn, user_id):
    query = "DELETE FROM users WHERE id = " + str(user_id)
    conn.execute(query)
"""
    analysis = analyze_python(source, "vuln.py")
    finding = next(f for f in analysis.findings if f.rule_id == "PY-SEC-SQLI")
    assert finding.line == 4, "the finding should point at the execute() sink"
    assert "line 3" in finding.message, "the message should name where the string was built"


def test_augmented_assignment_marks_a_query_dynamic():
    """`sql += user_filter` is the incremental form of the same bug."""
    source = """
def search(conn, term):
    sql = "SELECT * FROM items"
    sql += " WHERE name LIKE '%" + term + "%'"
    conn.execute(sql)
"""
    analysis = analyze_python(source, "vuln.py")
    assert "PY-SEC-SQLI" in rule_ids(analysis)


def test_reassigning_a_name_to_a_literal_clears_the_taint():
    """
    Over-reporting is the failure mode that erodes trust, so a name that is
    rebound to a constant must stop being treated as dynamic.
    """
    source = """
def q(conn, user_id):
    query = "SELECT * FROM t WHERE id = " + str(user_id)
    query = "SELECT * FROM t"
    conn.execute(query)
"""
    analysis = analyze_python(source, "safe.py")
    assert "PY-SEC-SQLI" not in rule_ids(analysis)


def test_non_sql_dynamic_string_in_execute_is_not_sql_injection():
    """`execute` also exists on thread pools and task runners."""
    source = """
def run(pool, name):
    pool.execute(f"task-{name}")
"""
    analysis = analyze_python(source, "safe.py")
    assert "PY-SEC-SQLI" not in rule_ids(analysis)


# ── Shell injection ───────────────────────────────────────────────────────────


def test_subprocess_with_arg_list_is_not_flagged():
    source = """
import subprocess
def run(args):
    return subprocess.run(args, shell=False, capture_output=True)
"""
    analysis = analyze_python(source, "safe.py")
    assert not {"PY-SEC-SHELL", "PY-SEC-SHELL-STATIC"} & rule_ids(analysis)


def test_dynamic_command_with_shell_true_is_critical():
    source = """
import subprocess
def backup(path):
    cmd = "tar -czf out.tgz " + path
    subprocess.Popen(cmd, shell=True)
"""
    analysis = analyze_python(source, "vuln.py")
    finding = next(f for f in analysis.findings if f.rule_id == "PY-SEC-SHELL")
    assert finding.severity is Severity.CRITICAL
    assert finding.cwe == "CWE-78"


def test_static_command_with_shell_true_is_low_not_critical():
    """
    `subprocess.run("ls -la", shell=True)` is a style problem, not an
    injection. Grading it CRITICAL alongside a real RCE is how a report becomes
    noise that gets ignored.
    """
    source = """
import subprocess
def listing():
    subprocess.run("ls -la", shell=True)
"""
    analysis = analyze_python(source, "meh.py")
    assert "PY-SEC-SHELL" not in rule_ids(analysis)
    finding = next(f for f in analysis.findings if f.rule_id == "PY-SEC-SHELL-STATIC")
    assert finding.severity is Severity.LOW


def test_os_system_with_interpolation_is_flagged():
    source = """
import os
def ping(host):
    os.system(f"ping -c 1 {host}")
"""
    analysis = analyze_python(source, "vuln.py")
    assert "PY-SEC-SHELL" in rule_ids(analysis)


# ── Hardcoded credentials ─────────────────────────────────────────────────────


def test_env_lookup_is_not_a_hardcoded_secret():
    source = """
import os
API_KEY = os.getenv("API_KEY")
PASSWORD = os.environ["DB_PASSWORD"]
"""
    analysis = analyze_python(source, "conf.py")
    assert "PY-SEC-HARDCODED" not in rule_ids(analysis)


@pytest.mark.parametrize("value", ["placeholder", "your-key-here", "changeme", "sk-test-xxx", ""])
def test_placeholder_secrets_are_ignored(value):
    analysis = analyze_python(f'API_KEY = "{value}"\n', "conf.py")
    assert "PY-SEC-HARDCODED" not in rule_ids(analysis)


def test_real_looking_literal_secret_is_critical():
    source = assignment("DATABASE_PASSWORD", fake_password())
    analysis = analyze_python(source, "conf.py")
    finding = next(f for f in analysis.findings if f.rule_id == "PY-SEC-HARDCODED")
    assert finding.severity is Severity.CRITICAL
    assert finding.cwe == "CWE-798"
    assert "rotate" in finding.remediation.lower()


# ── Exception handling ────────────────────────────────────────────────────────


def test_silently_swallowed_exception_is_high_severity():
    """
    This repo shipped a bug of exactly this shape: `asyncio.run()` inside a
    sync request handler always raised, and a bare `except: pass` hid it, so
    the activity feed returned empty forever with no error anywhere.
    """
    source = """
def load():
    try:
        return compute()
    except:
        pass
"""
    analysis = analyze_python(source, "svc.py")
    finding = next(f for f in analysis.findings if f.rule_id == "PY-EXC-SILENT")
    assert finding.severity is Severity.HIGH


def test_except_that_logs_is_not_reported_as_silent():
    source = """
import logging
def load():
    try:
        return compute()
    except Exception:
        logging.exception("compute failed")
        return None
"""
    analysis = analyze_python(source, "svc.py")
    assert "PY-EXC-SILENT" not in rule_ids(analysis)


def test_bare_except_that_reraises_is_not_flagged_as_bare():
    source = """
def load():
    try:
        return compute()
    except:
        raise
"""
    analysis = analyze_python(source, "svc.py")
    assert "PY-EXC-BARE" not in rule_ids(analysis)


# ── Other Python rules ────────────────────────────────────────────────────────


def test_yaml_safe_load_is_not_flagged_but_yaml_load_is():
    safe = "import yaml\ndata = yaml.safe_load(text)\n"
    unsafe = "import yaml\ndata = yaml.load(text)\n"
    assert "PY-SEC-DESERIALIZE" not in rule_ids(analyze_python(safe, "a.py"))
    assert "PY-SEC-DESERIALIZE" in rule_ids(analyze_python(unsafe, "b.py"))


def test_yaml_load_with_safeloader_is_accepted():
    source = "import yaml\ndata = yaml.load(text, Loader=yaml.SafeLoader)\n"
    assert "PY-SEC-DESERIALIZE" not in rule_ids(analyze_python(source, "a.py"))


def test_verify_false_is_flagged():
    source = "import requests\nrequests.get(url, verify=False)\n"
    finding = next(f for f in analyze_python(source, "a.py").findings if f.rule_id == "PY-SEC-NOVERIFY")
    assert finding.severity is Severity.HIGH


def test_md5_with_usedforsecurity_false_is_accepted():
    """An explicit opt-out is the developer stating intent; respect it."""
    flagged = "import hashlib\nh = hashlib.md5(data)\n"
    opted_out = "import hashlib\nh = hashlib.md5(data, usedforsecurity=False)\n"
    assert "PY-SEC-WEAKHASH" in rule_ids(analyze_python(flagged, "a.py"))
    assert "PY-SEC-WEAKHASH" not in rule_ids(analyze_python(opted_out, "b.py"))


def test_secret_compared_with_equals_is_flagged_as_timing_leak():
    source = """
def check(token, expected):
    return token == expected
"""
    assert "PY-SEC-TIMING" in rule_ids(analyze_python(source, "auth.py"))


def test_eval_on_literal_is_lower_severity_than_eval_on_variable():
    literal = 'x = eval("1 + 1")\n'
    dynamic = "def f(user_input):\n    return eval(user_input)\n"
    lit_finding = next(f for f in analyze_python(literal, "a.py").findings if f.rule_id == "PY-SEC-EXEC")
    dyn_finding = next(f for f in analyze_python(dynamic, "b.py").findings if f.rule_id == "PY-SEC-EXEC")
    assert dyn_finding.severity.rank > lit_finding.severity.rank


def test_method_named_eval_is_not_flagged():
    """`self.eval(...)` on a model object is not the builtin."""
    source = """
def score(model, x):
    return model.eval(x)
"""
    assert "PY-SEC-EXEC" not in rule_ids(analyze_python(source, "a.py"))


# ── Complexity metrics ────────────────────────────────────────────────────────


def test_cyclomatic_complexity_counts_each_boolean_operand():
    """`a and b and c` is two decision points. Radon agrees; so must we."""
    source = """
def f(a, b, c):
    if a and b and c:
        return 1
    return 0
"""
    analysis = analyze_python(source, "a.py")
    fn = analysis.functions[0]
    assert fn.complexity == 4  # base 1 + if 1 + two boolean operands


def test_complexity_threshold_produces_a_finding():
    branches = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    source = f"def dispatch(x):\n{branches}\n    return None\n"
    analysis = analyze_python(source, "a.py")
    assert "PY-QUAL-COMPLEXITY" in rule_ids(analysis)
    assert analysis.max_complexity > 10


def test_function_span_includes_decorators():
    """
    Mirrors the chunker bug fixed alongside this: `node.lineno` points at the
    `def`, so anything derived from it silently drops the decorator lines.
    """
    source = """
@app.route("/users")
@requires_auth
def list_users():
    return []
"""
    analysis = analyze_python(source, "api.py")
    fn = analysis.functions[0]
    assert fn.line == 2, "span must start at the first decorator, not the def"


def test_simple_function_produces_no_findings():
    """The most important negative test: ordinary code must come back clean."""
    source = '''
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
'''
    analysis = analyze_python(source, "math.py")
    assert analysis.findings == []
    assert analysis.risk_score() == 10


# ── Risk scoring ──────────────────────────────────────────────────────────────


def test_one_critical_outranks_many_style_nits():
    """
    Severity-weighted scoring. The old pass subtracted a flat amount per
    finding, so five long lines scored the same as a remote code execution.
    """
    critical = analyze_file(assignment("PASSWORD", fake_password()), "a.py", "py")
    nits = analyze_file("\n".join("assert x" for _ in range(6)), "b.py", "py")
    assert critical.risk_score() < nits.risk_score()


def test_clean_file_scores_ten():
    analysis = analyze_file("def f():\n    return 1\n", "a.py", "py")
    assert analysis.risk_score() == 10


def test_score_is_clamped_to_one():
    source = many_assignments("PASSWORD", 20)
    assert analyze_file(source, "a.py", "py").risk_score() == 1


# ── Parse failures ────────────────────────────────────────────────────────────


def test_syntax_error_degrades_instead_of_raising():
    """
    A repository always contains at least one file that does not parse. It must
    still be scanned for secrets, because a half-written config is exactly
    where a key gets left behind.
    """
    source = assignment("API_KEY", fake_api_key()) + "def broken(:\n"
    analysis = analyze_file(source, "broken.py", "py")
    assert analysis.parse_error
    assert any("SEC-HARDCODED" in f.rule_id for f in analysis.findings)


def test_null_bytes_do_not_crash_the_analyzer():
    analysis = analyze_file("def f():\x00\n    pass\n", "weird.py", "py")
    assert analysis.parse_error


def test_empty_file_is_handled():
    analysis = analyze_file("", "empty.py", "py")
    assert analysis.findings == []
    assert analysis.total_lines == 0


# ── Generic (non-Python) analyzer ─────────────────────────────────────────────


def test_password_in_a_comment_is_not_a_finding():
    """
    The single most common false positive in line-based scanners: a developer
    writes `// TODO: move password to env` and the tool reports a leaked
    credential. Stripping comments before matching removes the whole class.
    """
    source = (
        "\n// TODO: move the password out of here\n"
        + js_comment_with_key()
        + "\nconst config = { timeout: 30 };\n"
    )
    analysis = analyze_generic(source, "app.js", "js")
    assert not any("HARDCODED" in f.rule_id or "KEYSHAPE" in f.rule_id for f in analysis.findings)


def test_eval_inside_a_string_literal_is_not_a_finding():
    source = 'const help = "call eval() to run code";\n'
    analysis = analyze_generic(source, "a.js", "js")
    assert "JS-SEC-EVAL" not in rule_ids(analysis)


def test_real_eval_in_javascript_is_flagged():
    source = "function run(code) {\n  return eval(code);\n}\n"
    assert "JS-SEC-EVAL" in rule_ids(analyze_generic(source, "a.js", "js"))


def test_empty_catch_block_is_flagged():
    source = """
function load() {
  try {
    return compute();
  } catch (e) {}
}
"""
    finding = next(f for f in analyze_generic(source, "a.js", "js").findings
                   if f.rule_id == "GEN-QUAL-EMPTY-CATCH")
    assert finding.severity is Severity.HIGH


@pytest.mark.parametrize(
    "literal,label",
    [
        ("AKIAIOSFODNN7EXAMPLE", "AWS"),
        ("ghp_" + "a" * 36, "GitHub"),
        ("xoxb-123456789012-abcdefghijklm", "Slack"),
    ],
)
def test_known_key_shapes_are_detected_regardless_of_variable_name(literal, label):
    """
    A key assigned to `const x = ...` has no telltale variable name, so
    name-based detection misses it. The literal's own shape is the signal.
    """
    analysis = analyze_generic(f'const x = "{literal}";\n', "a.js", "js")
    assert "GEN-SEC-KEYSHAPE" in rule_ids(analysis), f"{label} key not detected"


def test_hash_is_not_a_comment_in_javascript():
    """
    `#` starts a comment in Python but denotes a private field in JS. Treating
    it as a comment would blank out real code and hide findings inside it.
    """
    source = "class A {\n  #secret = 1;\n  run(code) { return eval(code); }\n}\n"
    analysis = analyze_generic(source, "a.js", "js")
    assert "JS-SEC-EVAL" in rule_ids(analysis)


def test_generic_findings_carry_lower_confidence_than_ast_findings():
    """Heuristics must not claim the certainty of a parser."""
    js = analyze_generic("function f(c) { return eval(c); }\n", "a.js", "js")
    py = analyze_python("def f(c):\n    return eval(c)\n", "a.py")
    js_conf = next(f.confidence for f in js.findings if f.rule_id == "JS-SEC-EVAL")
    py_conf = next(f.confidence for f in py.findings if f.rule_id == "PY-SEC-EXEC")
    assert js_conf < py_conf


def test_brace_language_function_extents_are_measured():
    """
    Nesting depth used to be computed from Python indentation rules, which
    returned 0 for every brace-delimited language — a metric that was silently
    always wrong rather than absent.
    """
    source = """
function outer(a, b) {
  if (a) {
    for (let i = 0; i < b; i++) {
      if (i > 2) {
        console.log(i);
      }
    }
  }
}
"""
    analysis = analyze_generic(source, "a.js", "js")
    fn = next(f for f in analysis.functions if f.name == "outer")
    assert fn.max_depth >= 3
    assert fn.end_line > fn.line


def test_string_stripper_preserves_line_numbering():
    """Line numbers in findings are only meaningful if they survive stripping."""
    source = 'const a = 1;\n// comment\nconst b = "text";\n\nconst c = 3;\n'
    lines = _strip_comments_and_strings(source, "js")
    assert len(lines) == 5
    assert [l.number for l in lines] == [1, 2, 3, 4, 5]
    assert lines[1].is_comment_only
    assert lines[3].is_blank


# ── Dispatch ──────────────────────────────────────────────────────────────────


def test_language_is_inferred_from_the_filename_when_absent():
    """The paste-code review path supplies no language metadata."""
    analysis = analyze_file("def f():\n    return eval(x)\n", "snippet.py", "")
    assert analysis.language == "python"
    assert "PY-SEC-EXEC" in rule_ids(analysis)


def test_unknown_language_still_scans_for_secrets():
    analysis = analyze_file(assignment("password", fake_password()), "config.conf", "conf")
    assert "GEN-SEC-HARDCODED" in rule_ids(analysis)


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_markdown_marks_low_confidence_findings_as_likely():
    analysis = analyze_file("def f():\n    return 1\n", "a.py", "py")
    analysis.findings = [
        Finding("X", "Certain thing", Severity.HIGH, 1, "m", confidence=0.95),
        Finding("Y", "Guessed thing", Severity.HIGH, 2, "m", confidence=0.5),
    ]
    rendered = render_findings_markdown(analysis)
    before_guessed, after_guessed = rendered.split("Guessed thing", 1)
    assert "_(likely)_" not in before_guessed, "a 0.95-confidence finding must not be hedged"
    assert "_(likely)_" in after_guessed.split("\n")[0], "a 0.5-confidence finding must be hedged"


def test_markdown_caps_the_list_and_says_how_many_were_hidden():
    analysis = analyze_file("def f():\n    return 1\n", "a.py", "py")
    analysis.findings = [
        Finding(f"R{i}", f"Issue {i}", Severity.LOW, i, "m") for i in range(1, 21)
    ]
    rendered = render_findings_markdown(analysis, max_findings=5)
    assert "…and 15 lower-severity finding(s)." in rendered


def test_clean_file_renders_an_explicit_all_clear():
    analysis = analyze_file("def f():\n    return 1\n", "a.py", "py")
    assert "✅" in render_findings_markdown(analysis)


# ── LLM brief ─────────────────────────────────────────────────────────────────


def test_llm_facts_separate_verified_from_heuristic():
    """
    The split is the point of the brief. A small model told "this is proven"
    can stop looking for it and spend its budget on explanation; told "this is
    a guess", it must verify before repeating the claim.
    """
    source = """
import subprocess
def backup(path):
    cmd = "tar -czf out.tgz " + path
    subprocess.Popen(cmd, shell=True)
    assert path
"""
    facts = build_llm_facts(analyze_python(source, "a.py"))
    assert "VERIFIED ISSUES" in facts
    assert "do not re-derive" in facts
    assert "POSSIBLE ISSUES" in facts


def test_llm_facts_on_a_clean_file_redirect_the_model():
    """
    With nothing to report, an unguided model invents issues to look useful.
    Naming what to look at instead is what keeps a small model's output honest.
    """
    facts = build_llm_facts(analyze_python("def add(a, b):\n    return a + b\n", "a.py"))
    assert "No issues found" in facts
    assert "design" in facts


def test_llm_facts_report_the_most_complex_functions_with_line_numbers():
    branches = "\n".join(f"    if x == {i}: return {i}" for i in range(12))
    source = f"def big(x):\n{branches}\n"
    facts = build_llm_facts(analyze_python(source, "a.py"))
    assert "big(L1" in facts
    assert "cx=" in facts


def test_llm_facts_stay_compact():
    """
    Every token in the brief competes with the source code for the context
    window, which is the binding constraint on a local model.
    """
    source = many_assignments("SECRET", 40)
    facts = build_llm_facts(analyze_python(source, "a.py"))
    assert len(facts) < 1600, "brief must stay small enough to leave room for the code"


# ── False positives found by running this analyzer over this repository ───────
#
# Each case below was reported as a real issue on SavFlux's own source before
# the corresponding rule was tightened. Dogfooding is the only reliable way to
# find these: a rule looks correct until it meets a large body of working code.


def test_optional_file_read_with_pass_fallback_is_not_a_silent_failure():
    """
    From this repo's `config.py`: an optional config file is read inside a
    try/except, and the function returns a default when it is absent. The file
    is genuinely optional, so `pass` is the correct handler — and reporting it
    beside a real swallowed RuntimeError devalues both.
    """
    source = '''
import json
def load_config(p):
    if p.is_file():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}
'''
    analysis = analyze_python(source, "config.py")
    assert "PY-EXC-SILENT" not in rule_ids(analysis)


def test_pass_with_an_explanatory_comment_is_respected():
    """An author saying why is evidence; take their word for it."""
    source = """
def f():
    try:
        risky()
    except Exception:
        pass  # best-effort telemetry, never block the request
"""
    assert "PY-EXC-SILENT" not in rule_ids(analyze_python(source, "a.py"))


def test_trailing_pass_with_no_fallback_is_still_flagged():
    """
    The negative half of the pair. Nothing follows the try, so the error really
    is discarded — this must keep firing or the rule stops being worth having.
    """
    source = """
def f():
    try:
        important_write()
    except Exception:
        pass
"""
    assert "PY-EXC-SILENT" in rule_ids(analyze_python(source, "a.py"))


def test_regex_exec_is_not_reported_as_shell_execution():
    """
    From this repo's frontend: `/language-(\\w+)/.exec(className)` is
    RegExp.prototype.exec. A bare `exec\\s*\\(` pattern called it a shell
    command injection — in four separate components.
    """
    source = 'const match = /language-(\\w+)/.exec(className || "");\n'
    analysis = analyze_generic(source, "MessageBubble.tsx", "tsx")
    assert "JS-SEC-CHILDPROC" not in rule_ids(analysis)


def test_real_child_process_exec_is_still_flagged():
    source = 'const cp = require("child_process");\ncp.exec("ls " + dir);\n'
    assert "JS-SEC-CHILDPROC" in rule_ids(analyze_generic(source, "a.js", "js"))
