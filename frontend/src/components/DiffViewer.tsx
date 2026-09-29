/** Read-only comparisons of indexed snapshots, not local files or Git branches. */
import { useEffect, useId, useRef, useState } from "react";
import {
  ArrowLeftRight,
  Copy,
  FileCode,
  GitCompare,
  Loader2,
  Search,
} from "lucide-react";
import { apiFetch } from "../api";
import { ReviewSyntax } from "../lib/highlight";
import { repositoryKey } from "../lib/repositorySelection";

type IndexedFile = {
  source: string;
  file_name: string;
  language: string;
  repo_url?: string;
};
type Comparison = {
  unified_diff: string;
  added: number;
  removed: number;
  similarity: number;
  a_lines: number;
  b_lines: number;
  a_content: string;
  b_content: string;
  a_truncated?: boolean;
  b_truncated?: boolean;
};
const pathName = (source: string) => source.split("::").pop() || source;

export default function DiffViewer({
  activeRepoUrl = null,
}: {
  activeRepoUrl?: string | null;
}) {
  const id = useId();
  const [files, setFiles] = useState<IndexedFile[]>([]);
  const [sourceA, setSourceA] = useState("");
  const [sourceB, setSourceB] = useState("");
  const [context, setContext] = useState(3);
  const [result, setResult] = useState<Comparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [filesLoading, setFilesLoading] = useState(true);
  const [filesError, setFilesError] = useState("");
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [query, setQuery] = useState("");
  const [split, setSplit] = useState(false);
  const requestRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setFilesLoading(true);
    setFilesError("");
    setFiles([]);
    setSourceA("");
    setSourceB("");
    void (async () => {
      try {
        const res = await apiFetch("/api/v1/chat/indexed-files", {
          signal: controller.signal,
        });
        if (!res.ok)
          throw new Error(`Could not load indexed files (HTTP ${res.status}).`);
        const data = await res.json();
        if (!Array.isArray(data.files))
          throw new Error("The index did not return a file list.");
        const scoped = data.files.filter(
          (file: IndexedFile) =>
            !activeRepoUrl ||
            repositoryKey(file.repo_url || file.source.split("::")[0]) ===
              repositoryKey(activeRepoUrl),
        );
        if (!controller.signal.aborted) setFiles(scoped);
      } catch (e) {
        if (!controller.signal.aborted)
          setFilesError(
            e instanceof Error ? e.message : "Could not load indexed files.",
          );
      } finally {
        if (!controller.signal.aborted) setFilesLoading(false);
      }
    })();
    return () => controller.abort();
  }, [activeRepoUrl, retry]);

  // Changing selectors/context invalidates the old output and cancels its request.
  useEffect(() => {
    requestRef.current?.abort();
    setResult(null);
    setError("");
    setCopyStatus("");
    setLoading(false);
    return () => requestRef.current?.abort();
  }, [sourceA, sourceB, context, activeRepoUrl]);

  const blockedReason = loading
    ? "Comparing indexed snapshots…"
    : filesLoading
      ? "Loading indexed files…"
      : filesError
        ? "Retry loading the file list before comparing."
        : files.length < 2
          ? "Index at least two files in this repository to compare their contents."
          : !sourceA || !sourceB
            ? "Choose File A and File B to compare."
            : sourceA === sourceB
              ? "Choose two different files to compare."
              : "";

  const compare = async () => {
    if (blockedReason) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError("");
    setCopyStatus("");
    setResult(null);
    try {
      const res = await apiFetch("/api/v1/diff/compare", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_a: sourceA, source_b: sourceB, context }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok)
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Comparison failed (HTTP ${res.status}).`,
        );
      if (
        typeof data.unified_diff !== "string" ||
        typeof data.a_content !== "string" ||
        typeof data.b_content !== "string"
      ) {
        throw new Error(
          "The server did not return a complete comparison. Retry after refreshing the index.",
        );
      }
      if (!controller.signal.aborted) setResult(data);
    } catch (e) {
      if (!controller.signal.aborted)
        setError(e instanceof Error ? e.message : "Comparison failed.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  const copy = async () => {
    if (!result?.unified_diff) return;
    try {
      await navigator.clipboard.writeText(result.unified_diff);
      setCopyStatus("Diff copied.");
    } catch {
      setCopyStatus(
        "Clipboard unavailable. Select and copy the unified diff instead.",
      );
    }
  };

  const matches = files.filter((file) =>
    `${file.source} ${file.file_name}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  // Search must not remove the selected option and silently mislabel its value.
  const options = files.filter(
    (file) =>
      matches.includes(file) ||
      file.source === sourceA ||
      file.source === sourceB,
  );
  const picker = (
    side: "A" | "B",
    value: string,
    setValue: (value: string) => void,
  ) => (
    <label className="block min-w-0 text-xs sf-dim">
      File {side}
      <select
        aria-label={`File ${side}`}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        disabled={filesLoading || files.length === 0}
        className="sf-input mt-1 block w-full min-w-0 text-sm"
        title={value || `Choose File ${side}`}
      >
        <option value="">Choose a file…</option>
        {options.map((file) => (
          <option key={file.source} value={file.source}>
            {pathName(file.source)}
            {!activeRepoUrl
              ? ` — ${file.repo_url || file.source.split("::")[0]}`
              : ""}
          </option>
        ))}
      </select>
    </label>
  );
  const snapshot = (
    source: string,
    content: string,
    side: string,
    count: number,
  ) => (
    <section
      className="min-w-0 overflow-hidden rounded-lg border sf-line"
      aria-label={`File ${side} snapshot`}
    >
      <header className="sf-raised border-b sf-line p-3">
        <div className="sf-text flex min-w-0 items-center gap-2 text-xs font-medium">
          <FileCode className="h-4 w-4 shrink-0" />
          <span className="min-w-0 truncate" title={source}>
            {pathName(source)}
          </span>
        </div>
        <p className="sf-mute mt-1 text-xs">
          File {side} · {count} lines · indexed snapshot
        </p>
      </header>
      {((side === "A" ? result?.a_truncated : result?.b_truncated) ??
        content.length >= 20000) && (
        <p className="sf-mute border-b sf-line p-3 text-xs">
          Preview limited to the first 20,000 characters. The unified diff
          compares the complete indexed snapshots.
        </p>
      )}
      <div className="max-h-[60vh] overflow-auto py-2">
        {content === "" ? (
          <p className="sf-mute p-3 text-xs">Empty file</p>
        ) : (
          <ReviewSyntax
            code={content}
            language={
              files.find((file) => file.source === source)?.language ||
              source.split(".").pop() ||
              "text"
            }
            renderLines={(rows) =>
              rows.map((row, i) => (
                <div className="sf-code-line" key={i}>
                  <span className="sf-line-number">{i + 1}</span>
                  <code>{row}</code>
                </div>
              ))
            }
          />
        )}
      </div>
    </section>
  );

  return (
    <section className="mx-auto w-full min-w-0 max-w-5xl space-y-4">
      <header>
        <div className="flex flex-wrap items-center gap-2">
          <GitCompare className="sf-accent h-5 w-5" />
          <h2 className="sf-text text-lg font-semibold">
            Compare indexed files
          </h2>
          <span className="sf-chip">READ ONLY</span>
        </div>
        <p className="sf-dim mt-1 text-xs leading-relaxed">
          Choose two indexed snapshots
          {activeRepoUrl ? " from the selected repository" : ""}. These are not
          live working files or branch revisions. Use <strong>Branches</strong>{" "}
          for a GitHub branch diff.
        </p>
      </header>
      <div className="sf-surface space-y-4 rounded-xl border sf-line p-4">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Search className="sf-mute h-4 w-4 shrink-0" />
          <input
            aria-label="Filter files"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by file path…"
            className="sf-input min-w-0 flex-1 text-sm"
          />
          <span className="sf-mute text-xs">
            {query ? `${matches.length} matches · ` : ""}
            {files.length} files
          </span>
        </div>
        <div className="grid items-end gap-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
          {picker("A", sourceA, setSourceA)}
          <button
            type="button"
            onClick={() => {
              setSourceA(sourceB);
              setSourceB(sourceA);
            }}
            aria-label="Swap files"
            title="Swap File A and File B"
            disabled={!sourceA || !sourceB}
            className="sf-iconbtn h-9 w-9 justify-self-center"
          >
            <ArrowLeftRight className="h-4 w-4" />
          </button>
          {picker("B", sourceB, setSourceB)}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="sf-dim flex items-center gap-2 whitespace-nowrap text-xs">
            Diff context
            <select
              value={context}
              onChange={(e) => setContext(Number(e.target.value))}
              className="sf-input text-xs"
            >
              {[0, 1, 2, 3, 5, 10].map((n) => (
                <option key={n} value={n}>
                  {n} lines
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => void compare()}
            disabled={Boolean(blockedReason)}
            aria-describedby={`${id}-status`}
            className="sf-btn sf-btn-primary"
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <GitCompare className="h-4 w-4" />
            )}
            {loading ? "Comparing…" : "Compare"}
          </button>
        </div>
        <p id={`${id}-status`} role="status" className="sf-mute text-xs">
          {blockedReason || "Ready to compare File A → File B."}
        </p>
        {filesError && (
          <div role="alert" className="text-xs text-[var(--sf-bad)]">
            {filesError}
            <button
              className="sf-btn sf-btn-secondary ml-2"
              onClick={() => setRetry((n) => n + 1)}
            >
              Retry loading files
            </button>
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-[var(--sf-bad)]">
            {error}
          </p>
        )}
      </div>
      {result && (
        <div className="space-y-3" aria-label="Indexed comparison result">
          <div className="sf-surface flex flex-wrap items-center justify-between gap-3 rounded-xl border sf-line p-3">
            <p className="sf-dim text-xs">
              <span className="text-[var(--sf-good)]">+{result.added}</span> /{" "}
              <span className="text-[var(--sf-bad)]">−{result.removed}</span> ·
              similarity {Math.round(result.similarity * 100)}%
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                className={`sf-btn ${!split ? "sf-btn-primary" : "sf-btn-secondary"}`}
                aria-pressed={!split}
                onClick={() => setSplit(false)}
              >
                Unified diff
              </button>
              <button
                className={`sf-btn ${split ? "sf-btn-primary" : "sf-btn-secondary"}`}
                aria-pressed={split}
                onClick={() => setSplit(true)}
              >
                Side-by-side previews
              </button>
              <button
                onClick={() => void copy()}
                disabled={!result.unified_diff}
                className="sf-btn sf-btn-secondary"
              >
                <Copy className="h-3 w-3" />
                Copy diff
              </button>
            </div>
          </div>
          {copyStatus && (
            <p role="status" className="sf-dim text-xs">
              {copyStatus}
            </p>
          )}
          {!result.unified_diff && (
            <p
              role="status"
              className="sf-dim rounded-xl border sf-line p-4 text-sm"
            >
              These indexed files have identical contents.
            </p>
          )}
          {split ? (
            <>
              <p className="sf-mute text-xs">
                Snapshot previews, not line-aligned changes. Choose Unified diff
                to see added and removed lines.
              </p>
              <div className="grid min-w-0 gap-3 md:grid-cols-2">
                {snapshot(sourceA, result.a_content, "A", result.a_lines)}
                {snapshot(sourceB, result.b_content, "B", result.b_lines)}
              </div>
            </>
          ) : (
            result.unified_diff && (
              <pre
                aria-label="Unified file diff"
                className="sf-code-typography sf-surface max-h-[60vh] overflow-auto rounded-xl border sf-line p-3"
              >
                {result.unified_diff.split("\n").map((line, i) => (
                  <span
                    key={i}
                    className={`block whitespace-pre ${line.startsWith("+++") || line.startsWith("---") ? "sf-mute" : line.startsWith("+") ? "text-[var(--sf-good)] bg-emerald-500/10" : line.startsWith("-") ? "text-[var(--sf-bad)] bg-red-500/10" : line.startsWith("@@") ? "sf-accent" : "sf-dim"}`}
                  >
                    {line || " "}
                  </span>
                ))}
              </pre>
            )
          )}
        </div>
      )}
    </section>
  );
}
