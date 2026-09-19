/**
 * DiffViewer.tsx — P2 Diff Viewer ($0, difflib backend)
 *
 * Features:
 *  - Two file selectors (dropdowns from indexed files)
 *  - Fetch GET /diff/file?source= for preview + POST /diff/compare for unified diff
 *  - Unified diff with syntax (added green, removed red, hunk header cyan)
 *  - Split toggle (side-by-side via two <pre>), context lines 0-10
 *  - Stats (added/removed, similarity), Copy diff, Swap, $0 no deps
 */

import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { GitCompare, ArrowLeftRight, Copy, Search, FileCode } from "lucide-react";

type IndexedFile = { source: string; file_name: string; language: string };

export default function DiffViewer() {
  const [files, setFiles] = useState<IndexedFile[]>([]);
  const [sourceA, setSourceA] = useState("");
  const [sourceB, setSourceB] = useState("");
  const [context, setContext] = useState(3);
  const [unified, setUnified] = useState("");
  const [stats, setStats] = useState<{ added: number; removed: number; similarity: number; a_lines: number; b_lines: number } | null>(null);
  const [aContent, setAContent] = useState("");
  const [bContent, setBContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [split, setSplit] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const res = await apiFetch("/api/v1/chat/indexed-files");
        if (res.ok) {
          const data = await res.json();
          setFiles(data.files || []);
          if (data.files?.length >= 2) {
            setSourceA(data.files[0].source);
            setSourceB(data.files[1].source);
          } else if (data.files?.length === 1) {
            setSourceA(data.files[0].source);
          }
        }
      } catch {}
      // Also try file-tree flat for more files
      try {
        const r2 = await apiFetch("/api/v1/file-tree");
        if (r2.ok) {
          const j2 = await r2.json();
          const flat = (j2.flat || []).map((f: any) => ({ source: f.source, file_name: f.name, language: f.language }));
          if (flat.length) setFiles((prev) => (prev.length ? prev : flat));
        }
      } catch {}
    })();
  }, []);

  const handleCompare = async () => {
    if (!sourceA || !sourceB) { setError("Select two different files"); return; }
    if (sourceA === sourceB) { setError("Pick two different files"); return; }
    try {
      setLoading(true);
      setError(null);
      const res = await apiFetch("/api/v1/diff/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_a: sourceA, source_b: sourceB, context }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setUnified(data.unified_diff || "");
      setStats({ added: data.added, removed: data.removed, similarity: data.similarity, a_lines: data.a_lines, b_lines: data.b_lines });
      setAContent(data.a_content || "");
      setBContent(data.b_content || "");
    } catch (e: any) {
      setError(e?.message || "Diff failed");
      setUnified("");
      setStats(null);
    } finally {
      setLoading(false);
    }
  };

  const handleSwap = () => {
    const a = sourceA;
    setSourceA(sourceB);
    setSourceB(a);
  };

  const handleCopy = async () => {
    if (!unified) return;
    try { await navigator.clipboard.writeText(unified); } catch {}
  };

  const filteredFiles = query
    ? files.filter((f) => `${f.source} ${f.file_name}`.toLowerCase().includes(query.toLowerCase())).slice(0, 100)
    : files.slice(0, 100);

  const renderUnified = () => {
    if (!unified) return <div className="text-xs text-gray-500 p-6 text-center">No diff yet — pick two files and hit Compare.</div>;
    const lines = unified.split("\n");
    return (
      <pre className="text-xs font-mono bg-black/40 border border-white/5 rounded p-3 overflow-x-auto whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto">
        {lines.map((line, idx) => {
          let cls = "text-gray-300";
          if (line.startsWith("+++") || line.startsWith("---")) cls = "text-gray-500 font-bold";
          else if (line.startsWith("@@")) cls = "text-cyan-400 bg-cyan-500/10";
          else if (line.startsWith("+")) cls = "text-emerald-300 bg-emerald-500/10";
          else if (line.startsWith("-")) cls = "text-red-300 bg-red-500/10";
          return <div key={idx} className={cls}>{line || " "}</div>;
        })}
      </pre>
    );
  };

  const renderSplit = () => {
    if (!aContent || !bContent) return renderUnified();
    return (
      <div className="grid md:grid-cols-2 gap-3">
        <div>
          <div className="text-xs font-semibold text-gray-400 mb-1 flex items-center gap-1"><FileCode className="w-3 h-3" />{sourceA} · {stats?.a_lines} lines</div>
          <pre className="text-xs font-mono bg-black/40 border border-white/5 rounded p-3 overflow-x-auto whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto text-gray-300">{aContent}</pre>
        </div>
        <div>
          <div className="text-xs font-semibold text-gray-400 mb-1 flex items-center gap-1"><FileCode className="w-3 h-3" />{sourceB} · {stats?.b_lines} lines</div>
          <pre className="text-xs font-mono bg-black/40 border border-white/5 rounded p-3 overflow-x-auto whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto text-gray-300">{bContent}</pre>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4 max-w-4xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitCompare className="w-5 h-5 text-pink-400" />
          <h2 className="text-base font-bold text-pink-300">Diff Viewer</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-pink-500/20 text-pink-200">$0</span>
          {loading && <span className="text-xs text-gray-500">diffing…</span>}
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs text-gray-400 flex items-center gap-1">
            Context
            <select value={context} onChange={(e) => setContext(parseInt(e.target.value, 10))} className="px-1 py-1 rounded bg-black/30 border border-white/10 text-xs text-white">
              {[0,1,2,3,5,10].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <label className="text-xs text-gray-400 flex items-center gap-1">
            <input type="checkbox" checked={split} onChange={(e) => setSplit(e.target.checked)} className="accent-pink-500" /> Split
          </label>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      {/* File pickers */}
      <div className="rounded-lg border border-white/10 bg-white/[0.04] p-3 space-y-3">
        <div className="flex items-center gap-2">
          <Search className="w-3.5 h-3.5 text-gray-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter files…" className="flex-1 bg-black/30 border border-white/10 rounded px-2 py-1 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-pink-500/50" />
          <span className="text-xs text-gray-500">{files.length} files</span>
        </div>
        <div className="grid md:grid-cols-[1fr_auto_1fr] gap-2 items-end">
          <div>
            <label className="text-xs text-gray-400">File A</label>
            <select value={sourceA} onChange={(e) => setSourceA(e.target.value)} className="w-full mt-1 px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white">
              <option value="">— pick file —</option>
              {filteredFiles.map((f) => <option key={f.source} value={f.source}>{f.file_name} — {f.source.slice(0, 60)}</option>)}
            </select>
          </div>
          <button onClick={handleSwap} title="Swap A ↔ B" className="p-2 rounded bg-white/10 hover:bg-white/20 text-gray-300 self-center mt-5">
            <ArrowLeftRight className="w-4 h-4" />
          </button>
          <div>
            <label className="text-xs text-gray-400">File B</label>
            <select value={sourceB} onChange={(e) => setSourceB(e.target.value)} className="w-full mt-1 px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white">
              <option value="">— pick file —</option>
              {filteredFiles.map((f) => <option key={f.source} value={f.source}>{f.file_name} — {f.source.slice(0, 60)}</option>)}
            </select>
          </div>
        </div>
        <div className="flex justify-between items-center">
          <div className="text-xs text-gray-500">
            {stats ? <span className="text-emerald-300">+{stats.added}</span> : null}
            {stats ? <span className="text-red-300"> −{stats.removed}</span> : null}
            {stats ? <span className="text-gray-400"> · similarity {Math.round(stats.similarity * 100)}% · A {stats.a_lines} lines · B {stats.b_lines} lines</span> : null}
          </div>
          <div className="flex gap-2">
            <button onClick={handleCopy} disabled={!unified} className="text-xs px-2.5 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200 disabled:opacity-40 flex items-center gap-1"><Copy className="w-3 h-3" /> Copy diff</button>
            <button onClick={handleCompare} disabled={!sourceA || !sourceB || loading} className="text-xs px-3 py-1.5 rounded bg-pink-500 hover:bg-pink-400 text-white font-semibold disabled:opacity-40 flex items-center gap-1"><GitCompare className="w-3 h-3" /> Compare</button>
          </div>
        </div>
      </div>

      {/* Diff output */}
      <div className="rounded-lg border border-white/10 bg-white/[0.03] p-3">
        {split ? renderSplit() : renderUnified()}
      </div>

      <div className="text-xs text-gray-500 text-center">Diff via Python <code className="text-gray-400">difflib.unified_diff</code> · $0 local · context 0–10 · similarity via <code className="text-gray-400">SequenceMatcher</code></div>
    </div>
  );
}
