/**
 * FileTreePanel.tsx — P2 File Tree Explorer ($0, local)
 *
 * Features:
 *  - Fetches GET /file-tree (tree + flat + stats)
 *  - Collapsible folder tree (dirs first, files alphabetically)
 *  - Search within tree (client-side filter + server /search for large repos)
 *  - Click file → dispatch savflux:open-file / navigator to Review
 *  - Breadcrumb + expand/collapse all, $0 no deps
 */

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "../api";
import { Folder, FolderOpen, FileCode, Search, ChevronRight, ChevronDown, X, Expand, Minimize2 } from "lucide-react";

type TreeNode = {
  name: string;
  path: string;
  type: "dir" | "file";
  children?: TreeNode[];
  language?: string;
  source?: string;
  file_count?: number;
  languages?: Record<string, number>;
  chunk_count?: number;
};

export default function FileTreePanel({ onOpenFile }: { onOpenFile?: (source: string) => void }) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [flat, setFlat] = useState<TreeNode[]>([]);
  const [totalFiles, setTotalFiles] = useState(0);
  const [totalDirs, setTotalDirs] = useState(0);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(["", "src"]));
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchTree = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await apiFetch("/api/v1/file-tree");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setTree(data.tree);
      setFlat(data.flat || []);
      setTotalFiles(data.total_files || 0);
      setTotalDirs(data.total_dirs || 0);
      // Auto-expand top-level dirs
      const topDirs = (data.tree?.children || []).filter((c: TreeNode) => c.type === "dir").map((c: TreeNode) => c.path);
      setExpanded((prev) => new Set([...prev, ...topDirs.slice(0, 3)]));
    } catch (e: any) {
      setError(e?.message || "Failed to load tree");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchTree(); }, [fetchTree]);

  const toggle = (path: string) => {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path); else next.add(path);
    setExpanded(next);
  };

  const expandAll = () => {
    const allDirs = new Set<string>();
    const walk = (n: TreeNode) => {
      if (n.type === "dir") {
        allDirs.add(n.path);
        n.children?.forEach(walk);
      }
    };
    if (tree) walk(tree);
    setExpanded(allDirs);
  };

  const collapseAll = () => setExpanded(new Set([""]));

  const handleOpen = (node: TreeNode) => {
    if (node.type !== "file" || !node.source) return;
    try { window.dispatchEvent(new CustomEvent("savflux:open-file", { detail: node.source })); } catch {}
    onOpenFile?.(node.source);
  };

  const filteredFlat = query
    ? flat.filter((f) => `${f.path} ${f.name} ${f.language || ""}`.toLowerCase().includes(query.toLowerCase())).slice(0, 50)
    : null;

  const renderNode = (node: TreeNode, depth: number) => {
    if (node.type === "file") {
      const isMatch = !!query && `${node.path} ${node.name}`.toLowerCase().includes(query.toLowerCase());
      if (query && !isMatch) return null;
      return (
        <div
          key={node.path}
          onClick={() => handleOpen(node)}
          className={`flex items-center gap-2 px-2 py-1.5 hover:bg-white/[0.06] cursor-pointer text-xs ${isMatch ? "bg-amber-500/10 border-l-2 border-amber-500" : "border-l-2 border-transparent"}`}
          style={{ paddingLeft: `${8 + depth * 14}px` }}
          title={node.source}
        >
          <FileCode className="w-3.5 h-3.5 text-blue-400 shrink-0" />
          <span className="truncate text-gray-300 flex-1">{node.name}</span>
          <span className="text-[10px] px-1 py-0.5 rounded bg-white/10 text-gray-400">{node.language || "text"}</span>
          {node.chunk_count !== undefined && <span className="text-[10px] text-gray-500">{node.chunk_count}</span>}
        </div>
      );
    }
    const isExpanded = expanded.has(node.path) || !!query; // auto-expand when searching
    const isMatchDir = !query || node.children?.some((c) => `${c.path}`.toLowerCase().includes(query.toLowerCase()) || c.type === "file" && `${c.path}`.toLowerCase().includes(query.toLowerCase()));
    if (query && node.path && !isMatchDir && !node.children?.some((c) => c.type === "file" && `${c.path}`.toLowerCase().includes(query.toLowerCase()))) {
      // Hide non-matching dirs when searching (unless root)
      // Keep root visible
      if (node.path !== "") return null;
    }
    return (
      <div key={node.path || "root"}>
        {node.path !== "" && (
          <div
            onClick={() => toggle(node.path)}
            className="flex items-center gap-1.5 px-2 py-1 hover:bg-white/[0.04] cursor-pointer text-xs text-gray-400"
            style={{ paddingLeft: `${8 + depth * 14}px` }}
          >
            {isExpanded ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            {isExpanded ? <FolderOpen className="w-3.5 h-3.5 text-amber-400" /> : <Folder className="w-3.5 h-3.5 text-amber-500/80" />}
            <span className="truncate font-medium text-gray-300">{node.name}</span>
            <span className="text-[10px] text-gray-500 ml-auto">{node.file_count} files</span>
          </div>
        )}
        {isExpanded && (
          <div>
            {node.children?.map((c) => renderNode(c, node.path === "" ? 0 : depth + 1))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Folder className="w-5 h-5 text-amber-400" />
          <h2 className="text-base font-bold text-amber-300">File Explorer</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-200">$0</span>
          {loading && <span className="text-xs text-gray-500">loading…</span>}
        </div>
        <div className="flex items-center gap-1">
          <span className="text-xs text-gray-500">{totalFiles} files · {totalDirs} dirs</span>
          <button onClick={expandAll} className="p-1 rounded hover:bg-white/10 text-gray-400" title="Expand all"><Expand className="w-3.5 h-3.5" /></button>
          <button onClick={collapseAll} className="p-1 rounded hover:bg-white/10 text-gray-400" title="Collapse all"><Minimize2 className="w-3.5 h-3.5" /></button>
          <button onClick={fetchTree} className="text-xs px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-gray-200">Refresh</button>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-black/30 border border-white/10">
        <Search className="w-3.5 h-3.5 text-gray-500" />
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search files (path, name, language)…" className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 focus:outline-none" />
        {query && <button onClick={() => setQuery("")} className="p-0.5 hover:bg-white/10 rounded"><X className="w-3 h-3 text-gray-500" /></button>}
        {query && <span className="text-xs text-gray-500">{filteredFlat?.length || 0} matches</span>}
      </div>

      {query && filteredFlat && (
        <div className="rounded-lg border border-white/10 bg-white/[0.03] p-2 space-y-1 max-h-40 overflow-y-auto">
          <div className="text-xs text-gray-500 mb-1">Matches for “{query}”</div>
          {filteredFlat.length === 0 ? <div className="text-xs text-gray-500">No matches</div> : filteredFlat.map((f) => (
            <div key={f.path} onClick={() => handleOpen(f)} className="flex items-center gap-2 text-xs px-2 py-1 hover:bg-white/10 rounded cursor-pointer">
              <FileCode className="w-3 h-3 text-blue-400" /> <span className="truncate text-gray-300">{f.path}</span> <span className="text-[10px] text-gray-500">{f.language}</span>
            </div>
          ))}
        </div>
      )}

      <div className="rounded-lg border border-white/10 bg-white/[0.03] overflow-hidden max-h-[60vh] overflow-y-auto">
        {!tree ? (
          <div className="text-sm text-gray-500 p-6 text-center">No files indexed yet — ingest a repo to see tree.</div>
        ) : (
          renderNode(tree, 0)
        )}
      </div>

      <div className="text-xs text-gray-500 text-center">Tree built from Chroma metadatas · click file to open in Review (dispatches <code className="text-gray-400">savflux:open-file</code>) · $0</div>
    </div>
  );
}
