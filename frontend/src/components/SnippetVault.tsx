/**
 * SnippetVault.tsx — P2 Snippet Vault ($0, local file)
 *
 * Save code snippets (title, code, language, tags, source) → POST /snippets
 * Search/filter (fuzzy, tag, language, starred) → GET /snippets?q=
 * Star toggle, Use (copy + increment), Delete
 *
 * All $0 — local file `chroma_data/snippet_vault.json`, no LLM.
 * Fired via `savflux:copy-snippet` event for MessageBubble copy buttons.
 */

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "../api";
import { Code2, Star, Trash2, Search, Plus, Tag, Clock, Copy, X, FileCode } from "lucide-react";

type Snippet = {
  id: string;
  title: string;
  code: string;
  language: string;
  tags: string[];
  source?: string;
  starred: boolean;
  created_at: number;
  use_count: number;
  last_used?: number;
};

function timeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

const LANG_OPTIONS = ["text", "python", "javascript", "typescript", "tsx", "java", "go", "rust", "sql", "bash", "yaml", "json", "css", "html"];

export default function SnippetVault() {
  const [snippets, setSnippets] = useState<Snippet[]>([]);
  const [query, setQuery] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [langFilter, setLangFilter] = useState("");
  const [starredOnly, setStarredOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form
  const [showForm, setShowForm] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newCode, setNewCode] = useState("");
  const [newLang, setNewLang] = useState("python");
  const [newTags, setNewTags] = useState("");
  const [newSource, setNewSource] = useState("");

  const fetchSnippets = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params = new URLSearchParams();
      if (query) params.set("q", query);
      if (tagFilter) params.set("tag", tagFilter);
      if (langFilter) params.set("language", langFilter);
      if (starredOnly) params.set("starred", "true");
      params.set("limit", "100");
      const res = await apiFetch(`/api/v1/snippets?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setSnippets(data.snippets || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load snippets");
    } finally {
      setLoading(false);
    }
  }, [query, tagFilter, langFilter, starredOnly]);

  useEffect(() => { fetchSnippets(); }, [fetchSnippets]);

  // Listen for copy from chat/review — auto-fill form with code
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { code?: string; language?: string; source?: string };
      if (detail?.code) {
        setNewCode(detail.code);
        if (detail.language) setNewLang(detail.language);
        if (detail.source) setNewSource(detail.source);
        setShowForm(true);
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    };
    window.addEventListener("savflux:snippet-save" as any, handler);
    return () => window.removeEventListener("savflux:snippet-save" as any, handler);
  }, []);

  const handleCreate = async () => {
    if (!newCode.trim()) return;
    try {
      const tags = newTags.split(",").map((t) => t.trim()).filter(Boolean);
      const res = await apiFetch("/api/v1/snippets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: newCode, title: newTitle || undefined, language: newLang, tags, source: newSource || undefined }),
      });
      if (!res.ok) {
        const j = await res.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${res.status}`);
      }
      setNewTitle(""); setNewCode(""); setNewTags(""); setNewSource("");
      setShowForm(false);
      fetchSnippets();
    } catch (e: any) {
      setError(e?.message || "Failed to save");
    }
  };

  const handleUse = async (s: Snippet) => {
    try { await apiFetch(`/api/v1/snippets/${s.id}/use`, { method: "POST" }); } catch {}
    try { await navigator.clipboard.writeText(s.code); } catch {}
    // Also dispatch for chat input if someone wants
    try { window.dispatchEvent(new CustomEvent("savflux:use-prompt", { detail: s.code })); } catch {}
    fetchSnippets();
  };

  const handleStar = async (s: Snippet) => {
    try {
      const res = await apiFetch(`/api/v1/snippets/${s.id}/star`, { method: "POST" });
      if (res.ok) fetchSnippets();
    } catch {}
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete snippet?")) return;
    try {
      const res = await apiFetch(`/api/v1/snippets/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSnippets((prev) => prev.filter((x) => x.id !== id));
    } catch (e: any) { setError(e?.message || "Delete failed"); }
  };

  const handleCopy = async (code: string) => {
    try { await navigator.clipboard.writeText(code); } catch {}
  };

  const allTags = Array.from(new Set(snippets.flatMap((s) => s.tags))).slice(0, 20);
  const allLangs = Array.from(new Set(snippets.map((s) => s.language).filter(Boolean))).slice(0, 20);

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Code2 className="w-5 h-5 text-violet-400" />
          <h2 className="text-base font-bold text-violet-300">Snippet Vault</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-violet-500/20 text-violet-200">$0</span>
          {loading && <span className="text-xs text-gray-500">loading…</span>}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{snippets.length} saved</span>
          <button onClick={() => setShowForm((v) => !v)} className="text-xs px-2.5 py-1.5 rounded bg-violet-500/20 hover:bg-violet-500/30 text-violet-200 flex items-center gap-1">
            <Plus className="w-3 h-3" /> {showForm ? "Cancel" : "New snippet"}
          </button>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      {showForm && (
        <div className="rounded-lg border border-white/10 bg-white/[0.04] p-3 space-y-2">
          <div className="grid md:grid-cols-3 gap-2">
            <input value={newTitle} onChange={(e) => setNewTitle(e.target.value)} placeholder="Title (auto from first line)" className="md:col-span-2 px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-violet-500/50" />
            <select value={newLang} onChange={(e) => setNewLang(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white">
              {LANG_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </div>
          <textarea value={newCode} onChange={(e) => setNewCode(e.target.value)} placeholder="Paste code snippet here..." rows={6} className="w-full px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm font-mono text-white placeholder-gray-500 focus:outline-none focus:border-violet-500/50" />
          <div className="grid md:grid-cols-2 gap-2">
            <input value={newTags} onChange={(e) => setNewTags(e.target.value)} placeholder="Tags comma-separated" className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-violet-500/50" />
            <input value={newSource} onChange={(e) => setNewSource(e.target.value)} placeholder="Source (optional, e.g. src/auth.py:42)" className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-violet-500/50" />
          </div>
          <div className="flex justify-between items-center">
            <span className="text-xs text-gray-500">{newCode.length}/20000 · {newCode.split("\n").length} lines</span>
            <button onClick={handleCreate} disabled={!newCode.trim()} className="text-xs px-3 py-1.5 rounded bg-violet-500 hover:bg-violet-400 text-white font-semibold disabled:opacity-40">Save snippet</button>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-2 flex-1 min-w-[200px] px-2 py-1.5 rounded bg-black/30 border border-white/10">
          <Search className="w-3.5 h-3.5 text-gray-500" />
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search snippets (title, code, tags)…" className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 focus:outline-none" />
          {query && <button onClick={() => setQuery("")} className="p-0.5 hover:bg-white/10 rounded"><X className="w-3 h-3 text-gray-500" /></button>}
        </div>
        <select value={langFilter} onChange={(e) => setLangFilter(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-xs text-white">
          <option value="">all languages</option>
          {(allLangs.length ? allLangs : LANG_OPTIONS.slice(0, 8)).map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <select value={tagFilter} onChange={(e) => setTagFilter(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-xs text-white">
          <option value="">all tags</option>
          {allTags.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <label className="flex items-center gap-1 text-xs text-gray-400">
          <input type="checkbox" checked={starredOnly} onChange={(e) => setStarredOnly(e.target.checked)} className="accent-violet-500" /> Starred
        </label>
        <button onClick={fetchSnippets} className="text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Refresh</button>
      </div>

      {/* List */}
      {snippets.length === 0 && !loading ? (
        <div className="text-sm text-gray-400 border border-dashed border-white/10 rounded p-6 text-center">
          No snippets yet — save your first snippet above. Copies from chat/review can be saved via the <code className="text-gray-300">Save to Vault</code> button. Persists at <code className="text-gray-300">chroma_data/snippet_vault.json</code>.
        </div>
      ) : (
        <div className="space-y-2">
          {snippets.map((s) => (
            <div key={s.id} className="rounded-lg border border-white/10 bg-white/[0.03] p-3 hover:bg-white/[0.05] transition-colors">
              <div className="flex items-start justify-between gap-2 mb-1">
                <div className="flex items-center gap-2 min-w-0">
                  <FileCode className="w-3.5 h-3.5 text-violet-400 shrink-0" />
                  <span className="text-sm font-semibold text-white truncate">{s.title}</span>
                  <span className="text-xs px-1.5 py-0.5 rounded bg-white/10 text-gray-300">{s.language}</span>
                  {s.starred && <Star className="w-3.5 h-3.5 text-amber-400 fill-amber-400" />}
                  {s.use_count > 0 && <span className="text-xs text-violet-300">{s.use_count} uses</span>}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button onClick={() => handleStar(s)} title={s.starred ? "Unstar" : "Star"} className={`p-1 rounded ${s.starred ? "bg-amber-500/20 text-amber-300" : "bg-white/5 hover:bg-white/10 text-gray-400"}`}>
                    <Star className={`w-3.5 h-3.5 ${s.starred ? "fill-amber-400" : ""}`} />
                  </button>
                  <button onClick={() => handleCopy(s.code)} title="Copy" className="p-1 rounded bg-white/5 hover:bg-white/10 text-gray-400"><Copy className="w-3.5 h-3.5" /></button>
                  <button onClick={() => handleUse(s)} className="text-xs px-2 py-1 rounded bg-violet-500 hover:bg-violet-400 text-white font-semibold">Copy & Use</button>
                  <button onClick={() => handleDelete(s.id)} className="p-1 rounded bg-white/5 hover:bg-red-500/20 text-gray-400 hover:text-red-300"><Trash2 className="w-3 h-3" /></button>
                </div>
              </div>
              <pre className="text-xs font-mono bg-black/40 border border-white/5 rounded p-2 overflow-x-auto max-h-64 overflow-y-auto text-gray-200 whitespace-pre-wrap break-words">{s.code}</pre>
              <div className="flex flex-wrap gap-1.5 mt-2 items-center">
                {s.tags.map((t) => <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-violet-500/15 text-violet-200 flex items-center gap-1"><Tag className="w-3 h-3" />{t}</span>)}
                {s.source && <span className="text-xs text-gray-500">{s.source}</span>}
                <span className="text-xs text-gray-500 flex items-center gap-1 ml-auto"><Clock className="w-3 h-3" />{timeAgo(s.created_at)} {s.last_used ? `· used ${timeAgo(s.last_used)}` : ""}</span>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="text-xs text-gray-500 text-center">Vault at <code className="text-gray-400">chroma_data/snippet_vault.json</code> (500 cap) · star + use_count ranking · $0</div>
    </div>
  );
}
