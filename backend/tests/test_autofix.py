"""
Tests for deterministic autofix.

An autofix tool earns trust slowly and loses it in one bad merge. The tests
that matter most here are not "does it fix things" but:

  * it never produces a file that fails to parse
  * it never trades one finding for a worse one
  * it refuses instead of guessing when the fix needs judgement
  * it touches only the offending line, so the diff is reviewable

Every fixer therefore has a negative case proving it declines the forms it
cannot safely handle.
"""

from __future__ import annotations

import ast

import pytest

from app.services.code_analysis import analyze_file
from app.services.code_analysis.autofix import (
    FIXABLE_RULES,
    _FIXERS,
    autofix_python,
    summarise_fixes,
)
from app.services.code_analysis.models import Finding, Severity
from app.services.code_analysis.python_analyzer import analyze_python
from tests.secret_fixtures import assignment, fake_api_key, fake_password


def fix(source: str, file_name: str = "t.py"):
    """Analyse then autofix, the way the review pipeline does."""
    findings = analyze_python(source, file_name).findings
    return autofix_python(source, findings, file_name)


def rule_ids(source: str) -> set[str]:
    return {f.rule_id for f in analyze_python(source, "t.py").findings}


# ── Contract ──────────────────────────────────────────────────────────────────


def test_every_fixable_rule_has_a_fixer():
    """The advertised list and the implementation must not drift apart."""
    assert set(_FIXERS) == FIXABLE_RULES


def test_result_always_parses():
    """The single non-negotiable property: never emit a broken file."""
    source = (
        "import hashlib, requests, yaml, tempfile\n"
        "def f(a, b):\n"
        "    requests.get(a, verify=False)\n"
        "    hashlib.md5(b)\n"
        "    yaml.load(a)\n"
        "    tempfile.mktemp()\n"
        "    return a == b\n"
    )
    ast.parse(fix(source).content)  # raises if the fix corrupted the file


# ── verify=False ──────────────────────────────────────────────────────────────


def test_verify_false_is_removed():
    source = "import requests\ndef f(u):\n    return requests.get(u, verify=False)\n"
    result = fix(source)
    assert "verify=False" not in result.content
    assert "requests.get(u)" in result.content
    assert "PY-SEC-NOVERIFY" not in rule_ids(result.content)


def test_verify_false_as_the_only_keyword_still_leaves_valid_syntax():
    source = "import requests\ndef f(u):\n    return requests.get(url=u, verify=False)\n"
    result = fix(source)
    ast.parse(result.content)
    assert "verify" not in result.content


def test_verify_false_among_other_keywords_keeps_the_others():
    source = "import requests\ndef f(u):\n    return requests.get(u, verify=False, timeout=30)\n"
    result = fix(source)
    assert "timeout=30" in result.content
    assert "verify=False" not in result.content


# ── Weak hash ─────────────────────────────────────────────────────────────────


def test_md5_gains_an_explicit_usedforsecurity_marker():
    """
    The fix records intent — "this is a checksum" — rather than claiming md5 is
    suddenly safe. Changing the algorithm is a judgement call for a human.
    """
    source = "import hashlib\ndef k(d):\n    return hashlib.md5(d).hexdigest()\n"
    result = fix(source)
    assert "usedforsecurity=False" in result.content
    assert "PY-SEC-WEAKHASH" not in rule_ids(result.content)


def test_md5_with_no_arguments_does_not_gain_a_leading_comma():
    source = "import hashlib\nh = hashlib.md5()\n"
    result = fix(source)
    ast.parse(result.content)
    assert "(, " not in result.content
    assert "md5(usedforsecurity=False)" in result.content


# ── Timing-safe comparison ────────────────────────────────────────────────────


def test_secret_comparison_becomes_compare_digest():
    source = "def check(token, expected):\n    return token == expected\n"
    result = fix(source)
    assert "hmac.compare_digest(token, expected)" in result.content
    assert "PY-SEC-TIMING" not in rule_ids(result.content)


def test_hmac_import_is_added_once_and_below_existing_imports():
    source = "import os\nimport sys\ndef check(token, expected):\n    return token == expected\n"
    result = fix(source)
    lines = result.content.splitlines()
    assert result.content.count("import hmac") == 1
    # Must land in the import block, not above it or in the function body.
    assert lines.index("import hmac") == 2


