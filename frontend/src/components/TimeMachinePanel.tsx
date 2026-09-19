/**
 * TimeMachinePanel.tsx — Per-file git history: timeline + blame (P1 #1).
 *
 * Left: indexed-file picker (git-backed repos only — uploads have no
 * upstream history). Middle: commit timeline from GET /activity/file
 * (index record + git log --follow). Right: line-level blame from
 * GET /history/blame; clicking a commit re-blames at that revision.
 */

import { useCallback, useEffect, useState } from "react";
import {
  History, Loader2, GitCommitHorizontal, ChevronRight, AlertTriangle,
  User, CalendarDays, Database,
} from "lucide-react";
import { IndexedFile } from "../types";
import { apiFetch } from "../api";

interface Commit {
  sha: string;
  short_sha: string;
  author: string;
  date: string;
  message: string;
}

interface BlameLine {
  line_no: number;
  sha: string;
  short_sha: string;
  author: string;
  date: string;
  content: string;
}

interface TimeMachinePanelProps {
  initialSource?: string | null;
  onInitialSourceConsumed?: () => void;
}

const AUTHOR_COLORS = [
  "text-blue-300", "text-emerald-300", "text-amber-300",
  "text-purple-300", "text-pink-300", "text-cyan-300",
];

function authorColor(author: string): string {
  let h = 0;
  for (const c of author) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return AUTHOR_COLORS[h % AUTHOR_COLORS.length];
}

