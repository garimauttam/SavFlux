"""Deterministic PR impact and security analysis."""

import re
from collections import defaultdict


_FILE_RE = re.compile(r"^(?:diff --git a/[^ ]+ b/|\+\+\+ b/)([^\s]+)", re.MULTILINE)
_SYMBOL_RE = re.compile(
    r"^\+\s*(?:async\s+def|def|class|function|const|export\s+function)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_SECURITY_PATTERNS = (
    ("possible-secret", re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]+['\"]")),
    ("sql-concatenation", re.compile(r"(?i)(select|insert|update|delete).*(\+|f['\"]|format\()")),
    ("shell-execution", re.compile(r"(?i)(subprocess\.(run|Popen|call)|os\.system|child_process\.exec)")),
    ("unsafe-deserialization", re.compile(r"(?i)(pickle\.loads?|yaml\.load\s*\()")),
)


def _changed_files(diff: str) -> list[str]:
    files = []
    for match in _FILE_RE.finditer(diff):
        path = match.group(1)
        if path != "/dev/null" and path not in files:
            files.append(path)
    return files


def analyze_diff(diff: str, graph: dict | None = None) -> dict:
    """Return explainable impact, risk, and security metadata for a unified diff."""
    files = _changed_files(diff)
    added_lines = "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    symbols = list(dict.fromkeys(_SYMBOL_RE.findall(
        "\n".join(f"+{line}" for line in added_lines.splitlines())
    )))
    flags = []
    for name, pattern in _SECURITY_PATTERNS:
        matches = pattern.findall(added_lines)
        if matches:
            flags.append({"rule": name, "matches": len(matches)})

    impacted: set[str] = set()
    reasons: list[str] = []
    if graph:
        nodes = {node.get("id", ""): node for node in graph.get("nodes", [])}
        path_to_ids = defaultdict(list)
        for node_id, node in nodes.items():
            path_to_ids[node.get("label", "")].append(node_id)
            path_to_ids[node_id].append(node_id)

        changed_ids = set()
        for path in files:
            basename = path.rsplit("/", 1)[-1]
            changed_ids.update(path_to_ids.get(path, []))
            changed_ids.update(path_to_ids.get(basename, []))

        for edge in graph.get("edges", []):
            if edge.get("target") in changed_ids:
                impacted.add(edge.get("source", ""))
        impacted.difference_update(changed_ids)
        if impacted:
            reasons.append(f"{len(impacted)} indexed dependents import changed files")

    risk_score = min(10, len(files) + len(impacted))
    if flags:
        risk_score = min(10, risk_score + 3)
        reasons.append(f"{len(flags)} security pattern(s) detected in added lines")
    if any(name in " ".join(files).lower() for name in ("auth", "security", "permission", "credential")):
        risk_score = min(10, risk_score + 2)
        reasons.append("security-sensitive file changed")

    suggested_tests = [f"Add or update tests for {path}" for path in files[:5]]
    suggested_tests.extend(f"Run regression tests for dependent file {path}" for path in sorted(impacted)[:5])

    return {
        "changed_files": files,
        "changed_symbols": symbols,
        "impacted_files": sorted(impacted),
        "security_flags": flags,
        "risk_score": risk_score,
        "risk_reasons": reasons,
        "suggested_tests": suggested_tests,
        "graph_available": graph is not None,
    }


# ── Inline PR comments ─────────────────────────────────────────────────────────
# Line-level rules evaluated against ADDED lines only (context/removal lines
# are not actionable for a reviewer). Each rule: (id, severity, message, regex).
# Severity guides the UI chip colour: high=red, medium=amber, low=gray.
_LINE_RULES = (
    ("possible-secret", "high", "Possible hardcoded secret — move to env vars or a secret store.",
     re.compile(r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['\"][^'\"]{3,}['\"]")),
    ("eval-exec", "high", "Dynamic code execution (eval/exec) — high injection risk; use a safe alternative.",
     re.compile(r"(?i)\b(eval|exec)\s*\(")),
    ("shell-execution", "medium", "Shell execution from code — validate/escape all interpolated input.",
     re.compile(r"(?i)(subprocess\.(run|Popen|call)|os\.system|child_process\.exec|shell\s*=\s*True)")),
    ("unsafe-deserialization", "medium", "Unsafe deserialization — untrusted input can lead to RCE.",
     re.compile(r"(?i)(pickle\.loads?|yaml\.load\s*\(|Function\s*\()")),
    ("sql-concatenation", "medium", "Possible SQL string building — prefer parameterized queries.",
     re.compile(r"(?i)(select|insert|update|delete)\b.*(\+|f['\"]|\.format\()")),
    ("broad-except", "low", "Bare/broad except swallows errors — catch specific exceptions.",
     re.compile(r"^\s*except\s*(Exception|BaseException)?\s*:\s*(pass\s*)?$")),
    ("debug-print", "low", "Debug output left in code — remove print/console.log before merging.",
     re.compile(r"^\s*(print\s*\(|console\.(log|debug|info)\s*\()")),
    ("todo-fixme", "low", "TODO/FIXME added — fine for a draft, but track it before merging.",
     re.compile(r"(?i)\b(TODO|FIXME|HACK|XXX)\b")),
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_MAX_INLINE = 30


def inline_comments_for_diff(diff: str, max_comments: int = _MAX_INLINE) -> list[dict]:
    """Map added diff lines to new-file line numbers and lint each one.

    Returns GitHub-style inline comments:
      [{path, line, severity, rule, message, code}]
    sorted by (path, line), capped at max_comments.
    """
    comments: list[dict] = []
    path = ""
    new_line = 0

    for raw in (diff or "").splitlines():
        if raw.startswith("+++ b/"):
            path = raw[len("+++ b/"):]
            continue
        if raw.startswith("+++ ") or raw.startswith("--- "):
            continue
        if raw.startswith("diff --git"):
            path = ""
            continue
        m = _HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            code = raw[1:]
            for rule, severity, message, pattern in _LINE_RULES:
                if pattern.search(code):
                    comments.append({
                        "path": path or "(unknown file)",
                        "line": new_line,
                        "severity": severity,
                        "rule": rule,
                        "message": message,
                        "code": code.strip()[:200],
                    })
                    break  # one comment per line — highest-priority rule wins
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue  # removals don't advance the new-file line counter
        else:
            # Context line (or "\ No newline" marker — doesn't consume a line)
            if raw.startswith("\\"):
                continue
            new_line += 1

    comments.sort(key=lambda c: (c["path"], c["line"]))
    return comments[: max(1, max_comments)]