def test_existing_hmac_import_is_not_duplicated():
    source = "import hmac\ndef check(token, expected):\n    return token == expected\n"
    assert fix(source).content.count("import hmac") == 1


def test_import_is_inserted_below_a_module_docstring():
    """Inserting at line 1 would push the import above the docstring."""
    source = '"""Module docstring."""\ndef check(token, secret):\n    return token == secret\n'
    result = fix(source)
    lines = result.content.splitlines()
    assert lines[0] == '"""Module docstring."""'
    assert "import hmac" in lines[1]


# ── yaml.load ─────────────────────────────────────────────────────────────────


def test_yaml_load_becomes_safe_load():
    source = "import yaml\ndef f(t):\n    return yaml.load(t)\n"
    result = fix(source)
    assert "yaml.safe_load(t)" in result.content
    assert "PY-SEC-DESERIALIZE" not in rule_ids(result.content)


def test_yaml_load_with_an_explicit_loader_is_left_alone():
    """
    With a Loader argument the call is already a deliberate choice, and
    rewriting it would change behaviour rather than fix a bug.
    """
    source = "import yaml\nd = yaml.load(t, Loader=yaml.FullLoader)\n"
    result = fix(source)
    assert "yaml.load(t, Loader=yaml.FullLoader)" in result.content


def test_pickle_loads_is_not_autofixed():
    """
    There is no safe drop-in for `pickle.loads`. The right fix depends on what
    the data is, so this is reported and left for a human.
    """
    source = "import pickle\ndef f(b):\n    return pickle.loads(b)\n"
    result = fix(source)
    assert "pickle.loads(b)" in result.content
    assert not result.changed


# ── Bare except ───────────────────────────────────────────────────────────────


def test_bare_except_becomes_except_exception():
    source = "def f():\n    try:\n        g()\n    except:\n        return None\n"
    result = fix(source)
    assert "except Exception:" in result.content
    assert "PY-EXC-BARE" not in rule_ids(result.content)


def test_bare_except_fix_preserves_indentation():
    """
    A fix that reindents turns a one-line security diff into a whole-function
    diff, and reviewers stop reading those.

    Note the handler body is `return None`, not `pass`: a silent `pass` is
    reported as PY-EXC-SILENT, which is deliberately not autofixable because
    what to log or handle is a judgement call.
    """
    source = (
        "class A:\n"
        "    def f(self):\n"
        "        try:\n"
        "            g()\n"
        "        except:\n"
        "            return None\n"
    )
    result = fix(source)
    assert "        except Exception:" in result.content
    assert "    def f(self):" in result.content, "surrounding indentation must survive"


def test_silently_swallowed_exception_is_not_autofixed():
    """
    `except: pass` needs a human: only the author knows whether the right fix
    is to log it, narrow the exception type, or handle the failure. Rewriting
    it to `except Exception: pass` would silence the finding while leaving the
    actual bug in place — the worst possible outcome for an autofix tool.
    """
    source = "def f():\n    try:\n        g()\n    except:\n        pass\n"
    assert "PY-EXC-SILENT" in rule_ids(source)
    result = fix(source)
    assert not result.changed
    assert "PY-EXC-SILENT" not in FIXABLE_RULES


def test_bare_except_that_reraises_is_not_reported_and_so_not_fixed():
    source = "def f():\n    try:\n        g()\n    except:\n        raise\n"
    result = fix(source)
    assert not result.changed
    assert "except:" in result.content


# ── Rules deliberately NOT autofixed ──────────────────────────────────────────


@pytest.mark.parametrize(
    "source,rule",
    [
        ('def q(c, t):\n    c.execute(f"SELECT * FROM {t}")\n', "PY-SEC-SQLI"),
        ('import subprocess\ndef b(p):\n    subprocess.Popen("tar " + p, shell=True)\n', "PY-SEC-SHELL"),
        (assignment("API_KEY", fake_api_key()), "PY-SEC-HARDCODED"),
        ("def f(u):\n    return eval(u)\n", "PY-SEC-EXEC"),
    ],
)
def test_judgement_requiring_findings_are_reported_but_never_rewritten(source, rule):
    """
    These need context a parser does not have: which column is a value vs an
    identifier, what the shell command should become, where the secret should
    live. Guessing here is how autofix tools break production.
    """
    assert rule in rule_ids(source), "precondition: the analyzer should find this"
    result = fix(source)
    assert not result.changed, f"{rule} must not be autofixed"
    assert rule not in FIXABLE_RULES


