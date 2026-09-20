"""
python_analyzer — AST-based security and quality analysis with taint tracking.

Why this exists
---------------
The previous review pass matched regexes against raw lines. That approach has
two failure modes, and both were reproducible on ordinary code:

    conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))

was reported as "Dynamic SQL — use parameterised queries", which is precisely
what that line already does, while:

    sql = f"SELECT * FROM {table} WHERE {where}"
    return sql

went unreported, because the `execute(` call and the f-string are on different
lines and no single line matches the pattern.

A parser removes both failures. We know `?` placeholders with a params tuple
are safe. We can follow `sql` from the f-string to the `execute()` call and
prove the injection. Findings from this module are facts, not guesses, so the
LLM can be told to trust them instead of re-deriving them from raw text — which
is what lets a 7B local model produce review output comparable to a much larger
model on the security axis.

Everything is stdlib `ast`. No network, no model, no dependency, ~1ms/file.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Iterable

from app.services.code_analysis.models import (
    FileAnalysis,
    Finding,
    FunctionMetrics,
    Severity,
)

logger = logging.getLogger(__name__)

# ── Rule configuration ────────────────────────────────────────────────────────

#: Functions that execute a SQL string. Matched on the attribute/callable name,
#: so it works for sqlite3, psycopg2, SQLAlchemy Core and most DBAPI wrappers.
SQL_EXEC_NAMES = frozenset({"execute", "executemany", "executescript", "raw", "text"})

#: SQL keywords that indicate a string is actually a query and not prose.
SQL_KEYWORDS = re.compile(
    r"\b(select|insert\s+into|update|delete\s+from|drop\s+table|create\s+table|alter\s+table)\b",
    re.IGNORECASE,
)

#: Calls that spawn a process. `shell=True` with a non-literal turns into RCE.
SHELL_CALLS = frozenset({"system", "popen", "run", "call", "check_call", "check_output", "Popen"})

#: Direct code-execution sinks.
CODE_EXEC_NAMES = frozenset({"eval", "exec", "compile"})

#: Deserialisers that execute arbitrary code on untrusted input.
UNSAFE_DESERIALIZE = {
    ("pickle", "load"): "CWE-502",
    ("pickle", "loads"): "CWE-502",
    ("cPickle", "load"): "CWE-502",
    ("cPickle", "loads"): "CWE-502",
    ("yaml", "load"): "CWE-502",
    ("marshal", "load"): "CWE-502",
    ("marshal", "loads"): "CWE-502",
    ("shelve", "open"): "CWE-502",
}

#: Hash functions that are broken for security use. Fine for checksums, which
#: is why the finding is MEDIUM and mentions the distinction.
WEAK_HASHES = frozenset({"md5", "sha1"})

#: Names that look like secrets when assigned a literal.
SECRET_NAME = re.compile(
    r"(?i)(^|_)(api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key"
    r"|private[_-]?key|client[_-]?secret|auth[_-]?token)($|_)"
)

#: Values that are obviously not real secrets — placeholders, env lookups.
PLACEHOLDER_VALUE = re.compile(
    r"(?i)^\s*(|none|null|changeme|placeholder|dummy|example|sample|test|fake|xxx+|\.\.\.|"
    r"your[-_ ].*|<.*>|\{\{.*\}\}|\$\{.*\}|sk-(test|ci|xxx).*)\s*$"
)

#: Functions whose return value is attacker-controlled. The taint sources.
TAINT_SOURCES = {
    "input",
    "raw_input",
    "get_json",
    "getvalue",
    "read",
    "recv",
    "readline",
}

#: Attribute chains that yield request data in common web frameworks.
TAINT_ATTRS = frozenset(
    {"args", "form", "json", "data", "params", "query_params", "cookies", "headers", "body", "GET", "POST"}
)


def _name_of(node: ast.AST) -> str:
    """Best-effort dotted name for a call target: `os.path.join` → that string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_of(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _literal_str(node: ast.AST) -> str | None:
    """Return the value if `node` is a plain string literal, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_dynamic_string(node: ast.AST) -> bool:
    """
    True when a string expression is built at runtime from non-literal parts.

    This is the crux of injection detection. A JoinedStr (f-string) with only
    literal pieces is static and safe; one with a FormattedValue interpolates
    something. `"a" + b` concatenates a name. `"...".format(x)` and `"..." % x`
    are the older equivalents.
    """
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp):
        # `"lit" + var` or `"lit %s" % var`
        if isinstance(node.op, (ast.Add, ast.Mod)):
            left_lit = _literal_str(node.left) is not None
            right_lit = _literal_str(node.right) is not None
            if left_lit and right_lit:
                return False  # "a" + "b" folds to a constant
            return left_lit or right_lit or _is_dynamic_string(node.left) or _is_dynamic_string(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("format", "join"):
            return True
    return False


def _string_shape(node: ast.AST) -> str:
    """
    Flatten a string expression to its literal skeleton for keyword matching.

    An f-string's literal parts still tell us whether it is SQL. Interpolated
    holes become a marker so `SELECT * FROM {t}` still matches SQL_KEYWORDS.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " ? "
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return f"{_string_shape(node.left)} ? {_string_shape(node.right)}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            return _string_shape(node.func.value)
    return ""


class _TaintTracker(ast.NodeVisitor):
    """
    Tracks, per function scope, which local names hold runtime-built strings.

    This is intentionally a *reaching-definitions* approximation rather than
    full dataflow: it records the assignment that made a name dynamic, so when
    that name reaches a sink we can report both the sink line and the line
    where the dangerous string was constructed. Loops and branches are not
    path-sensitive — for review purposes over-approximating is right, because a
    string that is dynamic on any path is dynamic.
    """

    def __init__(self) -> None:
        #: name → (line where it became dynamic, accumulated source shape)
        self.dynamic: dict[str, tuple[int, str]] = {}
        #: names holding attacker-controlled data
        self.tainted: dict[str, int] = {}
        #: name → literal skeleton of the string it holds, dynamic or not.
        #: Needed because `sql = "SELECT ..."` then `sql += " WHERE " + x`
        #: only looks like SQL once both halves are considered together.
        self.shapes: dict[str, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if targets:
            shape = _string_shape(node.value)
            is_dynamic = _is_dynamic_string(node.value)
            is_tainted = self._is_taint_source(node.value)

            for name in targets:
                if shape:
                    self.shapes[name] = shape
                else:
                    self.shapes.pop(name, None)

                if is_dynamic:
                    self.dynamic[name] = (node.lineno, shape)
                if is_tainted:
                    self.tainted[name] = node.lineno
                # Rebinding to something static clears a previous mark, so a
                # name that stops being dangerous stops being reported.
                if not is_dynamic and not is_tainted:
                    self.dynamic.pop(name, None)
                    self.tainted.pop(name, None)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # `sql += user_input` — the classic incremental injection. The appended
        # fragment alone rarely contains the SQL verb ("SELECT" was in the
        # original assignment), so the accumulated shape is what gets tested.
        if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
            name = node.target.id
            combined = f"{self.shapes.get(name, '')} {_string_shape(node.value)}".strip()
            self.shapes[name] = combined
            if not isinstance(node.value, ast.Constant):
                self.dynamic[name] = (node.lineno, combined)
        self.generic_visit(node)

    def _is_taint_source(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            name = _name_of(node.func)
            leaf = name.rsplit(".", 1)[-1]
            if leaf in TAINT_SOURCES:
                return True
        if isinstance(node, ast.Subscript):
            return self._is_taint_source(node.value)
        if isinstance(node, ast.Attribute):
            if node.attr in TAINT_ATTRS:
                return True
            return self._is_taint_source(node.value)
        return False


class _PythonVisitor(ast.NodeVisitor):
    """Walks the module, emitting findings and per-function metrics."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.findings: list[Finding] = []
        self.functions: list[FunctionMetrics] = []
        self.imports: list[str] = []
        self._scope_stack: list[_TaintTracker] = [_TaintTracker()]
        self._func_stack: list[ast.AST] = []
        #: id(ExceptHandler) -> enclosing Try. ast has no parent pointers.
        self._try_parents: dict[int, ast.Try] = {}
        #: id(stmt) -> the statement list it lives in, for "what comes next".
        self._try_siblings: dict[int, list[ast.stmt]] = {}
        #: id(stmt) -> the enclosing compound statement, to walk outward when
        #: a try is the last statement of an `if` and the fallback follows it.
        self._block_owner: dict[int, ast.stmt] = {}

    # ── helpers ───────────────────────────────────────────────────────────────

    @property
    def _scope(self) -> _TaintTracker:
        return self._scope_stack[-1]

    def _evidence(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()[:200]
        return ""

    def _add(
        self,
        rule_id: str,
        title: str,
        severity: Severity,
        line: int,
        message: str,
        remediation: str = "",
        cwe: str = "",
        confidence: float = 0.9,
    ) -> None:
        self.findings.append(
            Finding(
                rule_id=rule_id,
                title=title,
                severity=severity,
                line=line,
                message=message,
                evidence=self._evidence(line),
                confidence=confidence,
                remediation=remediation,
                cwe=cwe,
            )
        )

    # ── imports ───────────────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append(node.module)
        self.generic_visit(node)

    # ── functions ─────────────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        tracker = _TaintTracker()
        # Seed: every parameter of a public function is potentially caller-
        # controlled. This is what catches `def q(table): execute(f"...{table}")`.
        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            if arg.arg not in ("self", "cls"):
                tracker.tainted[arg.arg] = node.lineno
        tracker.visit(node)

        self._scope_stack.append(tracker)
        self._func_stack.append(node)

        # Metrics. The decorator list matters for the start line: a route
        # handler's real extent begins at its first decorator.
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        params = len([a for a in node.args.args if a.arg not in ("self", "cls")])
        params += len(node.args.kwonlyargs) + len(node.args.posonlyargs)

        self.functions.append(
            FunctionMetrics(
                name=node.name,
                line=start,
                end_line=end,
                complexity=_cyclomatic_complexity(node),
                length=end - start + 1,
                params=params,
                max_depth=_max_nesting(node),
                has_docstring=ast.get_docstring(node) is not None,
            )
        )

        for child in node.body:
            self.visit(child)

        self._func_stack.pop()
        self._scope_stack.pop()

    # ── exception handling ────────────────────────────────────────────────────

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        is_bare = node.type is None
        is_broad = isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException")

        if is_bare or is_broad:
            # A handler that re-raises or logs is defensible. One whose entire
            # body is `pass` silently swallows every error, which is how the
            # activity-feed bug in this repo hid a RuntimeError for months.
            body_is_silent = all(isinstance(stmt, ast.Pass) for stmt in node.body)
            has_reraise = any(
                isinstance(stmt, ast.Raise) for stmt in ast.walk(node) if isinstance(stmt, ast.Raise)
            )
            if body_is_silent and not self._is_deliberate_fallback(node):
                self._add(
                    "PY-EXC-SILENT",
                    "Silently swallowed exception",
                    Severity.HIGH,
                    node.lineno,
                    "This handler catches everything and does nothing, so failures "
                    "disappear without a trace and the code continues in a broken state.",
                    remediation="Log the exception with `logger.exception(...)`, or narrow the "
                    "`except` to the specific error you can actually handle.",
                    cwe="CWE-390",
                    confidence=0.95,
                )
            elif is_bare and not has_reraise:
                self._add(
                    "PY-EXC-BARE",
                    "Bare `except:`",
                    Severity.MEDIUM,
                    node.lineno,
                    "A bare `except:` also catches KeyboardInterrupt and SystemExit, "
                    "which makes the process hard to stop.",
                    remediation="Use `except Exception:` at minimum, ideally the specific type.",
                    cwe="CWE-396",
                    confidence=0.9,
                )
        self.generic_visit(node)

    def _is_deliberate_fallback(self, node: ast.ExceptHandler) -> bool:
        """
        True when `except: pass` is a considered choice rather than a swallowed bug.

        Found by running this analyzer over this repository: `config.py` reads
        an optional config file inside `try: ... except Exception: pass` and
        falls through to a default. That is correct code — the file is
        genuinely optional — and reporting it as a silent failure alongside a
        real swallowed RuntimeError devalues both.

        The distinguishing signal is what follows the try statement. If the
        enclosing function goes on to return a default, the fallback is
        explicit in the control flow. A handler that is the last thing in the
        function, with nothing after it, really does discard the error.
        """
        parent_try = self._try_parents.get(id(node))
        if parent_try is None:
            return False

        # A comment on the `pass` line is an author telling us it is intentional.
        pass_line = node.body[0].lineno if node.body else node.lineno
        if 1 <= pass_line <= len(self.lines) and "#" in self.lines[pass_line - 1]:
            return True

        # Something concrete happens after the try: a return, a fallback
        # assignment, or another attempt. The error is handled by moving on.
        #
        # The search walks outward, because the try is often the last statement
        # of an enclosing `if` and the fallback `return {}` sits after that if:
        #
        #     if p.is_file():
        #         try: ...
        #         except Exception: pass   <- handler
        #     return {}                    <- fallback, two levels out
        #
        # Only ancestors inside the current function are considered.
        node_to_find: ast.AST = parent_try
        while node_to_find is not None:
            siblings = self._try_siblings.get(id(node_to_find), [])
            index = next((i for i, s in enumerate(siblings) if s is node_to_find), -1)
            if index >= 0 and index + 1 < len(siblings):
                following = siblings[index + 1]
                return isinstance(following, (ast.Return, ast.Assign, ast.Try, ast.If, ast.Expr))
            # Nothing followed it at this level — try the enclosing block.
            node_to_find = self._block_owner.get(id(node_to_find))

        return False

    def visit_Try(self, node: ast.Try) -> None:
        # Record parentage so a handler can see its own try statement and what
        # follows it. ast nodes carry no parent pointer.
        for handler in node.handlers:
            self._try_parents[id(handler)] = node
        self.generic_visit(node)

    def _index_block(self, owner: ast.AST, body: list[ast.stmt]) -> None:
        """
        Record sibling ordering and parentage for a statement list.

        Indexing every statement (not just Try) is what lets the fallback
        search walk outward: the try may be the last statement of an `if`, with
        the fallback `return` following the `if` itself.
        """
        for stmt in body:
            self._try_siblings[id(stmt)] = body
            if isinstance(owner, ast.stmt):
                self._block_owner[id(stmt)] = owner

    # ── assignments: secrets ──────────────────────────────────────────────────

    def visit_Assign(self, node: ast.Assign) -> None:
        literal = _literal_str(node.value)
        if literal is not None:
            for target in node.targets:
                name = target.id if isinstance(target, ast.Name) else (
                    target.attr if isinstance(target, ast.Attribute) else ""
                )
                if name and SECRET_NAME.search(name) and not PLACEHOLDER_VALUE.match(literal):
                    # Short values are usually config keys, not credentials.
                    if len(literal) >= 8:
                        self._add(
                            "PY-SEC-HARDCODED",
                            "Hardcoded credential",
                            Severity.CRITICAL,
                            node.lineno,
                            f"`{name}` is assigned a literal string. Anyone with repository "
                            "access — including anyone who clones a fork — has this value, "
                            "and rotating it means a code change and a deploy.",
                            remediation="Read it from the environment: "
                            f"`{name} = os.environ[\"{name.upper()}\"]`, and add the real value "
                            "to your secret manager. Rotate the exposed credential.",
                            cwe="CWE-798",
                            confidence=0.85,
                        )
        self.generic_visit(node)

    # ── calls: the sinks ──────────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        full_name = _name_of(node.func)
        leaf = full_name.rsplit(".", 1)[-1]

        self._check_sql(node, leaf)
        self._check_shell(node, full_name, leaf)
        self._check_code_exec(node, full_name, leaf)
        self._check_deserialize(node, full_name)
        self._check_weak_hash(node, full_name, leaf)
        self._check_requests_verify(node, full_name)
        self._check_tempfile(node, full_name)

        self.generic_visit(node)

    def _check_sql(self, node: ast.Call, leaf: str) -> None:
        if leaf not in SQL_EXEC_NAMES or not node.args:
            return
        query = node.args[0]

        # Parameterised call: `execute(sql, params)`. The driver escapes the
        # params, so even a dynamic-looking query string is safe as long as the
        # interpolation isn't in the SQL itself.
        has_params = len(node.args) > 1 or any(kw.arg in ("params", "parameters", "vars") for kw in node.keywords)

        shape = _string_shape(query)
        looks_like_sql = bool(SQL_KEYWORDS.search(shape))

        if isinstance(query, ast.Name):
            # Query built earlier and passed by name — follow it.
            origin = self._scope.dynamic.get(query.id)
            if origin:
                origin_line, origin_shape = origin
                if SQL_KEYWORDS.search(origin_shape):
                    tainted = query.id in self._scope.tainted or self._has_tainted_input(origin_shape)
                    self._add(
                        "PY-SEC-SQLI",
                        "SQL injection",
                        Severity.CRITICAL if tainted else Severity.HIGH,
                        node.lineno,
                        f"The query passed here is built by string interpolation on line "
                        f"{origin_line}, so any quote or semicolon in the interpolated value "
                        "changes the statement being executed.",
                        remediation="Pass values as parameters instead of interpolating them: "
                        "`cursor.execute(\"SELECT * FROM t WHERE id = ?\", (value,))`. If the "
                        "*table* name must vary, validate it against an allow-list.",
                        cwe="CWE-89",
                        confidence=0.9,
                    )
            return

        if _is_dynamic_string(query) and looks_like_sql:
            # Interpolating directly into execute() is unsafe even with params,
            # because the interpolated part is not parameterised.
            self._add(
                "PY-SEC-SQLI",
                "SQL injection",
                Severity.CRITICAL,
                node.lineno,
                "The SQL string is assembled with interpolation at the call site. "
                "The interpolated value is inserted into the statement verbatim, "
                + ("and the separate parameters argument does not protect it."
                   if has_params else "so it can terminate the string and append arbitrary SQL."),
                remediation="Use placeholders for every value: "
                "`execute(\"... WHERE id = %s\", (value,))`.",
                cwe="CWE-89",
                confidence=0.95,
            )

    def _has_tainted_input(self, shape: str) -> bool:
        return "?" in shape

    def _check_shell(self, node: ast.Call, full_name: str, leaf: str) -> None:
        if leaf not in SHELL_CALLS:
            return

        is_os_system = full_name in ("os.system", "os.popen")
        shell_true = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )

        if not (is_os_system or shell_true):
            return
        if not node.args:
            return

        command = node.args[0]
        dynamic = _is_dynamic_string(command)
        if isinstance(command, ast.Name):
            origin = self._scope.dynamic.get(command.id)
            dynamic = origin is not None
        if not dynamic:
            # `subprocess.run("ls -la", shell=True)` — a fixed command. Still
            # worth a note (shell=True is a habit worth breaking) but it is not
            # an injection, and calling it one destroys the report's credibility.
            self._add(
                "PY-SEC-SHELL-STATIC",
                "Unnecessary shell invocation",
                Severity.LOW,
                node.lineno,
                "The command is a fixed string, so there is no injection here, but "
                "running it through a shell costs a process and inherits shell quoting rules.",
                remediation="Pass an argument list and drop `shell=True`: "
                "`subprocess.run([\"ls\", \"-la\"])`.",
                cwe="CWE-78",
                confidence=0.7,
            )
            return

        self._add(
            "PY-SEC-SHELL",
            "Shell command injection",
            Severity.CRITICAL,
            node.lineno,
            "A command built from runtime values is handed to a shell. A value "
            "containing `;`, `&&` or a backtick runs a second command as this process.",
            remediation="Drop the shell and pass an argument list: "
            "`subprocess.run([\"tar\", \"-czf\", dest, src], shell=False)`. "
            "If a shell is unavoidable, quote every substitution with `shlex.quote()`.",
            cwe="CWE-78",
            confidence=0.95,
        )

    def _check_code_exec(self, node: ast.Call, full_name: str, leaf: str) -> None:
        if leaf not in CODE_EXEC_NAMES or full_name not in CODE_EXEC_NAMES:
            return  # skip `self.eval(...)` and similar unrelated methods
        if not node.args:
            return
        arg = node.args[0]
        static = _literal_str(arg) is not None
        if isinstance(arg, ast.Name):
            static = arg.id not in self._scope.dynamic and arg.id not in self._scope.tainted
        self._add(
            "PY-SEC-EXEC",
            f"`{leaf}()` on dynamic input" if not static else f"`{leaf}()` call",
            Severity.CRITICAL if not static else Severity.MEDIUM,
            node.lineno,
            "`eval`/`exec` run whatever string they are given as Python. "
            + ("The argument here is built at runtime, so control of that value is "
               "control of this process."
               if not static else
               "The argument is a literal here, but the call is a standing hazard."),
            remediation="Use `ast.literal_eval` for data, a dict dispatch table for "
            "behaviour, or `json.loads` for serialised structures.",
            cwe="CWE-95",
            confidence=0.95 if not static else 0.6,
        )

    def _check_deserialize(self, node: ast.Call, full_name: str) -> None:
        parts = full_name.split(".")
        if len(parts) < 2:
            return
        key = (parts[-2], parts[-1])
        if key not in UNSAFE_DESERIALIZE:
            return
        # yaml.load with an explicit SafeLoader is fine.
        if key == ("yaml", "load"):
            loader = next((kw for kw in node.keywords if kw.arg == "Loader"), None)
            loader_name = _name_of(loader.value) if loader else (
                _name_of(node.args[1]) if len(node.args) > 1 else ""
            )
            if "safe" in loader_name.lower():
                return
        self._add(
            "PY-SEC-DESERIALIZE",
            f"Unsafe deserialisation via `{full_name}`",
            Severity.HIGH,
            node.lineno,
            "This deserialiser can instantiate arbitrary objects, so a crafted payload "
            "executes code during loading — before any of your validation runs.",
            remediation="Use `yaml.safe_load` for YAML, or `json` for data you did not "
            "produce yourself. Reserve `pickle` for data your own process wrote.",
            cwe=UNSAFE_DESERIALIZE[key],
            confidence=0.9,
        )

    def _check_weak_hash(self, node: ast.Call, full_name: str, leaf: str) -> None:
        if leaf not in WEAK_HASHES or not full_name.startswith("hashlib."):
            return
        # hashlib.md5(..., usedforsecurity=False) is an explicit opt-out.
        if any(kw.arg == "usedforsecurity" for kw in node.keywords):
            return
        self._add(
            "PY-SEC-WEAKHASH",
            f"Weak hash `{leaf}`",
            Severity.MEDIUM,
            node.lineno,
            f"`{leaf}` is collision-broken. It is fine as a cache key or checksum, "
            "but not for passwords, signatures, or integrity checks.",
            remediation="For integrity use `hashlib.sha256`. For passwords use "
            "`bcrypt`/`argon2`. If this really is just a cache key, pass "
            "`usedforsecurity=False` to say so.",
            cwe="CWE-327",
            confidence=0.75,
        )

    def _check_requests_verify(self, node: ast.Call, full_name: str) -> None:
        if not full_name.startswith(("requests.", "httpx.", "session.", "aiohttp.")):
            return
        for kw in node.keywords:
            if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                self._add(
                    "PY-SEC-NOVERIFY",
                    "TLS verification disabled",
                    Severity.HIGH,
                    node.lineno,
                    "`verify=False` accepts any certificate, so anyone on the network path "
                    "can read and modify this traffic. TLS is doing nothing here.",
                    remediation="Remove `verify=False`. For an internal CA, point `verify` at "
                    "the CA bundle path instead.",
                    cwe="CWE-295",
                    confidence=0.95,
                )

    def _check_tempfile(self, node: ast.Call, full_name: str) -> None:
        if full_name == "tempfile.mktemp":
            self._add(
                "PY-SEC-MKTEMP",
                "Race-prone `tempfile.mktemp`",
                Severity.MEDIUM,
                node.lineno,
                "`mktemp` returns a name without creating the file, so another process "
                "can create it first and win the race.",
                remediation="Use `tempfile.NamedTemporaryFile` or `tempfile.mkstemp`, which "
                "create the file atomically.",
                cwe="CWE-377",
                confidence=0.9,
            )

    # ── comparisons ───────────────────────────────────────────────────────────

    def visit_Compare(self, node: ast.Compare) -> None:
        # `if token == secret` leaks length and prefix through timing.
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            left_name = _name_of(node.left)
            right_name = _name_of(comparator)
            names = f"{left_name} {right_name}".lower()
            if any(k in names for k in ("token", "secret", "password", "signature", "hmac", "digest", "api_key")):
                self._add(
                    "PY-SEC-TIMING",
                    "Non-constant-time secret comparison",
                    Severity.MEDIUM,
                    node.lineno,
                    "`==` returns as soon as two bytes differ, so response time reveals how "
                    "much of the secret was correct and the value can be recovered byte by byte.",
                    remediation="Use `hmac.compare_digest(a, b)`, which always takes the same "
                    "time for equal-length inputs.",
                    cwe="CWE-208",
                    confidence=0.7,
                )
        self.generic_visit(node)

    # ── assert in production paths ────────────────────────────────────────────

    def visit_Assert(self, node: ast.Assert) -> None:
        self._add(
            "PY-QUAL-ASSERT",
            "`assert` used for validation",
            Severity.LOW,
            node.lineno,
            "`python -O` strips assert statements, so any check written this way "
            "silently disappears in an optimised deployment.",
            remediation="Raise explicitly: `if not cond: raise ValueError(...)`. Keep "
            "`assert` for test code and internal invariants.",
            cwe="CWE-617",
            confidence=0.6,
        )
        self.generic_visit(node)


# ── complexity metrics ────────────────────────────────────────────────────────


def _cyclomatic_complexity(node: ast.AST) -> int:
    """
    McCabe complexity: one plus the number of independent decision points.

    Counted: if / for / while / except / with-item / boolean operator /
    comprehension condition / match case / ternary / assert. This mirrors what
    `radon` reports, so the numbers are comparable to published thresholds
    (>10 needs refactoring, >20 is effectively untestable).
    """
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.Assert)):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # `a and b and c` is two decision points, not one.
            complexity += len(child.values) - 1
        elif isinstance(child, ast.IfExp):
            complexity += 1
        elif isinstance(child, (ast.comprehension,)):
            complexity += 1 + len(child.ifs)
        elif isinstance(child, ast.match_case):
            complexity += 1
    return complexity


def _max_nesting(node: ast.AST) -> int:
    """Deepest block nesting inside a function. Depth > 4 is a readability cliff."""

    def depth(n: ast.AST, current: int = 0) -> int:
        deepest = current
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)):
                deepest = max(deepest, depth(child, current + 1))
            else:
                deepest = max(deepest, depth(child, current))
        return deepest

    return depth(node)


# ── line accounting ───────────────────────────────────────────────────────────


def _count_lines(lines: Iterable[str]) -> tuple[int, int, int]:
    """Return (code, comment, blank). Docstring bodies count as comments."""
    code = comment = blank = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            blank += 1
        elif stripped.startswith("#"):
            comment += 1
        else:
            code += 1
    return code, comment, blank


# ── entry point ───────────────────────────────────────────────────────────────


def analyze_python(source: str, file_name: str = "") -> FileAnalysis:
    """
    Parse and analyse a Python file.

    A syntax error is reported in `parse_error` rather than raised: a review of
    a file that does not compile is still useful, and the caller falls back to
    language-agnostic heuristics.
    """
    lines = source.splitlines()
    code, comment, blank = _count_lines(lines)
    analysis = FileAnalysis(
        file_name=file_name,
        language="python",
        total_lines=len(lines),
        code_lines=code,
        comment_lines=comment,
        blank_lines=blank,
    )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        analysis.parse_error = f"line {exc.lineno}: {exc.msg}"
        return analysis
    except (ValueError, RecursionError) as exc:
        # ValueError: source contains null bytes. RecursionError: pathological
        # nesting. Both are real inputs from a scraped repository.
        analysis.parse_error = str(exc)
        return analysis

    visitor = _PythonVisitor(lines)
    # Index every statement list before walking: a handler needs to know what
    # follows its try block, and the visitor reaches the handler first.
    for parent in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(parent, field, None)
            if isinstance(block, list):
                visitor._index_block(parent, block)
    visitor.visit(tree)

    analysis.findings = visitor.findings
    analysis.functions = visitor.functions
    analysis.imports = visitor.imports

    _add_complexity_findings(analysis)
    return analysis


def _add_complexity_findings(analysis: FileAnalysis) -> None:
    """Turn the worst metric outliers into findings so they reach the report."""
    for fn in analysis.functions:
        if fn.complexity > 20:
            analysis.findings.append(
                Finding(
                    rule_id="PY-QUAL-COMPLEXITY",
                    title=f"`{fn.name}` is very complex",
                    severity=Severity.HIGH,
                    line=fn.line,
                    message=f"Cyclomatic complexity {fn.complexity} means at least "
                    f"{fn.complexity} paths to cover. Functions above 20 are where defects "
                    "concentrate, and full branch coverage is impractical.",
                    evidence=f"def {fn.name}(...)  # {fn.length} lines, complexity {fn.complexity}",
                    confidence=1.0,
                    remediation="Extract the independent branches into named helpers; aim to "
                    "get each piece under 10.",
                )
            )
        elif fn.complexity > 10:
            analysis.findings.append(
                Finding(
                    rule_id="PY-QUAL-COMPLEXITY",
                    title=f"`{fn.name}` is complex",
                    severity=Severity.MEDIUM,
                    line=fn.line,
                    message=f"Cyclomatic complexity {fn.complexity} (threshold 10).",
                    evidence=f"def {fn.name}(...)  # {fn.length} lines",
                    confidence=1.0,
                    remediation="Split out the conditional branches into helpers.",
                )
            )

        if fn.max_depth >= 5:
            analysis.findings.append(
                Finding(
                    rule_id="PY-QUAL-NESTING",
                    title=f"`{fn.name}` nests {fn.max_depth} levels deep",
                    severity=Severity.MEDIUM,
                    line=fn.line,
                    message="Deep nesting forces the reader to hold every enclosing condition "
                    "in mind at once.",
                    evidence=f"def {fn.name}(...)",
                    confidence=1.0,
                    remediation="Invert the conditions and return early, or extract the inner "
                    "block into its own function.",
                )
            )

        if fn.params > 7:
            analysis.findings.append(
                Finding(
                    rule_id="PY-QUAL-PARAMS",
                    title=f"`{fn.name}` takes {fn.params} parameters",
                    severity=Severity.LOW,
                    line=fn.line,
                    message="Long parameter lists are easy to call incorrectly, especially "
                    "when several share a type.",
                    evidence=f"def {fn.name}(...)",
                    confidence=1.0,
                    remediation="Group the related arguments into a dataclass or TypedDict.",
                )
            )
