/**
 * BulkOpsPanel.tsx — P2 Bulk File Operations ($0, local Chroma)
 *
 * Features:
 *  - Lists indexed files with search + language/repo filters
 *  - Checkbox multi-select (Select All / Clear)
 *  - Bulk actions: Copy paths, Export markdown bundle, Delete selected (POST /bulk/delete)
 *  - Stats bar (total, by language, by repo) from GET /bulk/stats
 *  - Progress + error handling, $0 no LLM
 */

import { useEffect, useState, useMemo } from "react";
import { apiFetch } from "../api";
import { Layers, Trash2, Download, Copy, Search, Filter, CheckSquare, Square, X } from "lucide-react";

type IndexedFile = { source: string; file_name?: string; language?: string; repo_url?: string; chunk_count?: number };

export default function BulkOpsPanel({ onFilesUpdated }: { onFilesUpdated?: () => void }) {
  const [files, setFiles] = useState<IndexedFile[]>([]);
  const [stats, setStats] = useState<{ total_files: number; by_language: Record<string, number>; by_repo: Record<string, number> } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [langFilter, setLangFilter] = useState("");
  const [repoFilter, setRepoFilter] = useState("");
  const [bulkLoading, setBulkLoading] = useState(false);
  const [bulkResult, setBulkResult] = useState<string | null>(null);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await apiFetch("/api/v1/bulk/stats");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setStats({ total_files: data.total_files, by_language: data.by_language || {}, by_repo: data.by_repo || {} });
      setFiles((data.files || []) as IndexedFile[]);
    } catch (e: any) {
      setError(e?.message || "Failed to load files");
      // Fallback to old endpoint
      try {
        const r2 = await apiFetch("/api/v1/chat/indexed-files");
        if (r2.ok) {
          const j2 = await r2.json();
          setFiles(j2.files || []);
        }
      } catch {}
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, []);

  const filtered = useMemo(() => {
    return files.filter((f) => {
      const src = f.source || "";
      const lang = (f.language || "").toLowerCase();
      const repo = f.repo_url || src.split("::")[0] || "";
      if (query && !`${src} ${f.file_name || ""} ${lang}`.toLowerCase().includes(query.toLowerCase())) return false;
      if (langFilter && lang !== langFilter.toLowerCase()) return false;
      if (repoFilter && !repo.includes(repoFilter)) return false;
      return true;
    });
  }, [files, query, langFilter, repoFilter]);

  const allFilteredSelected = filtered.length > 0 && filtered.every((f) => selected.has(f.source));
  const toggleAll = () => {
    if (allFilteredSelected) {
      const next = new Set(selected);
      filtered.forEach((f) => next.delete(f.source));
      setSelected(next);
    } else {
      const next = new Set(selected);
      filtered.forEach((f) => next.add(f.source));
      setSelected(next);
    }
  };

  const toggleOne = (src: string) => {
    const next = new Set(selected);
    if (next.has(src)) next.delete(src); else next.add(src);
    setSelected(next);
  };

  const handleCopyPaths = async () => {
    const paths = Array.from(selected);
    if (!paths.length) return;
    try { await navigator.clipboard.writeText(paths.join("\n")); setBulkResult(`Copied ${paths.length} paths`); } catch { setBulkResult("Copy failed"); }
    setTimeout(() => setBulkResult(null), 2000);
  };

  const handleExport = async () => {
    const sources = Array.from(selected);
    if (!sources.length) return;
    try {
      setBulkLoading(true);
      const res = await apiFetch("/api/v1/bulk/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sources }) });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const blob = new Blob([data.markdown], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = data.filename || "bulk-export.md"; a.click();
      URL.revokeObjectURL(url);
      setBulkResult(`Exported ${data.found}/${data.requested} files`);
    } catch (e: any) {
      setBulkResult(e?.message || "Export failed");
    } finally {
      setBulkLoading(false);
      setTimeout(() => setBulkResult(null), 2500);
    }
  };

  const handleDelete = async () => {
    const sources = Array.from(selected);
    if (!sources.length) return;
    if (!confirm(`Delete ${sources.length} file(s) from index? This removes their chunks from ChromaDB.`)) return;
    try {
      setBulkLoading(true);
      const res = await apiFetch("/api/v1/bulk/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sources }) });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setBulkResult(`Deleted ${data.deleted} chunks from ${data.requested} files${data.not_found?.length ? `, ${data.not_found.length} not found` : ""}`);
      setSelected(new Set());
      fetchData();
      onFilesUpdated?.();
    } catch (e: any) {
      setBulkResult(e?.message || "Delete failed");
    } finally {
      setBulkLoading(false);
      setTimeout(() => setBulkResult(null), 3000);
    }
  };

  const languages = stats ? Object.keys(stats.by_language).sort() : Array.from(new Set(files.map((f) => f.language || "text"))).sort();
  const repos = stats ? Object.keys(stats.by_repo).sort() : Array.from(new Set(files.map((f) => f.repo_url || ""))).filter(Boolean).sort();

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Layers className="w-5 h-5 text-blue-400" />
          <h2 className="text-base font-bold text-blue-300">Bulk Operations</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-200">$0</span>
          {loading && <span className="text-xs text-gray-500">loading…</span>}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{selected.size} selected / {filtered.length} shown / {files.length} total</span>
          <button onClick={fetchData} className="text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Refresh</button>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}
      {bulkResult && <div className="text-xs text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded p-2">{bulkResult}</div>}

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-3 gap-2 text-xs">
          <div className="rounded-lg border border-white/10 bg-white/[0.03] p-2">
            <div className="text-gray-500">Total files</div>
            <div className="text-sm font-bold text-white">{stats.total_files}</div>
          </div>
          <div className="rounded-lg border border-white/10 bg-white/[0.03] p-2">
            <div className="text-gray-500">By language</div>
            <div className="text-xs text-gray-300 truncate">{Object.entries(stats.by_language).slice(0, 4).map(([k, v]) => `${k}:${v}`).join(" · ") || "—"}</div>
          </div>
          <div className="rounded-lg border border-white/10 bg-white/[0.03] p-2">
            <div className="text-gray-500">By repo</div>
            <div className="text-xs text-gray-300 truncate">{Object.entries(stats.by_repo).slice(0, 2).map(([k, v]) => `${k.split("/").pop()}:${v}`).join(" · ") || "—"}</div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-2 flex-1 min-w-[200px] px-2 py-1.5 rounded bg-black/30 border border-white/10">
          <Search className="w-3.5 h-3.5 text-gray-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search files (path, name, language)…" className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 focus:outline-none" />
          {query && <button onClick={() => setQuery("")} className="p-0.5 hover:bg-white/10 rounded"><X className="w-3 h-3 text-gray-500" /></button>}
        </div>
        <div className="flex items-center gap-1 text-xs text-gray-500"><Filter className="w-3 h-3" /></div>
        <select value={langFilter} onChange={(e) => setLangFilter(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-xs text-white">
          <option value="">all languages</option>
          {languages.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <select value={repoFilter} onChange={(e) => setRepoFilter(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-xs text-white">
          <option value="">all repos</option>
          {repos.map((r) => <option key={r} value={r}>{r.split("/").pop() || r}</option>)}
        </select>
        <button onClick={() => { setQuery(""); setLangFilter(""); setRepoFilter(""); }} className="text-xs px-2 py-1.5 rounded bg-white/5 hover:bg-white/10 text-gray-400">Clear filters</button>
      </div>

      {/* Bulk actions */}
      <div className="flex flex-wrap gap-2">
        <button onClick={toggleAll} className="text-xs px-2.5 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200 flex items-center gap-1">
          {allFilteredSelected ? <CheckSquare className="w-3 h-3" /> : <Square className="w-3 h-3" />} {allFilteredSelected ? "Deselect all" : "Select all"} ({filtered.length})
        </button>
        <button onClick={() => setSelected(new Set())} disabled={!selected.size} className="text-xs px-2.5 py-1.5 rounded bg-white/5 hover:bg-white/10 text-gray-400 disabled:opacity-40">Clear selection</button>
        <div className="ml-auto flex gap-2">
          <button onClick={handleCopyPaths} disabled={!selected.size} className="text-xs px-2.5 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200 disabled:opacity-40 flex items-center gap-1"><Copy className="w-3 h-3" /> Copy paths</button>
          <button onClick={handleExport} disabled={!selected.size || bulkLoading} className="text-xs px-2.5 py-1.5 rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 disabled:opacity-40 flex items-center gap-1"><Download className="w-3 h-3" /> Export</button>
          <button onClick={handleDelete} disabled={!selected.size || bulkLoading} className="text-xs px-2.5 py-1.5 rounded bg-red-500/20 hover:bg-red-500/30 text-red-300 disabled:opacity-40 flex items-center gap-1"><Trash2 className="w-3 h-3" /> Delete</button>
        </div>
      </div>

      {/* File list */}
      <div className="rounded-lg border border-white/10 bg-white/[0.03] overflow-hidden">
        <div className="max-h-[50vh] overflow-y-auto">
          {filtered.length === 0 ? (
            <div className="text-sm text-gray-500 p-6 text-center">No files match filters</div>
          ) : (
            filtered.map((f) => {
              const src = f.source;
              const isSel = selected.has(src);
              return (
                <label key={src} className={`flex items-center gap-2 px-3 py-2 border-b border-white/5 hover:bg-white/[0.04] cursor-pointer ${isSel ? "bg-blue-500/10" : ""}`}>
                  <input type="checkbox" checked={isSel} onChange={() => toggleOne(src)} className="accent-blue-500" />
                  <span className="text-xs font-mono text-gray-300 truncate flex-1">{f.file_name || src}</span>
                  <span className="text-xs px-1.5 py-0.5 rounded bg-white/10 text-gray-400">{f.language || "text"}</span>
                  {f.chunk_count !== undefined && <span className="text-xs text-gray-500">{f.chunk_count} chunks</span>}
                </label>
              );
            })
          )}
        </div>
      </div>

      <div className="text-xs text-gray-500 text-center">Bulk delete removes chunks from ChromaDB · Export bundles markdown · $0 local · 100 files max per bulk call</div>
    </div>
  );
}
