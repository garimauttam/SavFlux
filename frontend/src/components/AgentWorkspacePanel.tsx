import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch, Loader2, RefreshCw, RotateCcw } from "lucide-react";
import { apiFetch } from "../api";

interface WorkspaceStatus {
  exists: boolean;
  dirty: boolean;
  repo_url?: string;
  message?: string;
  branch?: string;
  base_sha?: string;
  indexed_sha?: string;
  stale_index?: boolean;
  changed_file_count?: number;
  files_changed?: { status: string; path: string }[];
  stat?: string;
  diff?: string;
  truncated?: boolean;
}

async function responseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    /* non-JSON error */
  }
  return `Workspace request failed (HTTP ${res.status}).`;
}

export function AgentWorkspacePanel({ repoUrl }: { repoUrl: string | null }) {
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestSeq.current;
    if (!repoUrl) {
      setStatus(null);
      setError(null);
      setLoading(false);
      return;
    }
    setStatus(null);
    setLoading(true);
    setError(null);
    try {
      const url = `/api/v1/workspace/status?repo=${encodeURIComponent(repoUrl)}`;
      const res = await apiFetch(url);
      if (!res.ok) throw new Error(await responseError(res));
      const data = (await res.json()) as WorkspaceStatus;
      if (requestId === requestSeq.current) setStatus(data);
    } catch (e) {
      if (requestId === requestSeq.current) {
        setError(e instanceof Error ? e.message : "Could not load the local Agent workspace.");
      }
    } finally {
      if (requestId === requestSeq.current) setLoading(false);
    }
  }, [repoUrl]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const reset = async () => {
    const prompt = status?.stale_index
      ? "Reset this SavFlux Agent worktree to the latest indexed commit? Any uncommitted changes here will be discarded. GitHub will not change."
      : "Discard every uncommitted change in this SavFlux Agent worktree? This does not change GitHub.";
    if (!repoUrl || !window.confirm(prompt)) return;
    const requestId = requestSeq.current;
    setResetting(true);
    setError(null);
    try {
      const url = `/api/v1/workspace/reset?repo=${encodeURIComponent(repoUrl)}`;
      const res = await apiFetch(url, { method: "POST" });
      if (!res.ok) throw new Error(await responseError(res));
      const data = (await res.json()) as WorkspaceStatus;
      if (requestId === requestSeq.current) setStatus(data);
    } catch (e) {
      if (requestId === requestSeq.current) {
        setError(e instanceof Error ? e.message : "Could not reset the local workspace.");
      }
    } finally {
      setResetting(false);
    }
  };

  if (!repoUrl) {
    return (
      <div className="mx-auto mt-10 max-w-xl rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-6 text-center">
        <GitBranch className="mx-auto h-6 w-6 text-[var(--sf-text-mute)]" />
        <h2 className="mt-3 text-sm font-semibold text-[var(--sf-text)]">Choose an indexed GitHub repository</h2>
        <p className="mt-1 text-xs leading-relaxed text-[var(--sf-text-dim)]">Agent workspaces are isolated per repository and are created from its indexed commit.</p>
      </div>
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)]">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--sf-line)] px-4 py-3.5 sm:px-5">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]"><GitBranch className="h-4 w-4" /></span>
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-[var(--sf-text)]">Local Agent workspace</h2>
            <p className="truncate text-[11px] text-[var(--sf-text-mute)]">{repoUrl.replace(/^https?:\/\//, "").replace(/\.git$/, "")}</p>
          </div>
        </div>
        <div className="flex gap-2">
          <button type="button" onClick={() => void refresh()} disabled={loading || resetting} className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--sf-line)] px-2.5 py-1.5 text-[11px] text-[var(--sf-text-dim)] hover:bg-[var(--sf-raised)] disabled:opacity-50">
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Refresh
          </button>
          {(status?.dirty || status?.stale_index) && (
            <button type="button" onClick={() => void reset()} disabled={resetting} className="inline-flex items-center gap-1.5 rounded-lg border border-rose-400/30 px-2.5 py-1.5 text-[11px] text-rose-500 hover:bg-rose-500/10 disabled:opacity-50">
              {resetting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RotateCcw className="h-3.5 w-3.5" />} {status?.stale_index && !status.dirty ? "Update base" : "Reset edits"}
            </button>
          )}
        </div>
      </header>

      <div className="p-4 sm:p-5">
        {error && <p role="alert" className="mb-3 rounded-lg border border-rose-400/25 bg-rose-500/5 px-3 py-2 text-xs text-rose-400">{error}</p>}
        {loading && !status ? (
          <div className="flex items-center gap-2 py-8 text-xs text-[var(--sf-text-dim)]"><Loader2 className="h-4 w-4 animate-spin" /> Loading workspace status…</div>
        ) : status?.exists ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className={`rounded-full px-2 py-1 font-medium ${status.dirty ? "bg-amber-500/10 text-amber-500" : "bg-emerald-500/10 text-emerald-500"}`}>{status.dirty ? `${status.changed_file_count ?? 0} changed` : "Clean"}</span>
              <span className="font-mono text-[var(--sf-text-dim)]">{status.branch}</span>
              {status.base_sha && <span className="text-[var(--sf-text-mute)]">· based on {status.base_sha.slice(0, 10)}</span>}
            </div>
            {status.stale_index && (
              <p className="mt-3 rounded-lg border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-[11px] leading-relaxed text-amber-600">
                The repository has been re-indexed since this worktree was created. Your local edits still compare against {status.base_sha?.slice(0, 10)}; applying new proposals is paused. Reset will discard these edits and move the worktree to the latest indexed commit {status.indexed_sha?.slice(0, 10)}.
              </p>
            )}
            {status.stat && <p className="mt-3 text-xs text-[var(--sf-text-dim)]">{status.stat}</p>}
            {!!status.files_changed?.length && (
              <ul className="mt-3 divide-y divide-[var(--sf-line)] rounded-xl border border-[var(--sf-line)]">
                {status.files_changed.map((file) => <li key={file.path} className="flex items-center gap-2 px-3 py-2 text-xs"><span className={`w-4 font-mono font-bold ${file.status === "A" ? "text-emerald-500" : file.status === "D" ? "text-rose-500" : "text-amber-500"}`}>{file.status}</span><span className="truncate font-mono text-[var(--sf-text)]">{file.path}</span></li>)}
              </ul>
            )}
            {status.diff && <pre className="mt-3 max-h-[55vh] overflow-auto rounded-xl border border-[var(--sf-line)] bg-[var(--sf-canvas)] p-3 font-mono text-[11px] leading-relaxed text-[var(--sf-text-dim)]">{status.diff}</pre>}
            {status.truncated && <p className="mt-2 text-[11px] text-amber-500">The displayed diff is capped at 200 kB; the worktree retains the full changes.</p>}
            {!status.dirty && <p className="mt-3 text-xs text-[var(--sf-text-dim)]">Build a patch in Agent mode to add proposed edits here. Nothing has been pushed.</p>}
          </>
        ) : (
          <div className="py-7 text-center">
            <p className="text-sm font-medium text-[var(--sf-text)]">No local Agent worktree yet</p>
            <p className="mx-auto mt-1.5 max-w-lg text-xs leading-relaxed text-[var(--sf-text-dim)]">When Agent builds a patch for this repository, SavFlux creates a server-side Git worktree at the indexed commit and saves the proposal on an isolated local branch. It stays local until you explicitly approve a PR.</p>
            {status?.message && <p className="mt-2 text-[11px] text-[var(--sf-text-mute)]">{status.message}</p>}
          </div>
        )}
      </div>
      <footer className="border-t border-[var(--sf-line)] px-4 py-2.5 text-[10.5px] leading-relaxed text-[var(--sf-text-mute)] sm:px-5">
        This is a persistent local Git worktree on the SavFlux server, not your laptop. Changes stay uncommitted and unpushed. Reset discards only this worktree’s local edits; PR creation remains separately gated by explicit diff confirmation.
      </footer>
    </section>
  );
}