export function TimeMachinePanel({ initialSource, onInitialSourceConsumed }: TimeMachinePanelProps) {
  const [files, setFiles] = useState<IndexedFile[]>([]);
  const [source, setSource] = useState<string>("");
  const [commits, setCommits] = useState<Commit[]>([]);
  const [resolvedPath, setResolvedPath] = useState("");
  const [indexInfo, setIndexInfo] = useState<{ indexed_sha?: string | null; indexed_at?: number | null } | null>(null);
  const [blame, setBlame] = useState<BlameLine[]>([]);
  const [rev, setRev] = useState("HEAD");
  const [view, setView] = useState<"timeline" | "blame">("timeline");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Indexed files for the picker (git-backed only)
  useEffect(() => {
    apiFetch("/api/v1/chat/indexed-files")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const all: IndexedFile[] = data?.files ?? [];
        setFiles(all.filter((f) => f.source.includes("::")));
      })
      .catch(() => {});
  }, []);

  // Preselect when navigated from Code Writer / elsewhere
  useEffect(() => {
    if (initialSource) {
      setSource(initialSource);
      onInitialSourceConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource]);

  const loadTimeline = useCallback(async (src: string) => {
    if (!src) return;
    setLoading(true);
    setError(null);
    setRev("HEAD");
    try {
      const res = await apiFetch(`/api/v1/activity/file?file=${encodeURIComponent(src)}&limit=50`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
      setCommits(data.commits ?? []);
      setResolvedPath(data.path ?? "");
      setIndexInfo(data.index ?? null);
      if ((data.commits ?? []).length === 0) {
        setError("No git history for this file yet — the mirror builds on first read; retry in a few seconds.");
      }
    } catch (e) {
      setCommits([]);
      setError(e instanceof Error ? e.message : "Timeline failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadBlame = useCallback(async (src: string, revision: string) => {
    if (!src) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/v1/history/blame?file=${encodeURIComponent(src)}&rev=${encodeURIComponent(revision)}`
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
      setBlame(data.lines ?? []);
      setResolvedPath(data.path ?? "");
    } catch (e) {
      setBlame([]);
      setError(e instanceof Error ? e.message : "Blame failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (source) {
      if (view === "timeline") loadTimeline(source);
      else loadBlame(source, rev);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  const pickCommit = (c: Commit) => {
    setRev(c.sha);
    setView("blame");
    loadBlame(source, c.sha);
  };

  return (
    <div className="flex h-full overflow-hidden bg-gray-950">
      {/* File picker */}
      <div className="flex w-64 shrink-0 flex-col border-r border-gray-800">
        <div className="flex items-center gap-2 border-b border-gray-800 p-3">
          <History className="h-4 w-4 text-teal-400" />
          <h2 className="text-sm font-semibold text-white">Time Machine</h2>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {files.length === 0 && (
            <p className="p-3 text-xs text-gray-600">
              No git-backed files indexed. Index a GitHub repo to browse history.
            </p>
          )}
          {files.map((f) => (
            <button
              key={f.source}
              onClick={() => setSource(f.source)}
              title={f.source}
              className={`block w-full truncate rounded-lg px-2.5 py-1.5 text-left font-mono text-xs ${
                source === f.source
                  ? "bg-teal-600/20 text-teal-200"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              }`}
            >
              {f.file_name}
            </button>
          ))}
        </div>
      </div>

      {/* Main */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {!source && (
          <div className="flex flex-1 items-center justify-center p-8 text-center">
            <p className="max-w-sm text-sm text-gray-500">
              Pick a file to see its commit timeline and line-level blame.
              Uploaded files have no upstream git history.
            </p>
          </div>
        )}

        {source && (
          <>
            <div className="flex items-center gap-2 border-b border-gray-800 px-4 py-2.5">
              <div className="flex gap-1 rounded-lg border border-gray-700 bg-gray-900 p-1">
                {(["timeline", "blame"] as const).map((v) => (
                  <button
                    key={v}
                    onClick={() => {
                      setView(v);
                      if (v === "timeline") loadTimeline(source);
                      else loadBlame(source, rev);
                    }}
                    className={`rounded-md px-3 py-1 text-xs font-medium capitalize ${
                      view === v ? "bg-gray-700 text-white" : "text-gray-500 hover:text-gray-300"
                    }`}
                  >
                    {v}
                  </button>
                ))}
              </div>
              <span className="truncate font-mono text-xs text-gray-500" title={source}>
                {resolvedPath || source}
              </span>
              {view === "blame" && rev !== "HEAD" && (
                <button
                  onClick={() => { setRev("HEAD"); loadBlame(source, "HEAD"); }}
                  className="ml-auto shrink-0 rounded-lg border border-gray-700 px-2 py-1 font-mono text-[11px] text-gray-400 hover:text-white"
                  title="Back to current HEAD blame"
                >
                  @{rev.slice(0, 7)} ✕
                </button>
              )}
            </div>

            {indexInfo?.indexed_sha && (
              <div className="flex items-center gap-2 border-b border-gray-800/60 px-4 py-1.5 text-[11px] text-gray-500">
                <Database className="h-3 h-3" />
                index built from
                <span className="font-mono text-gray-400">{indexInfo.indexed_sha.slice(0, 7)}</span>
                {indexInfo.indexed_at && <span>· {new Date(indexInfo.indexed_at * 1000).toLocaleString()}</span>}
              </div>
            )}

            <div className="flex-1 overflow-y-auto p-4">
              {loading && (
                <p className="flex items-center gap-2 text-sm text-gray-400">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {view === "timeline" ? "Loading timeline…" : "Loading blame…"}
                </p>
              )}
              {error && (
                <div className="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
                  <AlertTriangle className="h-4 w-4 shrink-0" /> {error}
                </div>
              )}

              {/* Timeline */}
              {view === "timeline" && !loading && commits.length > 0 && (
                <ol className="relative space-y-1 border-l border-gray-800 pl-4">
                  {commits.map((c) => (
                    <li key={c.sha}>
                      <button
                        onClick={() => pickCommit(c)}
                        title="Blame file at this commit"
                        className="group block w-full rounded-lg px-3 py-2 text-left hover:bg-gray-900"
                      >
                        <div className="flex items-center gap-2">
                          <GitCommitHorizontal className="h-3.5 w-3.5 shrink-0 text-teal-400" />
                          <span className="truncate text-sm text-gray-200">{c.message}</span>
                          <ChevronRight className="ml-auto h-3.5 w-3.5 shrink-0 text-gray-600 opacity-0 transition-opacity group-hover:opacity-100" />
                        </div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 pl-5 text-[11px] text-gray-500">
                          <span className="font-mono text-teal-300">{c.short_sha}</span>
                          <span className={`flex items-center gap-1 ${authorColor(c.author)}`}>
                            <User className="h-3 w-3" /> {c.author}
                          </span>
                          <span className="flex items-center gap-1">
                            <CalendarDays className="h-3 w-3" /> {c.date}
                          </span>
                        </div>
                      </button>
                    </li>
                  ))}
                </ol>
              )}

              {/* Blame */}
              {view === "blame" && !loading && blame.length > 0 && (
                <div className="overflow-hidden rounded-xl border border-gray-800">
                  <div className="grid grid-cols-[3rem_7rem_1fr] font-mono text-xs">
                    {blame.map((l) => (
                      <div key={l.line_no} className="contents">
                        <span className="border-b border-gray-800/50 bg-gray-900 px-2 py-0.5 text-right text-gray-600">
                          {l.line_no}
                        </span>
                        <span
                          className={`truncate border-b border-gray-800/50 bg-gray-900 px-2 py-0.5 ${authorColor(l.author)}`}
                          title={`${l.sha}\n${l.author} · ${l.date}`}
                        >
                          {l.short_sha} {l.author.split(" ")[0]}
                        </span>
                        <span className="overflow-x-auto border-b border-gray-800/50 px-2 py-0.5 whitespace-pre text-gray-300">
                          {l.content || " "}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default TimeMachinePanel;
