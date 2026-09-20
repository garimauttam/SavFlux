/**
 * IngestPanel.tsx — Left sidebar: ingest repos, select active repo, clear index.
 *
 * Enhancements:
 * 1. Repo selector — when multiple repos are indexed, shows a dropdown to pick
 *    which one the chat is scoped to. Sends `activeRepoUrl` to useChat.
 *
 * 2. Clear index button — calls DELETE /ingest/clear to wipe a specific repo
 *    or the entire index. Prevents the "mixed-repo answer pollution" problem.
 *
 * 3. Indexed file list now groups by repo when multiple repos exist.
 */

import React, { useState, useRef } from "react";
import {
  Github, Upload, Loader2, CheckCircle, AlertCircle, FileCode,
  Trash2, Database, ChevronDown,
} from "lucide-react";
import { IngestionProgress, IndexedFile, IndexedRepo } from "../types";
import { apiFetch } from "../api";

interface IngestPanelProps {
  indexedFiles: IndexedFile[];
  indexedRepos: IndexedRepo[];
  activeRepoUrl: string | null;
  activeRepoUrls: string[];           // multi-repo selection
  onSetActiveRepo: (url: string | null) => void;
  onSetActiveRepoUrls: (urls: string[]) => void;  // multi-repo setter
  onFilesUpdated: () => void;
}

