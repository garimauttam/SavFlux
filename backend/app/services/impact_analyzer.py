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
