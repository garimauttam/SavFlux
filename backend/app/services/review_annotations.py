"""Line comments from this run's analyzer; never persisted or inferred from prose."""
import hashlib
from app.services.stream_protocol import status_event
from app.services.code_analysis.models import FileAnalysis


def annotation_events(source: str, content: str, analysis: FileAnalysis):
    # One finding per event avoids silently truncating lists at the protocol cap.
    yield status_event(
        "Static findings ready", step="analysis", id=source,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        parse_error=analysis.parse_error, finding_count=len(analysis.findings),
    )
    for finding in analysis.sorted_findings():
        yield status_event(
            finding.message[:600], step="finding", id=source,
            rule_id=finding.rule_id, title=finding.title[:160],
            severity=finding.severity.value, line=finding.line,
            end_line=finding.end_line, evidence=finding.evidence[:200],
            remediation=finding.remediation[:400], confidence=finding.confidence,
            origin="static", cwe=finding.cwe,
        )


INLINE_COMMENT_INSTRUCTIONS = '''
For actionable issues, append a fenced savflux-comments JSON block at the END of
THIS FILE's review (inside its FILE/END FILE block when reviewing a batch).
Use {"comments": [{"line": 1, "end_line": 1, "title": "Short issue title",
"reason": "Why the code is a problem", "suggestion": "Concrete remediation",
"quote": "EXACT source lines, including indentation", "severity": "medium"}]}.
Line numbers are one-based within that file. Quote must equal the complete source
lines at line..end_line, not a substring. Never guess a line or invent evidence.
If parsing failed, report the file as not rated rather than giving a health score.
Include at most 3 comments and only issues supported by the supplied code. An
optional "replacement" string is the proposed code replacing those quoted lines;
keep it under 300 characters. Do not include replacement unless it is a concrete
edit. No issues means {"comments": []}. Keep the normal readable review too.
'''


def model_annotation_events(source: str, content: str, review: str):
    """Return (readable report, bounded events) for source-verified model comments.

    Matching a quote validates location, NOT the model's claim or proposed fix.
    Invalid/missing quotes stay file-level notes. No generated edit is executed.
    """
    import json
    import re

    lines = content.splitlines()
    events = []

    def replace_block(match):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            return match.group(0)
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            return match.group(0)
        comments = payload["comments"]
        # Keep oversized output at file level, rather than silently losing items.
        if len(comments) > 12:
            return match.group(0)
        notes = []
        for index, item in enumerate(comments):
            if not isinstance(item, dict):
                notes.append("Unanchored model note: " + str(item))
                continue
            line, end = item.get("line"), item.get("end_line", item.get("line"))
            title, reason, suggestion, quote = (item.get(key) for key in ("title", "reason", "suggestion", "quote"))
            valid = (
                type(line) is int and type(end) is int and 1 <= line <= end <= len(lines)
                and all(isinstance(value, str) and value.strip() for value in (title, reason, suggestion, quote))
                and quote.rstrip("\n") == "\n".join(lines[line - 1:end])
            )
            if not valid:
                notes.append(f"### Unanchored model comment\n{title or 'Comment'}\n\n{reason or ''}\n\nSuggested change: {suggestion or ''}")
                continue
            fields = dict(
                step="finding", id=source, origin="model", rule_id=f"model-{line}-{index}",
                line=line, end_line=end, title=title[:160],
                severity=item.get("severity") if item.get("severity") in ("critical", "high", "medium", "low", "info") else "info",
                remediation=suggestion[:400], evidence=quote[:200],
            )
            replacement = item.get("replacement")
            # Do not render a truncated replacement as if it were complete code.
            if isinstance(replacement, str) and len(replacement) <= 300:
                fields["replacement"] = replacement
            events.append(status_event(reason[:600], **fields))
            notes.append(f"### {title}\nLine {line}{f'–{end}' if end != line else ''}: {reason}\n\nSuggested change: {suggestion}")
        return "\n\n".join(notes)

    report = re.sub(r"```savflux-comments\s*\n(.*?)\n```", replace_block, review, flags=re.DOTALL)
    return report, events
