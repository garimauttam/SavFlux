/**
 * ContextPanel.tsx — the right-hand surface: what the agent is pointing at.
 *
 * THE PROBLEM IT SOLVES
 * ---------------------
 * A citation said "auth/tokens.py:42-58" and the only way to see it was to
 * switch to the Review tab, wait for it to load a file list, pick the file, and
 * scroll. Five steps, and every one of them lost the thread you were reading.
 * The claim and its evidence lived in two different parts of the application.
 *
 * So the evidence lives next to the claim. Click a citation and the file opens
 * here, scrolled to the line, with the cited span marked. If an answer is
 * wrong, the thing that shows it is one glance away — which is the only reason
 * the rest of the product's honesty rules are worth anything.
 *
 * It is a drawer rather than a permanent column because on a laptop the
 * conversation needs the width, and a panel nobody opens should not cost
 * anyone 400 pixels.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FileCode,
  FolderTree,
  Loader2,
  PanelRightClose,
  Search,
  X,
} from "lucide-react";
import { apiFetch } from "../../api";
import { CodeHighlight } from "../../lib/highlight";
import {
  isLineInRanges, openFileAt, parseLineRanges, sourceFileName, type OpenFileDetail,
} from "../../lib/openFile";

interface ContextPanelProps {
  open: boolean;
  onClose: () => void;
  /** The file to show, or null for the indexed-file tree. */
  target: OpenFileDetail | null;
  onClearTarget: () => void;
  indexedFiles: { file_name: string; source: string; repo_url: string }[];
  repoUrl: string | null;
}

interface LoadedFile {
  content: string;
  language: string;
}

function languageOf(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    py: "python", js: "javascript", jsx: "jsx", ts: "typescript", tsx: "tsx",
    go: "go", rs: "rust", java: "java", rb: "ruby", c: "c", cpp: "cpp",
    sh: "bash", yml: "yaml", yaml: "yaml", json: "json", md: "markdown", sql: "sql",
  };
  return map[ext] ?? "text";
}

/** Contiguous runs of lines, each tagged with whether it is cited. */
interface LineRun {
  start: number;
  cited: boolean;
  text: string;
}

function buildRuns(lines: string[], ranges: { start: number; end: number }[]): LineRun[] {
  const runs: LineRun[] = [];
  lines.forEach((line, i) => {
    const n = i + 1;
    const cited = isLineInRanges(n, ranges);
    const last = runs[runs.length - 1];
    if (last && last.cited === cited) {
      last.text += `${line}\n`;
    } else {
      runs.push({ start: n, cited, text: `${line}\n` });
    }
  });
  return runs;
}

