"""
test_inline_comments.py — Unit tests for line-level PR comments.
"""

from app.services.impact_analyzer import inline_comments_for_diff

DIFF = """\
diff --git a/auth.py b/auth.py
--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,5 @@
 import os
+API_KEY = "sk-live-abc123"
 def login():
-    pass
+    eval(user_input)
+    print("debug", user_input)
"""

DIFF_TWO_FILES = DIFF + """\
diff --git a/db.py b/db.py
--- a/db.py
+++ b/db.py
@@ -10,2 +10,3 @@
 def query(name):
+    return db.execute("SELECT * FROM users WHERE name = '" + name + "'")
"""


def test_flags_secret_eval_and_print_with_line_numbers():
    comments = inline_comments_for_diff(DIFF)
    by_rule = {c["rule"]: c for c in comments}
    assert by_rule["possible-secret"]["line"] == 2
    assert by_rule["eval-exec"]["line"] == 4
    assert by_rule["debug-print"]["line"] == 5
    assert all(c["path"] == "auth.py" for c in comments)
    assert by_rule["possible-secret"]["severity"] == "high"
    assert by_rule["debug-print"]["severity"] == "low"


def test_sql_concatenation_in_second_file():
    comments = inline_comments_for_diff(DIFF_TWO_FILES)
    sql = [c for c in comments if c["rule"] == "sql-concatenation"]
    assert len(sql) == 1
    assert sql[0]["path"] == "db.py"
    assert sql[0]["line"] == 11
    # sorted by (path, line)
    keys = [(c["path"], c["line"]) for c in comments]
    assert keys == sorted(keys)


def test_clean_diff_has_no_comments():
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1,2 @@\n x = 1\n+y = x + 1\n"
    assert inline_comments_for_diff(diff) == []


def test_max_comments_cap():
    blob = DIFF * 20
    assert len(inline_comments_for_diff(blob, max_comments=5)) == 5
