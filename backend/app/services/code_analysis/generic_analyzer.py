"""
generic_analyzer — Language-agnostic analysis for files we cannot parse.

Python gets a real AST. Everything else (JS/TS, Go, Java, Rust, Ruby, PHP, C#)
gets this: a brace/indent-aware scanner that is still substantially better than
matching regexes against raw lines, because it tracks two things a flat regex
cannot:

  1. Whether a line is inside a comment or a string literal. The old pass
     reported the word "password" in a comment as a hardcoded credential.
  2. Block depth, so nesting and function extents are real measurements rather
     than Python-only indentation heuristics that silently returned 0 for every
     brace-delimited language.

Findings here carry lower confidence than the Python analyzer's, and the report
labels them accordingly. Saying "likely" when we are guessing is what makes the
"proven" findings worth believing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.code_analysis.js_analyzer import detect_structural_issues
from app.services.code_analysis.models import (
    FileAnalysis,
    Finding,
    FunctionMetrics,
    Severity,
)

#: Line-comment prefixes by language family. Order matters: check longest first.
LINE_COMMENTS = ("///", "//", "#", "--")
BLOCK_COMMENT_OPEN = ("/*", "<!--", '"""', "'''")
BLOCK_COMMENT_CLOSE = {"/*": "*/", "<!--": "-->", '"""': '"""', "'''": "'''"}

#: Languages where `#` starts a comment. In JS/Go/Java it is a private field or
#: a preprocessor directive, so treating it as a comment loses real code.
HASH_COMMENT_LANGS = frozenset(
    {"py", "python", "rb", "ruby", "sh", "bash", "zsh", "yaml", "yml", "toml", "r", "pl", "perl"}
)

BRACE_LANGS = frozenset(
    {"js", "jsx", "ts", "tsx", "go", "java", "c", "cpp", "cc", "h", "hpp", "cs",
     "rs", "swift", "kt", "kts", "scala", "php", "dart", "groovy"}
)


@dataclass
class _Line:
    """One physical line with the parts we can trust to be real code."""

    number: int
    raw: str
    #: The line with comments and string bodies blanked out. Pattern matching
    #: runs against this, so a URL in a comment cannot trigger a finding.
    code: str

    @property
    def is_blank(self) -> bool:
        return not self.raw.strip()

    @property
    def is_comment_only(self) -> bool:
        return bool(self.raw.strip()) and not self.code.strip()


def _strip_comments_and_strings(source: str, language: str) -> list[_Line]:
    """
    Blank out comment and string content while preserving line structure.

    String *bodies* are replaced with spaces but the quotes are kept, so a
    pattern can still tell that an argument was a literal without matching on
    its contents. This is what stops `// TODO: remove hardcoded password` from
    being reported as a credential.
    """
    hash_comments = language in HASH_COMMENT_LANGS
    lines: list[_Line] = []
    in_block: str | None = None
    in_string: str | None = None

    for number, raw in enumerate(source.splitlines(), 1):
        out: list[str] = []
        i = 0
        length = len(raw)

        while i < length:
            rest = raw[i:]

            if in_block is not None:
                closer = BLOCK_COMMENT_CLOSE[in_block]
                if rest.startswith(closer):
                    out.append(" " * len(closer))
                    i += len(closer)
                    in_block = None
                else:
                    out.append(" ")
                    i += 1
                continue

            if in_string is not None:
                if rest.startswith("\\"):
                    out.append("  ")
                    i += 2
                    continue
                if rest.startswith(in_string):
                    out.append(in_string)
                    i += len(in_string)
                    in_string = None
                else:
                    out.append(" ")
                    i += 1
                continue

            # Block comment start
            opener = next(
                (o for o in BLOCK_COMMENT_OPEN if rest.startswith(o) and (o not in ('"""', "'''") or hash_comments)),
                None,
            )
            if opener:
                in_block = opener
                out.append(" " * len(opener))
                i += len(opener)
                continue

            # Line comment — blank the remainder
            comment = next(
                (c for c in LINE_COMMENTS if rest.startswith(c) and (c != "#" or hash_comments)),
                None,
            )
            if comment:
                out.append(" " * (length - i))
                break

            # String start
            quote = next((q for q in ('"', "'", "`") if rest.startswith(q)), None)
            if quote:
                in_string = quote
                out.append(quote)
                i += 1
                continue

            out.append(raw[i])
            i += 1

        lines.append(_Line(number=number, raw=raw, code="".join(out)))

    return lines