export function ContextPanel({
  open,
  onClose,
  target,
  onClearTarget,
  indexedFiles,
  repoUrl,
}: ContextPanelProps) {
  const [file, setFile] = useState<LoadedFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // The old reading order is deliberate: clear the previous file *first*, so
  // scrolling to line 42 can never happen while the reader is still looking at
  // the contents of a different file.
  useEffect(() => {
    if (!target?.source) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setFile(null);
    (async () => {
      try {
        const res = await apiFetch(
          `/api/v1/write/file-content?source=${encodeURIComponent(target.source)}`,
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail || `Could not load the file (HTTP ${res.status})`);
        }
        const data = await res.json();
        if (cancelled) return;
        setFile({ content: data.content ?? "", language: data.language || languageOf(target.source) });
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Could not load the file.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [target]);

  const ranges = useMemo(() => {
    const parsed = parseLineRanges(target?.lineRanges);
    if (parsed.length > 0) return parsed;
    if (typeof target?.startLine === "number") {
      return [{ start: target.startLine, end: target.endLine ?? target.startLine }];
    }
    return [];
  }, [target]);

  const lines = useMemo(() => (file ? file.content.replace(/\n$/, "").split("\n") : []), [file]);
  const runs = useMemo(() => buildRuns(lines, ranges), [lines, ranges]);
  const citedCount = ranges.reduce((n, r) => n + (r.end - r.start + 1), 0);

  const tree = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = indexedFiles
      .filter((f) => !repoUrl || f.repo_url === repoUrl)
      .filter((f) => (q ? f.file_name.toLowerCase().includes(q) : true));
    // Grouped by directory so the list reads like a project rather than a dump.
    const groups = new Map<string, typeof list>();
    for (const f of list) {
      const parts = f.file_name.split("/");
      const dir = parts.length > 1 ? parts.slice(0, -1).join("/") : "";
      const bucket = groups.get(dir) ?? [];
      bucket.push(f);
      groups.set(dir, bucket);
    }
    return Array.from(groups.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [indexedFiles, query, repoUrl]);

  const firstCitedLine = useMemo(
    () => runs.find((run) => run.cited)?.start ?? null,
    [runs],
  );

  // The citation is the only reason the file opened, so the cited code has to be
  // on screen. Without this the drawer lands on line 1 and a reader who clicked
  // "auth/tokens.py:42-58" is shown forty-one lines of context and the one line
  // they wanted sitting just below the fold — which is the same five-steps-to-
  // find-the-evidence problem this panel was built to remove.
  //
  // Runs are collapsed, so this scrolls to the block, not the line, and seats it
  // a third of the way down instead of flush against the header: the lines above
  // it are the context that makes the cited line legible, and a reader should be
  // able to see the block, what precedes it, and the header without scrolling.
  // Rect maths rather than `offsetTop`, which is relative to the nearest
  // positioned ancestor and would be some ancestor the component never sees.
  useEffect(() => {
    if (firstCitedLine === null) return;
    const container = scrollRef.current;
    const block = container?.querySelector<HTMLElement>(`[data-cited-start="${firstCitedLine}"]`);
    if (!container || !block) return;
    const delta = block.getBoundingClientRect().top - container.getBoundingClientRect().top;
    container.scrollTop = Math.max(0, container.scrollTop + delta - container.clientHeight / 3);
  }, [firstCitedLine, file]);

  if (!open) {
    // Nothing. The toggle lives in the top bar next to the other view
    // controls, rather than as a sliver floating on the right edge — a
    // 20px tab hanging off the middle of the window reads as a rendering
    // artefact, and it is invisible on every screen narrower than `xl`, which
    // is exactly when a reader most wants the evidence beside the answer.
    return null;
  }

  return (
    <aside
      className="sf-surface flex w-[min(440px,42vw)] shrink-0 flex-col border-l sf-line"
      aria-label="Context"
      data-testid="context-panel"
    >
      <div className="flex h-10 shrink-0 items-center gap-2 border-b sf-line px-3">
        {target ? (
          <>
            <button
              type="button"
              onClick={onClearTarget}
              className="sf-iconbtn h-6 w-6 shrink-0"
              title="Back to the file tree"
              aria-label="Back to the file tree"
            >
              <FolderTree className="h-3.5 w-3.5" />
            </button>
            <FileCode className="h-3.5 w-3.5 shrink-0 sf-mute" />
            <span className="sf-mono sf-text min-w-0 flex-1 truncate text-[12px]" title={target.source}>
              {sourceFileName(target.source)}
            </span>
            {typeof target.startLine === "number" && (
              <span className="sf-mute shrink-0 text-[11px]">
                L{target.startLine}
                {target.endLine && target.endLine !== target.startLine ? `–${target.endLine}` : ""}
              </span>
            )}
          </>
        ) : (
          <>
            <FolderTree className="h-3.5 w-3.5 shrink-0 sf-mute" />
            <span className="sf-text flex-1 text-[12.5px] font-medium">
              Indexed files
              <span className="sf-mute ml-1.5 font-normal">{indexedFiles.length}</span>
            </span>
          </>
        )}
        <button
          type="button"
          onClick={onClose}
          className="sf-iconbtn h-6 w-6"
          title="Close"
          aria-label="Close the context panel"
        >
          <PanelRightClose className="h-3.5 w-3.5" />
        </button>
      </div>

      {target ? (
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto" data-testid="context-file">
          {loading && (
            <p className="sf-mute flex items-center gap-2 p-4 text-[12.5px]">
              <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading…
            </p>
          )}
          {error && (
            <div className="p-4">
              <p className="rounded-lg border border-rose-500/25 bg-rose-500/10 px-3 py-2 text-[12px] text-rose-300">
                {error}
              </p>
              <p className="sf-mute mt-2 text-[11.5px] leading-relaxed">
                Files are reconstructed from the index, so one that was never ingested — or edited
                since it was — cannot be shown. Re-index the repository to refresh it.
              </p>
            </div>
          )}
          {file && (
            <div className="sf-mono text-[11.5px] leading-[1.55]">
              {runs.map((run) => (
                <div
                  key={run.start}
                  id={`line-${run.start}`}
                  data-cited-start={run.cited ? run.start : undefined}
                  className={run.cited ? "bg-amber-400/10" : ""}
                  style={run.cited ? { boxShadow: "inset 2px 0 0 var(--sf-warn)" } : undefined}
                >
                  <CodeHighlight
                    language={file.language}
                    customStyle={{
                      margin: 0,
                      borderRadius: 0,
                      fontSize: "11.5px",
                      lineHeight: 1.55,
                      background: "transparent",
                      display: "block",
                    }}
                  >
                    {run.text}
                  </CodeHighlight>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="border-b sf-line p-2">
            <div className="relative">
              <Search className="sf-mute pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter files…"
                aria-label="Filter files"
                className="sf-input pl-8"
              />
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto py-1">
            {tree.length === 0 && (
              <p className="sf-mute p-4 text-center text-[12px]">
                {indexedFiles.length === 0 ? "Nothing indexed yet." : `No file matches “${query}”.`}
              </p>
            )}
            {tree.map(([dir, files]) => (
              <div key={dir || "__root"}>
                {dir && (
                  <p className="sf-mute px-3 pb-1 pt-2 text-[10.5px] font-semibold uppercase tracking-[0.06em]">
                    {dir}
                  </p>
                )}
                <ul>
                  {files.map((f) => (
                    <li key={f.source}>
                      <button
                        type="button"
                        onClick={() => openFileAt({ source: f.source })}
                        className="sf-dim flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12px] transition-colors hover:bg-[var(--sf-raised)] hover:text-[var(--sf-text)]"
                      >
                        <FileCode className="h-3.5 w-3.5 shrink-0 opacity-50" />
                        <span className="truncate">{f.file_name.split("/").pop()}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}

      {target && file && !loading && (
        <p className="sf-mute shrink-0 border-t sf-line px-3 py-1.5 text-[11px]">
          {lines.length} lines · {file.language}
          {citedCount > 0 &&
            ` · ${citedCount} cited ${citedCount === 1 ? "line" : "lines"} highlighted`}
        </p>
      )}
    </aside>
  );
}

/** Whether the reader wants the drawer open, remembered across sessions. */
export function useContextPanelOpen() {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem("savflux:contextPanel") === "1";
    } catch {
      return false;
    }
  });
  const set = useCallback((v: boolean) => {
    setOpen(v);
    try {
      localStorage.setItem("savflux:contextPanel", v ? "1" : "0");
    } catch {
      /* private mode — the preference simply does not persist */
    }
  }, []);
  return [open, set] as const;
}

export { X };
