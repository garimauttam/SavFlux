/**
 * openFile.ts — The one way to say "show me this code" across the app.
 *
 * WHY AN EVENT BUS
 * Citations live deep inside the chat tree (ChatWindow → MessageBubble →
 * TrustLedgerDrawer), while the Review panel lives in a sibling branch under
 * App. Threading an `onOpenInReview` callback down four levels means every
 * intermediate component takes a prop it does not use — and any component that
 * forgets to forward it silently disables the button. (That is exactly what had
 * happened: MessageBubble accepted `onOpenInReview`, ChatWindow never passed
 * it, so the "Open in Code Review" button never rendered at all.)
 *
 * A single typed event keeps the contract in one file: any component can raise
 * `openFileAt(...)`, and App is the only listener.
 *
 * WHY THE LINE NUMBER MATTERS
 * Opening `retrieval_service.py` at the top is not evidence — the file is 766
 * lines long. Carrying `startLine`/`endLine` is what makes a citation
 * verifiable in one click: the reader lands on the exact span the answer used.
 */

export const OPEN_FILE_EVENT = "savflux:open-file";

export interface OpenFileDetail {
  /** Stable source id, e.g. "https://github.com/o/r::src/auth.py". */
  source: string;
  /** 1-indexed first line of the cited evidence, when known. */
  startLine?: number;
  /** 1-indexed last line of the cited evidence, when known. */
  endLine?: number;
  /**
   * Exact cited regions as "1-30,88-92", when the evidence is discontinuous.
   *
   * A module-level chunk gathers the import header AND constants declared
   * between functions. Highlighting `startLine..endLine` for that chunk marks
   * the entire file. These ranges let the viewer highlight only the lines the
   * retriever actually returned.
   */
  lineRanges?: string;
}

/** A parsed inclusive line range. */
export interface LineRange {
  start: number;
  end: number;
}

/**
 * Parse the "1-30,88-92" encoding into ranges, skipping malformed segments.
 *
 * Returns [] when nothing is parseable, so callers fall back to the plain
 * start/end span rather than highlighting nothing.
 */
export function parseLineRanges(encoded?: string | null): LineRange[] {
  if (!encoded) return [];
  const ranges: LineRange[] = [];
  for (const part of encoded.split(",")) {
    const segment = part.trim();
    if (!segment) continue;
    const [rawStart, rawEnd] = segment.includes("-")
      ? segment.split("-", 2)
      : [segment, segment];
    const start = Number.parseInt(rawStart, 10);
    const end = Number.parseInt(rawEnd, 10);
    if (Number.isFinite(start) && Number.isFinite(end) && start >= 1 && end >= start) {
      ranges.push({ start, end });
    }
  }
  return ranges;
}

/** True when `line` falls inside any of the given ranges. */
export function isLineInRanges(line: number, ranges: LineRange[]): boolean {
  return ranges.some((r) => line >= r.start && line <= r.end);
}

/**
 * Ask the app to open a file, optionally scrolled to a cited line range.
 *
 * Accepts a bare string for backwards compatibility with the existing
 * file-tree callers, which emit `detail: source`.
 */
export function openFileAt(detail: OpenFileDetail | string): void {
  const payload: OpenFileDetail =
    typeof detail === "string" ? { source: detail } : detail;
  if (!payload.source) return;
  window.dispatchEvent(new CustomEvent(OPEN_FILE_EVENT, { detail: payload }));
}

/**
 * Normalise an incoming event payload.
 *
 * Older emitters dispatch `detail` as a plain source string; newer ones send an
 * OpenFileDetail. Returning null for anything unusable keeps the listener from
 * navigating to an empty file.
 */
export function parseOpenFileDetail(detail: unknown): OpenFileDetail | null {
  if (typeof detail === "string") {
    return detail ? { source: detail } : null;
  }
  if (detail && typeof detail === "object") {
    const candidate = detail as Partial<OpenFileDetail>;
    if (typeof candidate.source === "string" && candidate.source) {
      return {
        source: candidate.source,
        startLine: typeof candidate.startLine === "number" ? candidate.startLine : undefined,
        endLine: typeof candidate.endLine === "number" ? candidate.endLine : undefined,
        lineRanges: typeof candidate.lineRanges === "string" ? candidate.lineRanges : undefined,
      };
    }
  }
  return null;
}