# ── Verification gate ─────────────────────────────────────────────────────────


def test_a_fix_that_fails_to_parse_is_rejected(monkeypatch):
    """The parse gate is the last line of defence; prove it actually fires."""
    import app.services.code_analysis.autofix as autofix_mod

    monkeypatch.setitem(
        autofix_mod._FIXERS, "PY-SEC-NOVERIFY", lambda src, tree, f: "def broken(:\n"
    )
    source = "import requests\nrequests.get(u, verify=False)\n"
    result = fix(source)

    assert not result.changed
    assert result.content == source
    assert any("does not parse" in r for r in result.rejected)


def test_a_fix_that_introduces_a_worse_finding_is_rejected(monkeypatch):
    """
    The condition that matters most: an autofix which trades a MEDIUM for a
    CRITICAL has made things worse while looking like an improvement.
    """
    import app.services.code_analysis.autofix as autofix_mod

    monkeypatch.setitem(
        autofix_mod._FIXERS,
        "PY-SEC-MKTEMP",
        lambda src, tree, f: "import tempfile\n" + assignment("PASSWORD", fake_password()),
    )
    source = "import tempfile\np = tempfile.mktemp()\n"
    result = fix(source)

    assert not result.changed
    assert any("introduced a new" in r for r in result.rejected)


def test_a_fix_that_does_not_remove_the_finding_is_rejected(monkeypatch):
    import app.services.code_analysis.autofix as autofix_mod

    # Returns a changed file that still contains the same problem.
    monkeypatch.setitem(
        autofix_mod._FIXERS,
        "PY-SEC-MKTEMP",
        lambda src, tree, f: src + "\n# cosmetic change only\np2 = tempfile.mktemp()\n",
    )
    source = "import tempfile\np = tempfile.mktemp()\n"
    result = fix(source)

    assert not result.changed
    assert any("still present" in r for r in result.rejected)


def test_a_raising_fixer_is_contained(monkeypatch):
    """A bug in one fixer must not fail the request or block the others."""
    import app.services.code_analysis.autofix as autofix_mod

    def exploding(src, tree, f):
        raise RuntimeError("boom")

    monkeypatch.setitem(autofix_mod._FIXERS, "PY-SEC-MKTEMP", exploding)
    source = "import tempfile, requests\np = tempfile.mktemp()\nrequests.get(u, verify=False)\n"
    result = fix(source)

    # The unrelated fix still landed.
    assert "verify=False" not in result.content
    assert any("internal error" in r for r in result.rejected)


# ── Multiple fixes in one file ────────────────────────────────────────────────


def test_several_fixes_apply_together_and_clear_the_file():
    source = (
        "import hashlib\n"
        "import requests\n"
        "import tempfile\n"
        "import yaml\n"
        "def fetch(u):\n"
        "    return requests.get(u, verify=False)\n"
        "def key(d):\n"
        "    return hashlib.md5(d).hexdigest()\n"
        "def load(t):\n"
        "    return yaml.load(t)\n"
        "def check(token, expected):\n"
        "    return token == expected\n"
        "def tmp():\n"
        "    return tempfile.mktemp()\n"
    )
    before = analyze_python(source, "t.py")
    result = fix(source)
    after = analyze_python(result.content, "t.py")

    assert len(result.fixes) == 5
    assert after.findings == []
    assert after.risk_score() > before.risk_score()
    ast.parse(result.content)


def test_line_numbers_stay_correct_after_an_import_shifts_the_file():
    """
    Adding `import hmac` moves every line below it down by one. A later fix
    using the original line number would edit the wrong line — which is why
    findings are relocated by re-analysis between fixes.
    """
    source = (
        "import tempfile\n"
        "def check(token, expected):\n"
        "    return token == expected\n"
        "def tmp():\n"
        "    return tempfile.mktemp()\n"
    )
    result = fix(source)
    assert "hmac.compare_digest" in result.content
    assert "tempfile.mkstemp()" in result.content
    assert analyze_python(result.content, "t.py").findings == []


def test_clean_file_is_returned_byte_identical():
    """No findings must mean no diff at all, not a reformatted file."""
    source = "def add(a, b):\n    return a + b\n"
    result = fix(source)
    assert result.content == source
    assert not result.changed