# ── Patterns, matched against comment-free, string-free code ──────────────────

#: (rule_id, title, severity, pattern, message, remediation, cwe, confidence)
GENERIC_RULES: list[tuple[str, str, Severity, re.Pattern[str], str, str, str, float]] = [
    (
        "JS-SEC-EVAL",
        "`eval()` on runtime input",
        Severity.CRITICAL,
        re.compile(r"\beval\s*\("),
        "`eval` executes its argument as code, so whoever controls that string "
        "controls this program.",
        "Use `JSON.parse` for data, or a lookup object for behaviour.",
        "CWE-95",
        0.8,
    ),
    (
        "JS-SEC-FUNCTION-CTOR",
        "`new Function()` constructor",
        Severity.HIGH,
        re.compile(r"\bnew\s+Function\s*\("),
        "The Function constructor compiles a string into executable code, with the "
        "same consequences as `eval`.",
        "Replace with a closure or a dispatch table.",
        "CWE-95",
        0.85,
    ),
    (
        "JS-SEC-INNERHTML",
        "Assignment to `innerHTML`",
        Severity.HIGH,
        re.compile(r"\.innerHTML\s*="),
        "Assigning to `innerHTML` parses the value as HTML, so a value containing a "
        "`<script>` tag or an `onerror` attribute executes.",
        "Use `.textContent` for text. If HTML is genuinely required, sanitise with "
        "DOMPurify first.",
        "CWE-79",
        0.8,
    ),
    (
        "JS-SEC-DANGEROUSHTML",
        "`dangerouslySetInnerHTML`",
        Severity.HIGH,
        re.compile(r"dangerouslySetInnerHTML"),
        "React escapes output by default; this prop turns that off for the value.",
        "Sanitise with DOMPurify before passing it, or render the value as text.",
        "CWE-79",
        0.75,
    ),
    (
        "JS-SEC-CHILDPROC",
        "Shell execution",
        Severity.HIGH,
        # `exec` alone matches `RegExp.prototype.exec`, which is what
        # `/language-(\w+)/.exec(className)` is — found by running this
        # analyzer over this repo's own frontend, where it fired in four
        # components. The receiver has to look like a process module, not a
        # regex literal. `_CHILD_PROCESS_ALIAS` below covers `const cp =
        # require("child_process")`, since the local name is arbitrary.
        re.compile(
            r"\bchild_process\s*\.\s*exec(Sync|File)?\s*\("
            r"|\bexecSync\s*\("
            r"|\bshelljs\b"
        ),
        "`exec` runs its argument through a shell, so interpolated values can chain "
        "extra commands with `;` or `&&`.",
        "Use `execFile`/`spawn` with an argument array, which never involves a shell.",
        "CWE-78",
        0.7,
    ),
    (
        "GO-SEC-EXEC",
        "Shell execution via `sh -c`",
        Severity.HIGH,
        re.compile(r"exec\.Command\s*\(\s*[\"'](?:/bin/)?(?:ba)?sh[\"']\s*,\s*[\"']-c[\"']"),
        "Invoking `sh -c` with a built string reintroduces shell parsing.",
        "Call the binary directly: `exec.Command(\"tar\", \"-czf\", dst, src)`.",
        "CWE-78",
        0.85,
    ),
    (
        "GEN-SEC-TLS-OFF",
        "TLS verification disabled",
        Severity.HIGH,
        re.compile(
            r"InsecureSkipVerify\s*:\s*true"
            r"|rejectUnauthorized\s*:\s*false"
            r"|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*[\"']?0"
            r"|verify\s*=\s*False"
        ),
        "Certificate checking is turned off, so any machine on the network path can "
        "impersonate the server and read the traffic.",
        "Remove the override. For a private CA, install its root certificate instead.",
        "CWE-295",
        0.9,
    ),
    (
        "GEN-SEC-WEAKHASH",
        "Weak hash algorithm",
        Severity.MEDIUM,
        re.compile(r"\b(md5|sha1)\s*\(|createHash\s*\(\s*[\"'](md5|sha1)[\"']", re.IGNORECASE),
        "MD5 and SHA-1 are collision-broken and unsuitable for signatures, integrity "
        "checks, or password storage.",
        "Use SHA-256 for integrity, bcrypt or argon2 for passwords.",
        "CWE-327",
        0.7,
    ),
    (
        "GEN-QUAL-EMPTY-CATCH",
        "Empty catch block",
        Severity.HIGH,
        re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}"),
        "This catch discards the error, so the failure leaves no trace and execution "
        "continues as though it had succeeded.",
        "Log it, rethrow it, or handle it. An empty catch is a silent bug factory.",
        "CWE-390",
        0.95,
    ),
    (
        "GO-QUAL-IGNORED-ERR",
        "Ignored error return",
        Severity.MEDIUM,
        re.compile(r"^\s*_\s*,\s*_\s*(:?=)|^\s*_\s*(:?=)\s*\w+\.\w+\("),
        "The error return is discarded, so a failure here is invisible.",
        "Check the error and return or log it.",
        "CWE-390",
        0.55,
    ),
]