export function IngestPanel({
  indexedFiles,
  indexedRepos,
  activeRepoUrl,
  activeRepoUrls,
  onSetActiveRepo,
  onSetActiveRepoUrls,
  onFilesUpdated,
}: IngestPanelProps) {
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [progress, setProgress] = useState<IngestionProgress | null>(null);
  const [isIngesting, setIsIngesting] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── GitHub ingestion ───────────────────────────────────────────────────────
  const handleIngestGitHub = async () => {
    if (!repoUrl.trim() || isIngesting) return;

    setIsIngesting(true);
    setProgress({ step: "cloning", message: "Starting ingestion..." });

    try {
      const response = await apiFetch("/api/v1/ingest/github", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: repoUrl, branch: branch.trim() }),
      });

      if (!response.ok) {
        const err = await response.json();
        setProgress({ step: "error", message: err.detail ?? "Failed to ingest." });
        return;
      }

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";

        for (const line of frames) {
          if (line.startsWith("data: ")) {
            try {
              const event = JSON.parse(line.slice(6)) as IngestionProgress;
              setProgress(event);
              if (event.step === "complete") {
                onFilesUpdated();
                setRepoUrl("");
                setBranch("");
              }
            } catch {}
          }
        }
      }
    } catch (err) {
      setProgress({ step: "error", message: "Failed to ingest repository." });
    } finally {
      setIsIngesting(false);
    }
  };

  // ── File upload ingestion ──────────────────────────────────────────────────
  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setIsIngesting(true);
    setProgress({ step: "embedding", message: "Uploading and indexing files..." });

    const formData = new FormData();
    for (const file of Array.from(files)) {
      formData.append("files", file);
    }

    try {
      const response = await apiFetch("/api/v1/ingest/files", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const err = await response.json();
        setProgress({ step: "error", message: err.detail ?? "Upload failed." });
        return;
      }

      const result = await response.json();
      setProgress({
        step: "done",
        message: `✅ Indexed ${result.chunks_created} chunks from ${result.files_indexed} files`,
      });
      onFilesUpdated();
    } catch {
      setProgress({ step: "error", message: "File upload failed." });
    } finally {
      setIsIngesting(false);
      // Reset the file input so the same file can be re-uploaded
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // ── Clear index ────────────────────────────────────────────────────────────
  const handleClear = async (repoToClear?: string) => {
    if (isClearing || isIngesting) return;
    const label = repoToClear ? `Remove "${repoToClear.split("/").slice(-1)[0]}"` : "clear entire index";
    if (!confirm(`Are you sure you want to ${label}? This cannot be undone.`)) return;

    setIsClearing(true);
    try {
      const url = repoToClear
        ? `/api/v1/ingest/clear?repo_url=${encodeURIComponent(repoToClear)}`
        : "/api/v1/ingest/clear";

      const response = await apiFetch(url, { method: "DELETE" });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        setProgress({ step: "error", message: err.detail ?? `Clear failed (${response.status}).` });
        return;
      }
      setProgress({ step: "done", message: "✅ Index cleared." });
      onFilesUpdated();
    } catch {
      setProgress({ step: "error", message: "Failed to clear index." });
    } finally {
      setIsClearing(false);
    }
  };

  const getProgressColor = () => {
    if (progress?.step === "done" || progress?.step === "complete") return "text-green-400";
    if (progress?.step === "error") return "text-red-400";
    return "text-blue-400";
  };

  // Derive a short display name from a repo URL
  const repoDisplayName = (url: string) => {
    try { return new URL(url).pathname.replace(/^\//, ""); }
    catch { return url; }
  };

  return (
    <aside className="w-72 bg-gray-900 border-r border-gray-700 flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-gray-700">
        <h1 className="text-xl font-bold text-white flex items-center gap-2">
          <FileCode className="w-6 h-6 text-purple-400" />
          SavFlux
        </h1>
        <p className="text-xs text-gray-400 mt-1">AI-powered codebase assistant</p>
      </div>

      {/* GitHub URL Input */}
      <div className="p-4 border-b border-gray-700">
        <label className="block text-sm font-medium text-gray-300 mb-2">
          Index a GitHub Repository
        </label>
        <div className="flex flex-col gap-2">
          <input
            type="text"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleIngestGitHub()}
            placeholder="https://github.com/owner/repo"
            className="w-full bg-gray-800 text-white text-sm rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-purple-500 placeholder-gray-500"
          />
          <input
            type="text"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleIngestGitHub()}
            placeholder="Branch (default: main)"
            className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-purple-500 placeholder-gray-500"
          />
          <button
            onClick={handleIngestGitHub}
            disabled={isIngesting || !repoUrl.trim()}
            className="flex items-center justify-center gap-2 bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:cursor-not-allowed text-white text-sm font-medium py-2 px-4 rounded-lg transition-colors"
          >
            {isIngesting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Github className="w-4 h-4" />}
            {isIngesting ? "Indexing..." : "Index Repo"}
          </button>
        </div>

        <div className="flex items-center my-3">
          <div className="flex-1 border-t border-gray-700" />
          <span className="px-2 text-xs text-gray-500">or</span>
          <div className="flex-1 border-t border-gray-700" />
        </div>

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".py,.js,.ts,.jsx,.tsx,.go,.java,.md,.txt"
          onChange={handleFileUpload}
          className="hidden"
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={isIngesting}
          className="w-full flex items-center justify-center gap-2 border border-dashed border-gray-600 hover:border-purple-500 text-gray-400 hover:text-purple-400 text-sm py-2 px-4 rounded-lg transition-colors"
        >
          <Upload className="w-4 h-4" />
          Upload files
        </button>

        {progress && (
          <div className={`mt-3 text-xs ${getProgressColor()} flex items-start gap-1.5`}>
            {(progress.step === "done" || progress.step === "complete") ? (
              <CheckCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            ) : progress.step === "error" ? (
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            ) : (
              <Loader2 className="w-3.5 h-3.5 mt-0.5 shrink-0 animate-spin" />
            )}
            <span>{progress.message}</span>
          </div>
        )}
      </div>

      {/* ── Active repo selector ─────────────────────────────────────────────── */}
      {indexedRepos.length > 0 && (
        <div className="p-4 border-b border-gray-700">
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-gray-400 uppercase tracking-wide flex items-center gap-1.5">
              <Database className="w-3 h-3" />
              {indexedRepos.length > 1 ? "Repos for Chat" : "Active Repo"}
            </label>
            {/* Clear entire index button */}
            <button
              onClick={() => handleClear()}
              disabled={isClearing || isIngesting}
              className="flex items-center gap-1 text-xs text-gray-600 hover:text-red-400 transition-colors disabled:opacity-40"
              title="Clear entire index"
            >
              {isClearing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Trash2 className="w-3 h-3" />}
              Clear all
            </button>
          </div>

          {/* ── Multi-repo checkboxes (shown when >1 repo indexed) ──────────── */}
          {indexedRepos.length > 1 ? (
            <div className="space-y-1.5">
              <p className="text-xs text-gray-500 mb-1.5">
                Select repos to search across (cross-search):
              </p>
              {indexedRepos.map((r) => {
                const checked = activeRepoUrls.length === 0 || activeRepoUrls.includes(r.repo_url);
                return (
                  <label
                    key={r.repo_url}
                    className="flex items-center gap-2 cursor-pointer group"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => {
                        if (e.target.checked) {
                          // All repos checked → clear filter (no filter = all)
                          const next = indexedRepos
                            .map((x) => x.repo_url)
                            .filter((u) => u === r.repo_url || activeRepoUrls.includes(u));
                          onSetActiveRepoUrls(next.length === indexedRepos.length ? [] : next);
                        } else {
                          // Uncheck: exclude this repo (keep others)
                          const base = activeRepoUrls.length === 0
                            ? indexedRepos.map((x) => x.repo_url)
                            : activeRepoUrls;
                          const next = base.filter((u) => u !== r.repo_url);
                          onSetActiveRepoUrls(next);
                        }
                      }}
                      className="accent-purple-500 w-3 h-3 shrink-0"
                    />
                    <span className="text-xs text-gray-400 group-hover:text-gray-200 truncate transition-colors">
                      {repoDisplayName(r.repo_url)}
                      <span className="text-gray-600 ml-1">({r.chunk_count})</span>
                    </span>
                    <button
                      onClick={(e) => { e.preventDefault(); handleClear(r.repo_url); }}
                      disabled={isClearing || isIngesting}
                      className="ml-auto text-gray-700 hover:text-red-400 transition-colors shrink-0 disabled:opacity-40"
                      title={`Remove ${repoDisplayName(r.repo_url)}`}
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </label>
                );
              })}
              {activeRepoUrls.length > 0 && activeRepoUrls.length < indexedRepos.length && (
                <p className="text-xs text-blue-400/80 mt-1">
                  Searching {activeRepoUrls.length} of {indexedRepos.length} repos
                </p>
              )}
            </div>
          ) : (
            /* Single repo: keep the original dropdown UI */
            <>
              <div className="relative">
                <select
                  value={activeRepoUrl ?? ""}
                  onChange={(e) => onSetActiveRepo(e.target.value || null)}
                  className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-purple-500 appearance-none pr-7"
                >
                  <option value="">All repos ({indexedRepos.length})</option>
                  {indexedRepos.map((r) => (
                    <option key={r.repo_url} value={r.repo_url}>
                      {repoDisplayName(r.repo_url)} ({r.chunk_count} chunks)
                    </option>
                  ))}
                </select>
                <ChevronDown className="absolute right-2 top-2.5 w-3.5 h-3.5 text-gray-500 pointer-events-none" />
              </div>

              {/* Per-repo clear button */}
              {activeRepoUrl && (
                <button
                  onClick={() => handleClear(activeRepoUrl)}
                  disabled={isClearing || isIngesting}
                  className="mt-2 w-full flex items-center justify-center gap-1.5 text-xs text-red-500 hover:text-red-400 border border-red-900/40 hover:border-red-700 py-1.5 rounded-lg transition-colors disabled:opacity-40"
                >
                  <Trash2 className="w-3 h-3" />
                  Remove this repo
                </button>
              )}
            </>
          )}
        </div>
      )}

      {/* Indexed Files list */}
      <div className="flex-1 overflow-y-auto p-4">
        <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-wide mb-2">
          Indexed Files ({indexedFiles.length})
        </h2>
        {indexedFiles.length === 0 ? (
          <p className="text-xs text-gray-600">No files indexed yet. Add a repo above.</p>
        ) : (
          <ul className="space-y-1">
            {indexedFiles
              // Show files from selected repos (or all if nothing selected)
              .filter((f) =>
                activeRepoUrls.length > 0
                  ? activeRepoUrls.includes(f.repo_url ?? "")
                  : !activeRepoUrl || f.repo_url === activeRepoUrl
              )
              .map((f) => (
                <li
                  key={f.source}
                  className="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-200 py-0.5"
                  title={f.source}
                >
                  <span className="shrink-0 text-purple-400 font-mono text-xs">{f.language}</span>
                  <span className="truncate">{f.file_name}</span>
                </li>
              ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
