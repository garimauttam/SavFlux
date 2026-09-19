/**
 * SlashCommandsPanel.tsx — P2 Slash Commands & Quick Actions ($0, local)
 *
 * Features:
 *  - Lists GET /slash/commands with search
 *  - Expand POST /slash/expand to preview prompt
 *  - Copy / Use (dispatch savflux:use-prompt)
 *  - $0 no deps
 */

import { useEffect, useState } from "react";
import { apiFetch } from "../api";
import { Terminal, Search, Copy, Zap, X } from "lucide-react";

type SlashCmd = {
  command: string;
  name: string;
  description: string;
  template: string;
  args_hint: string;
  kind: string;
  examples: string[];
};

export default function SlashCommandsPanel({ onUse }: { onUse?: (prompt: string) => void }) {
  const [cmds, setCmds] = useState<SlashCmd[]>([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<SlashCmd | null>(null);
  const [args, setArgs] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchCmds = async (q: string) => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      params.set("limit", "20");
      const res = await apiFetch(`/api/v1/slash/commands?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCmds(data.commands || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load commands");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchCmds(""); }, []);
  useEffect(() => {
    const id = setTimeout(() => fetchCmds(query), 250);
    return () => clearTimeout(id);
  }, [query]);

  const handleExpand = async (cmd: SlashCmd) => {
    setSelected(cmd);
    try {
      const res = await apiFetch("/api/v1/slash/expand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd.command, args }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setPreview(data.prompt);
    } catch (e: any) {
      setPreview(`Error: ${e?.message}`);
    }
  };

  const handleUse = async () => {
    if (!preview) return;
    try { await navigator.clipboard.writeText(preview); } catch {}
    try { window.dispatchEvent(new CustomEvent("savflux:use-prompt", { detail: preview })); } catch {}
    onUse?.(preview);
  };

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center gap-2">
        <Terminal className="w-5 h-5 text-emerald-400" />
        <h2 className="text-base font-bold text-emerald-300">Slash Commands</h2>
        <span className="text-xs px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-200">$0</span>
        {loading && <span className="text-xs text-gray-500">loading…</span>}
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-black/30 border border-white/10">
        <Search className="w-3.5 h-3.5 text-gray-500" />
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search commands (/explain, /review, /test…)" className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 focus:outline-none" />
        {query && <button onClick={() => setQuery("")} className="p-0.5 hover:bg-white/10 rounded"><X className="w-3 h-3 text-gray-500" /></button>}
      </div>

      <div className="grid md:grid-cols-2 gap-3">
        <div className="space-y-1 max-h-[60vh] overflow-y-auto pr-1">
          {cmds.map((c) => (
            <button
              key={c.command}
              onClick={() => handleExpand(c)}
              className={`w-full text-left px-3 py-2 rounded-lg border transition-colors ${selected?.command === c.command ? "bg-emerald-500/15 border-emerald-500/30 text-emerald-200" : "bg-white/[0.03] border-white/10 hover:bg-white/[0.06] text-gray-300"}`}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm font-bold text-emerald-400">{c.command}</span>
                <span className="text-xs px-1 py-0.5 rounded bg-white/10 text-gray-400">{c.kind}</span>
              </div>
              <div className="text-xs text-gray-400 mt-1">{c.description}</div>
              <div className="text-xs text-gray-500 font-mono mt-1">{c.args_hint}</div>
              <div className="text-xs text-gray-600 mt-1 truncate">{c.examples[0]}</div>
            </button>
          ))}
          {cmds.length === 0 && !loading && <div className="text-xs text-gray-500 p-3">No commands match “{query}”</div>}
        </div>

        <div className="rounded-lg border border-white/10 bg-white/[0.04] p-3 space-y-2">
          {!selected ? (
            <div className="text-sm text-gray-500 text-center py-6">Select a command to preview</div>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <Zap className="w-4 h-4 text-emerald-400" />
                <span className="font-mono text-sm font-bold text-white">{selected.command}</span>
                <span className="text-xs text-gray-500">{selected.description}</span>
              </div>
              <div className="text-xs text-gray-400">Args</div>
              <input
                value={args}
                onChange={(e) => setArgs(e.target.value)}
                placeholder={selected.args_hint || "args..."}
                className="w-full px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-emerald-500/50"
              />
              <div className="flex gap-2">
                <button onClick={() => handleExpand(selected)} className="flex-1 text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Preview</button>
                <button onClick={handleUse} disabled={!preview} className="flex-1 text-xs px-2 py-1.5 rounded bg-emerald-500 hover:bg-emerald-400 text-white font-semibold disabled:opacity-40 flex items-center justify-center gap-1"><Copy className="w-3 h-3" /> Use (copy & fill chat)</button>
              </div>
              {preview && (
                <pre className="text-xs font-mono bg-black/40 border border-white/5 rounded p-2 whitespace-pre-wrap break-words max-h-64 overflow-y-auto text-gray-200">{preview}</pre>
              )}
              <div className="text-xs text-gray-500">
                <div>Examples:</div>
                {selected.examples.map((ex) => <div key={ex} className="font-mono text-gray-400">· {ex}</div>)}
              </div>
            </>
          )}
        </div>
      </div>

      <div className="text-xs text-gray-500 text-center">Type <code className="text-gray-300">/explain</code> in chat for autocomplete · 12 commands · $0 prompt templates · dispatch <code className="text-gray-400">savflux:use-prompt</code></div>
    </div>
  );
}