#: `const cp = require("child_process")` / `import cp from "child_process"`.
#: The local alias is arbitrary, so the import is what identifies it. Without
#: this, aliased shell execution goes unreported; with a bare `\w+\.exec\(`
#: pattern instead, every regex `.exec()` call is a false positive.
_CHILD_PROCESS_ALIAS = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"]child_process['"]"""
    r"""|import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+['"](?:node:)?child_process['"]"""
)


def _child_process_aliases(source: str) -> set[str]:
    """Local names bound to the child_process module in this file."""
    return {
        name
        for match in _CHILD_PROCESS_ALIAS.finditer(source)
        for name in match.groups()
        if name
    }


#: Secret detection runs separately because it needs the string body, which
#: `_strip_comments_and_strings` deliberately removes.
SECRET_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|token|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|auth[_-]?token)\b\s*[:=]\s*[\"'`]([^\"'`\n]{8,})[\"'`]"
)

PLACEHOLDER_VALUE = re.compile(
    r"(?i)^\s*(none|null|changeme|placeholder|dummy|example|sample|test|fake|xxx+|\.\.\."
    r"|your[-_ ].*|<.*>|\{\{.*\}\}|\$\{.*\}|%[sd]|sk-(test|ci|xxx).*"
    r"|process\.env\..*|import\.meta\.env\..*|os\.getenv.*)\s*$"
)

#: High-entropy literals that look like real keys regardless of the variable name.
KNOWN_KEY_SHAPES = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "GitHub fine-grained token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "OpenAI-style API key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "Google API key"),
]

FUNCTION_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
    r"(?:function\s+(?P<fn>\w+)"
    r"|func\s+(?:\([^)]*\)\s*)?(?P<go>\w+)\s*\("
    r"|fn\s+(?P<rs>\w+)"
    r"|(?:const|let|var)\s+(?P<arrow>\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]+)?=>"
    r"|(?P<method>\w+)\s*\([^)]*\)\s*(?::\s*[\w<>\[\],\s|]+\s*)?\{)"
)

CLASS_DECL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")

