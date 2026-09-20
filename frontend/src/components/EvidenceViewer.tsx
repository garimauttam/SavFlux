/**
 * EvidenceViewer.tsx — Shows a file with the cited lines highlighted and scrolled into view.
 *
 * This is the payoff for line-precise citations. A citation that says
 * `retrieval_service.py:612-640` is only useful if one click puts the reader on
 * line 612 with the span marked. Opening the file at the top and asking them to
 * scroll is not verification — it is homework.
 *
 * DESIGN NOTES
 * - Line numbers come from the backend's chunk spans, which are 1-indexed and
 *   inclusive on both ends (matching how editors and GitHub display them).
 * - Content is fetched from the existing /write/file-content endpoint, which
 *   reconstructs the file from ChromaDB chunks. No new backend route needed.
 * - Rendering is a plain <pre> grid rather than a syntax highlighter: a 5000-line
 *   file highlighted per-line stalls the main thread, and the job here is to show
 *   *which lines*, not to be an editor.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, FileCode, Loader2, X } from "lucide-react";
import { apiFetch } from "../api";
import { isLineInRanges, parseLineRanges } from "../lib/openFile";

interface EvidenceViewerProps {
  /** Stable source id of the file to display. */
  source: string;
  /** Display name for the header. */
  fileName?: string;
  /** 1-indexed inclusive span to highlight, if any. */
  startLine?: number;
  endLine?: number;
  /** Exact cited regions as "1-30,88-92" when the evidence is discontinuous. */
  lineRanges?: string;
  onClose: () => void;
}

/** Lines of context to show above the highlighted span when scrolling to it. */
const CONTEXT_LINES = 8;

export function EvidenceViewer({
  source,
  fileName,
  startLine,
  endLine,
  lineRanges,
  onClose,
}: EvidenceViewerProps) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const highlightRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setContent(null);

    apiFetch(`/api/v1/write/file-content?source=${encodeURIComponent(source)}`)
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          throw new Error(
            res.status === 404
              ? "File is no longer indexed — re-index the repo to view it."
              : `Could not load file (HTTP ${res.status}).`,
          );
        }
        const data = await res.json();
        if (!cancelled) setContent(data.content ?? "");
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load file.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [source]);

  const lines = useMemo(() => (content ? content.split("\n") : []), [content]);

  // Scroll the highlighted span into view once the content is painted.
  // `block: "center"` keeps surrounding context visible, which matters when the
  // reader is checking whether the cited lines really support the claim.
  useEffect(() => {
    if (!content || typeof startLine !== "number") return;
    const frame = requestAnimationFrame(() => {
      highlightRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [content, startLine]);

  const from = typeof startLine === "number" ? startLine : null;
  const to = typeof endLine === "number" ? endLine : from;

  // Prefer the exact regions when the backend supplied them; otherwise treat
  // the plain span as a single range. Falling back this way means older
  // indexes (written before line_ranges existed) still highlight correctly.
  const highlighted = useMemo(() => {
    const parsed = parseLineRanges(lineRanges);
    if (parsed.length > 0) return parsed;
    return from !== null && to !== null ? [{ start: from, end: to }] : [];
  }, [lineRanges, from, to]);

  const spanLabel =
    from === null ? "" : to !== null && to !== from ? `:${from}-${to}` : `:${from}`;

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-xl border border-purple-500/30 bg-gray-950">
      <div className="flex items-center gap-2 border-b border-gray-800 bg-gray-900 px-3 py-2">
        <FileCode className="h-3.5 w-3.5 shrink-0 text-purple-400" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-gray-200" title={source}>
          {fileName || source.split("::").pop()?.split("/").pop() || source}
          <span className="text-purple-300">{spanLabel}</span>
        </span>
        {from !== null && (
          <span className="shrink-0 rounded-full border border-purple-500/30 bg-purple-500/10 px-2 py-0.5 text-[10px] text-purple-200">
            cited evidence
          </span>
        )}
        <button
          onClick={onClose}
          title="Close"
          className="rounded p-1 text-gray-500 transition-colors hover:text-white"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {loading && (
          <p className="flex items-center gap-2 p-4 text-xs text-gray-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading file…
          </p>
        )}

        {error && (
          <p className="flex items-start gap-2 p-4 text-xs text-amber-300">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {error}
          </p>
        )}

        {content !== null && !error && (
          <pre className="p-0 font-mono text-[11px] leading-relaxed">
            {lines.map((line, index) => {
              const lineNumber = index + 1;
              const isHighlighted = isLineInRanges(lineNumber, highlighted);
              // Anchor the scroll a little above the span so the reader sees
              // the lines leading into the evidence, not just the evidence.
              const isScrollAnchor = from !== null && lineNumber === Math.max(1, from - CONTEXT_LINES);

              return (
                <div
                  key={lineNumber}
                  ref={isScrollAnchor ? highlightRef : undefined}
                  className={`flex ${
                    isHighlighted
                      ? "border-l-2 border-purple-400 bg-purple-500/15"
                      : "border-l-2 border-transparent"
                  }`}
                >
                  <span
                    className={`w-14 shrink-0 select-none px-2 text-right ${
                      isHighlighted ? "text-purple-300" : "text-gray-600"
                    }`}
                  >
                    {lineNumber}
                  </span>
                  <code
                    className={`whitespace-pre-wrap break-all px-2 ${
                      isHighlighted ? "text-gray-100" : "text-gray-400"
                    }`}
                  >
                    {line || " "}
                  </code>
                </div>
              );
            })}
          </pre>
        )}
      </div>
    </div>
  );
}

export default EvidenceViewer;