def test_only_the_offending_lines_change():
    """
    A minimal diff is what makes the fix reviewable. Everything except the
    target line must survive byte-for-byte.
    """
    source = (
        "# a comment that must survive\n"
        "import requests\n"
        "\n"
        "\n"
        "def f(u):\n"
        "    # inline comment\n"
        "    return requests.get(u, verify=False)   # trailing note\n"
    )
    result = fix(source)
    before_lines = source.splitlines()
    after_lines = result.content.splitlines()

    assert len(before_lines) == len(after_lines)
    differing = [i for i, (a, b) in enumerate(zip(before_lines, after_lines)) if a != b]
    assert differing == [6], f"expected only line 7 to change, got {differing}"
    assert "# trailing note" in after_lines[6]
    assert "# a comment that must survive" in result.content


# ── Reporting ─────────────────────────────────────────────────────────────────


def test_summary_lists_each_fix_with_its_line_and_rule():
    source = "import requests\ndef f(u):\n    return requests.get(u, verify=False)\n"
    summary = summarise_fixes(fix(source))
    assert "PY-SEC-NOVERIFY" in summary
    assert "L3" in summary


def test_summary_reports_skipped_fixes_honestly(monkeypatch):
    """
    Silently dropping a failed fix would let a user believe a vulnerability was
    handled when it was not.
    """
    import app.services.code_analysis.autofix as autofix_mod

    monkeypatch.setitem(autofix_mod._FIXERS, "PY-SEC-MKTEMP", lambda src, tree, f: "def broken(:\n")
    summary = summarise_fixes(fix("import tempfile\np = tempfile.mktemp()\n"))
    assert "Skipped" in summary


def test_summary_on_a_clean_file():
    assert "No automatic fixes" in summarise_fixes(fix("x = 1\n"))


# ── Robustness ────────────────────────────────────────────────────────────────


def test_unparseable_input_is_returned_unchanged():
    source = "def broken(:\n"
    result = fix(source)
    assert result.content == source
    assert not result.changed


def test_empty_file_is_handled():
    assert fix("").content == ""


def test_finding_pointing_past_the_end_of_the_file_is_ignored():
    """Stale findings from a previous version of a file must not crash."""
    source = "x = 1\n"
    bogus = Finding("PY-EXC-BARE", "Bare except", Severity.MEDIUM, 9999, "stale")
    result = autofix_python(source, [bogus], "t.py")
    assert result.content == source


# ── API surface ───────────────────────────────────────────────────────────────


def test_autofix_endpoint_returns_a_patch_and_before_after_scores(client):
    source = (
        "import requests\n"
        "import hashlib\n"
        "def fetch(u):\n"
        "    return requests.get(u, verify=False)\n"
        "def key(d):\n"
        "    return hashlib.md5(d).hexdigest()\n"
    )
    response = client.post(
        "/api/v1/review/autofix", json={"path": "net.py", "content": source}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["fixed"] is True
    assert len(data["fixes"]) == 2
    assert data["findings_after"] < data["findings_before"]
    assert data["score_after"] > data["score_before"]
    assert data["patch"]["diff"].startswith("diff --git a/net.py b/net.py")


def test_autofix_endpoint_reports_an_honest_empty_result(client):
    """
    A file whose only problems need human judgement must come back with
    `fixed: false` and no patch — not an empty diff implying success.
    """
    source = 'def q(conn, table):\n    conn.execute(f"SELECT * FROM {table}")\n'
    response = client.post(
        "/api/v1/review/autofix", json={"path": "db.py", "content": source}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["fixed"] is False
    assert data["patch"] is None
    assert data["findings_after"] == data["findings_before"]


def test_autofix_endpoint_rejects_non_python(client):
    """Saying "Python only" is better than silently returning no fixes."""
    response = client.post(
        "/api/v1/review/autofix", json={"path": "app.js", "content": "eval(x);"}
    )
    assert response.status_code == 400
    assert "python" in response.json()["detail"].lower()


def test_autofix_endpoint_404s_when_the_file_is_unknown(client):
    response = client.post("/api/v1/review/autofix", json={"path": "ghost.py"})
    assert response.status_code == 404


def test_autofix_endpoint_requires_a_path(client):
    response = client.post("/api/v1/review/autofix", json={"path": "  "})
    assert response.status_code == 422