#: Keywords that add a branch, for the language-agnostic complexity estimate.
BRANCH_TOKENS = re.compile(
    r"\b(if|else\s+if|elif|for|while|case|catch|except|&&|\|\||\?\?|\?[^.:]|switch)\b|\?\s*[^.:]"
)


def _estimate_complexity(code_lines: list[str]) -> int:
    return 1 + sum(len(BRANCH_TOKENS.findall(line)) for line in code_lines)


def _find_functions(lines: list[_Line], language: str) -> list[FunctionMetrics]:
    """
    Locate function extents using brace balance (or indentation for the rest).

    Brace counting is exact for C-family languages once strings and comments
    are blanked, which is precisely what `_strip_comments_and_strings` gives us.
    """
    functions: list[FunctionMetrics] = []
    brace_lang = language in BRACE_LANGS

    for index, line in enumerate(lines):
        match = FUNCTION_DECL.match(line.code)
        if not match:
            continue
        name = next((g for g in match.groupdict().values() if g), "") or "<anonymous>"
        # Skip control keywords that look like calls: `if (...) {`
        if name in ("if", "for", "while", "switch", "catch", "return", "function"):
            continue

        start = line.number
        end = start
        if brace_lang:
            depth = 0
            seen_open = False
            for probe in lines[index:]:
                depth += probe.code.count("{") - probe.code.count("}")
                if "{" in probe.code:
                    seen_open = True
                end = probe.number
                if seen_open and depth <= 0:
                    break
        else:
            base_indent = len(line.raw) - len(line.raw.lstrip())
            for probe in lines[index + 1:]:
                if probe.is_blank or probe.is_comment_only:
                    continue
                if len(probe.raw) - len(probe.raw.lstrip()) <= base_indent:
                    break
                end = probe.number

        body = [l.code for l in lines[index:end]]
        depth_estimate = 0
        current = 0
        for probe in body:
            current += probe.count("{") - probe.count("}")
            depth_estimate = max(depth_estimate, current)

        functions.append(
            FunctionMetrics(
                name=name,
                line=start,
                end_line=end,
                complexity=_estimate_complexity(body),
                length=end - start + 1,
                params=line.code.count(",") + 1 if "()" not in line.code else 0,
                max_depth=depth_estimate,
                has_docstring=index > 0 and lines[index - 1].is_comment_only,
            )
        )

    return functions


