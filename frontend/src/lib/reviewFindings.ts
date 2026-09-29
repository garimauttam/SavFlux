/** Anchors are emitted by the analyzer, never guessed from markdown headings. */
export interface ReviewFinding {
  ruleId: string;
  title: string;
  severity: string;
  line: number;
  endLine?: number;
  message: string;
  remediation: string;
  evidence: string;
  confidence: number;
  origin: "static" | "model";
  replacement?: string;
}
export interface ReviewAnalysis {
  contentHash: string;
  parseError: string;
  findingCount: number;
}
export function decodeFinding(
  meta: Record<string, unknown>,
): ReviewFinding | null {
  if (
    (meta.origin !== "static" && meta.origin !== "model") ||
    typeof meta.line !== "number" ||
    !Number.isInteger(meta.line) ||
    meta.line < 1 ||
    typeof meta.rule_id !== "string" ||
    typeof meta.title !== "string"
  )
    return null;
  return {
    ruleId: meta.rule_id,
    title: meta.title,
    severity: typeof meta.severity === "string" ? meta.severity : "info",
    line: meta.line,
    endLine:
      typeof meta.end_line === "number" && meta.end_line >= meta.line
        ? meta.end_line
        : undefined,
    message: typeof meta.message === "string" ? meta.message : "",
    remediation: typeof meta.remediation === "string" ? meta.remediation : "",
    evidence: typeof meta.evidence === "string" ? meta.evidence : "",
    confidence: typeof meta.confidence === "number" ? meta.confidence : 0,
    origin: meta.origin,
    replacement:
      typeof meta.replacement === "string" ? meta.replacement : undefined,
  };
}
export function findingKey(finding: ReviewFinding): string {
  return `${finding.ruleId}:${finding.line}:${finding.title}`;
}
export function reviewPath(source: string): string {
  return source.includes("::")
    ? source.split("::").slice(1).join("::")
    : source;
}
export type CodeRow =
  { kind: "line"; line: number } | { kind: "gap"; start: number; end: number };
export function codeRows(
  count: number,
  findings: ReviewFinding[],
  expanded: Set<string>,
  all = false,
): CodeRow[] {
  // Keep small files intact; folding a one-line gap breaks useful context.
  if (count <= 60) all = true;
  const visible = new Set<number>();
  if (!all) {
    for (let n = 1; n <= Math.min(count, findings.length ? 4 : 60); n++)
      visible.add(n);
    for (const finding of findings) {
      for (
        let n = Math.max(1, finding.line - 4);
        n <= Math.min(count, (finding.endLine ?? finding.line) + 4);
        n++
      )
        visible.add(n);
    }
  }
  const rows: CodeRow[] = [];
  for (let n = 1; n <= count;) {
    if (all || visible.has(n)) {
      rows.push({ kind: "line", line: n++ });
      continue;
    }
    const start = n;
    while (n <= count && !visible.has(n)) n++;
    const end = n - 1;
    if (end - start < 3 || expanded.has(`${start}:${end}`)) {
      for (let line = start; line <= end; line++)
        rows.push({ kind: "line", line });
    } else rows.push({ kind: "gap", start, end });
  }
  return rows;
}
