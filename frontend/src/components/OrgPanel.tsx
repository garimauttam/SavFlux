/**
 * OrgPanel.tsx — Workspace / org repo manager (P2 Org, $0).
 *
 * One place to see every indexed repo, pick the active (scoped) repo, and
 * multi-select repos for cross-repo search. Per-repo clearing reuses the
 * existing DELETE /ingest/clear endpoint.
 */

import { useState } from "react";
import {
  Building2, Check, Loader2, RefreshCw, Trash2, Layers, Circle,
  CheckCircle2, AlertTriangle,
} from "lucide-react";
import { IndexedRepo } from "../types";
import { apiFetch } from "../api";

interface OrgPanelProps {
  indexedRepos: IndexedRepo[];
  activeRepoUrl: string | null;
  activeRepoUrls: string[];
  onSetActiveRepo: (url: string | null) => void;
  onSetActiveRepoUrls: (urls: string[]) => void;
  onFilesUpdated: () => void;
}

function repoName(url: string): string {
  try {
    const parts = new URL(url).pathname.split("/").filter(Boolean);
    return parts.slice(-2).join("/") || url;
  } catch {
    return url;
  }
}

export function OrgPanel({
  indexedRepos,
  activeRepoUrl,
  activeRepoUrls,
  onSetActiveRepo,
  onSetActiveRepoUrls,
  onFilesUpdated,
}: OrgPanelProps) {
  const [clearing, setClearing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const toggleMulti = (url: string) => {
    onSetActiveRepoUrls(
      activeRepoUrls.includes(url)
        ? activeRepoUrls.filter((u) => u !== url)
        : [...activeRepoUrls, url]
    );
  };

  const clearRepo = async (url: string) => {
    if (!window.confirm(`Remove all indexed data for ${repoName(url)}?`)) return;
    setClearing(url);
    setError(null);
    setNotice(null);
    try {
      const res = await apiFetch(`/api/v1/ingest/clear?repo_url=${encodeURIComponent(url)}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      if (activeRepoUrl === url) onSetActiveRepo(null);
      onSetActiveRepoUrls(activeRepoUrls.filter((u) => u !== url));
      setNotice(`Cleared ${repoName(url)}`);
      onFilesUpdated();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Clear failed");
    } finally {
      setClearing(null);
    }
  };

  const totalChunks = indexedRepos.reduce((n, r) => n + (r.chunk_count ?? 0), 0);

  return (
    <div className="h-full overflow-y-auto bg-gray-950 p-6">
      <div className="mx-auto max-w-3xl space-y-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Building2 className="w-5 h-5 text-cyan-400" />
            <h2 className="text-lg font-semibold text-white">Workspace</h2>
            <span className="text-xs text-gray-500">
              {indexedRepos.length} repos · {totalChunks} chunks
            </span>
          </div>
          <button
            onClick={onFilesUpdated}
            title="Refresh repo list"
            className="flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:text-white"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2.5 text-sm text-red-300">
            <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}
        {notice && (
          <div className="flex items-center gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2.5 text-sm text-emerald-300">
            <Check className="w-4 h-4 shrink-0" /> {notice}
          </div>
        )}

        {indexedRepos.length === 0 && (
          <div className="rounded-xl border border-dashed border-gray-700 p-8 text-center text-sm text-gray-500">
            No repos indexed yet. Use the sidebar to ingest a GitHub repo or upload files.
          </div>
        )}

        <div className="space-y-2">
          {indexedRepos.map((repo) => {
            const isActive = activeRepoUrl === repo.repo_url;
            const inMulti = activeRepoUrls.includes(repo.repo_url);
            const isClearing = clearing === repo.repo_url;
            return (
              <div
                key={repo.repo_url}
                className={`rounded-xl border p-4 transition-colors ${
                  isActive ? "border-cyan-500/40 bg-cyan-500/5" : "border-gray-800 bg-gray-900"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      {isActive
                        ? <CheckCircle2 className="w-4 h-4 shrink-0 text-cyan-400" />
                        : <Circle className="w-4 h-4 shrink-0 text-gray-600" />}
                      <span className="truncate text-sm font-medium text-white">
                        {repoName(repo.repo_url)}
                      </span>
                    </div>
                    <p className="mt-1 truncate font-mono text-[11px] text-gray-500">{repo.repo_url}</p>
                    <p className="mt-1 text-xs text-gray-500">{repo.chunk_count ?? 0} chunks indexed</p>
                  </div>
                  <button
                    onClick={() => clearRepo(repo.repo_url)}
                    disabled={isClearing}
                    title={`Remove ${repoName(repo.repo_url)} from the index`}
                    className="shrink-0 rounded-lg border border-gray-700 bg-gray-800 p-2 text-gray-400 hover:border-red-500/50 hover:text-red-300 disabled:opacity-50"
                  >
                    {isClearing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                  </button>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    onClick={() => onSetActiveRepo(isActive ? null : repo.repo_url)}
                    className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                      isActive
                        ? "bg-cyan-600 text-white hover:bg-cyan-500"
                        : "border border-gray-700 bg-gray-800 text-gray-300 hover:text-white"
                    }`}
                  >
                    {isActive ? "Active ✓" : "Set active"}
                  </button>
                  <button
                    onClick={() => toggleMulti(repo.repo_url)}
                    title="Include in multi-repo cross-search"
                    className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                      inMulti
                        ? "bg-purple-600/30 text-purple-200 border border-purple-500/40"
                        : "border border-gray-700 bg-gray-800 text-gray-400 hover:text-white"
                    }`}
                  >
                    <Layers className="w-3.5 h-3.5" />
                    {inMulti ? "In cross-search ✓" : "Add to cross-search"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        {activeRepoUrls.length > 0 && (
          <div className="rounded-xl border border-purple-500/30 bg-purple-500/5 p-4 text-xs text-purple-200">
            Cross-search spans {activeRepoUrls.length} repo{activeRepoUrls.length === 1 ? "" : "s"}.
            Chat retrieval and the dependency graph combine results across all of them.
            <button
              onClick={() => onSetActiveRepoUrls([])}
              className="ml-2 underline hover:text-white"
            >
              Clear selection
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default OrgPanel;