def analyze_generic(source: str, file_name: str = "", language: str = "") -> FileAnalysis:
    """Analyse a non-Python file using comment/string-aware scanning."""
    lines = _strip_comments_and_strings(source, language)

    blank = sum(1 for l in lines if l.is_blank)
    comment = sum(1 for l in lines if l.is_comment_only)
    code = len(lines) - blank - comment

    analysis = FileAnalysis(
        file_name=file_name,
        language=language or "unknown",
        total_lines=len(lines),
        code_lines=code,
        comment_lines=comment,
        blank_lines=blank,
    )

    # An aliased child_process import makes `cp.exec(...)` a shell call, but
    # only in a file that actually imports it — which is what keeps
    # `regex.exec(...)` from matching.
    aliases = _child_process_aliases(source)
    alias_exec = (
        re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\s*\.\s*exec(Sync|File)?\s*\(")
        if aliases else None
    )

    for line in lines:
        if not line.code.strip():
            continue
        if alias_exec and alias_exec.search(line.code):
            analysis.findings.append(
                Finding(
                    rule_id="JS-SEC-CHILDPROC",
                    title="Shell execution",
                    severity=Severity.HIGH,
                    line=line.number,
                    message="`exec` runs its argument through a shell, so interpolated values "
                    "can chain extra commands with `;` or `&&`.",
                    evidence=line.raw.strip()[:200],
                    confidence=0.8,
                    remediation="Use `execFile`/`spawn` with an argument array, which never "
                    "involves a shell.",
                    cwe="CWE-78",
                )
            )
        for rule_id, title, severity, pattern, message, remediation, cwe, confidence in GENERIC_RULES:
            if pattern.search(line.code):
                analysis.findings.append(
                    Finding(
                        rule_id=rule_id,
                        title=title,
                        severity=severity,
                        line=line.number,
                        message=message,
                        evidence=line.raw.strip()[:200],
                        confidence=confidence,
                        remediation=remediation,
                        cwe=cwe,
                    )
                )

    # Secrets need the raw line (the value was blanked in `code`), but we only
    # look at lines that were not entirely a comment.
    for line in lines:
        if line.is_comment_only or line.is_blank:
            continue
        match = SECRET_ASSIGN.search(line.raw)
        if match and not PLACEHOLDER_VALUE.match(match.group(2)):
            analysis.findings.append(
                Finding(
                    rule_id="GEN-SEC-HARDCODED",
                    title="Hardcoded credential",
                    severity=Severity.CRITICAL,
                    line=line.number,
                    message=f"`{match.group(1)}` is assigned a literal value. It is in the "
                    "repository history from now on, so rotating it is the only real fix.",
                    evidence=line.raw.strip()[:80],
                    confidence=0.75,
                    remediation="Load it from the environment and add the value to your "
                    "secret manager. Then rotate the exposed credential.",
                    cwe="CWE-798",
                )
            )
            continue

        for shape, description in KNOWN_KEY_SHAPES:
            if shape.search(line.raw):
                analysis.findings.append(
                    Finding(
                        rule_id="GEN-SEC-KEYSHAPE",
                        title=f"Exposed {description}",
                        severity=Severity.CRITICAL,
                        line=line.number,
                        message=f"This matches the exact format of a {description}. Credential "
                        "scanners run against public repositories continuously; assume it is "
                        "already compromised.",
                        evidence=line.raw.strip()[:60],
                        confidence=0.95,
                        remediation="Revoke this credential now, then load the replacement "
                        "from the environment.",
                        cwe="CWE-798",
                    )
                )
                break

    # ── Structural pass: what the regexes provably cannot reach ───────────────
    #
    # Every rule above matched against `line.code`, which has string *contents*
    # blanked. That is correct for the great majority of patterns, but it makes
    # two of them unreachable, because their trigger IS a string literal:
    #
    #   createHash('md5')                          → becomes createHash('   ')
    #   NODE_TLS_REJECT_UNAUTHORIZED = '0'         → becomes ... = ' '
    #
    # Loosening those regexes to match the raw line would trade a silent miss for
    # false positives on comments and prose. Instead they are detected from the
    # parse tree, which can tell an argument from a mention. See js_analyzer.py.
    #
    # Findings are merged by (rule_id, line): the unquoted `= 0` form already
    # matches the regex, and one problem must be reported once.
    for finding in detect_structural_issues(source, file_name, language):
        already_reported = any(
            f.rule_id == finding.rule_id and f.line == finding.line
            for f in analysis.findings
        )
        if not already_reported:
            analysis.findings.append(finding)

    analysis.functions = _find_functions(lines, language)

    for fn in analysis.functions:
        if fn.complexity > 20:
            analysis.findings.append(
                Finding(
                    rule_id="GEN-QUAL-COMPLEXITY",
                    title=f"`{fn.name}` is very complex",
                    severity=Severity.HIGH,
                    line=fn.line,
                    message=f"Roughly {fn.complexity} branch points across {fn.length} lines.",
                    evidence=f"{fn.name}(...)",
                    confidence=0.7,
                    remediation="Extract the branches into named helpers.",
                )
            )
        elif fn.complexity > 12:
            analysis.findings.append(
                Finding(
                    rule_id="GEN-QUAL-COMPLEXITY",
                    title=f"`{fn.name}` is complex",
                    severity=Severity.MEDIUM,
                    line=fn.line,
                    message=f"Roughly {fn.complexity} branch points.",
                    evidence=f"{fn.name}(...)",
                    confidence=0.6,
                    remediation="Split the conditional logic into helpers.",
                )
            )

    return analysis
