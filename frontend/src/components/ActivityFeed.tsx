/**
 * ActivityFeed.tsx — P2 Activity Feed ($0, aggregated local)
 *
 * Unified timeline from:
 *  - Prompt history/saved, snippets, shares, analytics snapshots, ingests
 * GET /activity?limit=&kind= → timeline sorted ts desc
 * DELETE /activity?kind= → clear
 */

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "../api";
import { Activity, Clock, Trash2, Filter, Share2, Code2, Bookmark, BarChart3, Database, FileText } from "lucide-react";

type ActivityItem = {
  id: string;
  kind: string;
  title: string;
  detail: string;
  sub_kind: string;
  ts: number;
  meta?: any;
};

function timeAgo(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function kindIcon(kind: string) {
  switch (kind) {
    case "prompt_history": return <Clock className="w-3.5 h-3.5 text-cyan-400" />;
    case "prompt_saved": return <Bookmark className="w-3.5 h-3.5 text-amber-400" />;
    case "snippet": return <Code2 className="w-3.5 h-3.5 text-violet-400" />;
    case "share": return <Share2 className="w-3.5 h-3.5 text-emerald-400" />;
    case "analytics": return <BarChart3 className="w-3.5 h-3.5 text-indigo-400" />;
    case "ingest": return <Database className="w-3.5 h-3.5 text-blue-400" />;
    default: return <FileText className="w-3.5 h-3.5 text-gray-400" />;
  }
}

function kindColor(kind: string): string {
  switch (kind) {
    case "prompt_history": return "bg-cyan-500/15 text-cyan-200 border-cyan-500/20";
    case "prompt_saved": return "bg-amber-500/15 text-amber-200 border-amber-500/20";
    case "snippet": return "bg-violet-500/15 text-violet-200 border-violet-500/20";
    case "share": return "bg-emerald-500/15 text-emerald-200 border-emerald-500/20";
    case "analytics": return "bg-indigo-500/15 text-indigo-200 border-indigo-500/20";
    case "ingest": return "bg-blue-500/15 text-blue-200 border-blue-500/20";
    default: return "bg-white/10 text-gray-300";
  }
}

export default function ActivityFeed() {
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [kind, setKind] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchFeed = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params = new URLSearchParams();
      params.set("limit", "50");
      if (kind) params.set("kind", kind);
      const res = await apiFetch(`/api/v1/activity?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setItems(data.items || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load activity");
    } finally {
      setLoading(false);
    }
  }, [kind]);

  useEffect(() => { fetchFeed(); }, [fetchFeed]);

  const handleClear = async () => {
    if (!confirm(kind ? `Clear ${kind} activity?` : "Clear ALL activity? This will delete prompts/snippets/shares/analytics.")) return;
    try {
      const params = new URLSearchParams();
      if (kind) params.set("kind", kind);
      const res = await apiFetch(`/api/v1/activity?${params.toString()}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fetchFeed();
    } catch (e: any) { setError(e?.message || "Clear failed"); }
  };

  const counts = {
    total: items.length,
    prompt_history: items.filter((i) => i.kind === "prompt_history").length,
    prompt_saved: items.filter((i) => i.kind === "prompt_saved").length,
    snippet: items.filter((i) => i.kind === "snippet").length,
    share: items.filter((i) => i.kind === "share").length,
    analytics: items.filter((i) => i.kind === "analytics").length,
    ingest: items.filter((i) => i.kind === "ingest").length,
  };

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity className="w-5 h-5 text-teal-400" />
          <h2 className="text-base font-bold text-teal-300">Activity Feed</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-teal-500/20 text-teal-200">$0</span>
          {loading && <span className="text-xs text-gray-500">loading…</span>}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{items.length} items</span>
          <button onClick={fetchFeed} className="text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Refresh</button>
          <button onClick={handleClear} className="text-xs px-2 py-1.5 rounded bg-red-500/15 hover:bg-red-500/25 text-red-300 flex items-center gap-1">
            <Trash2 className="w-3 h-3" /> Clear {kind || "all"}
          </button>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-1 text-xs text-gray-400"><Filter className="w-3 h-3" /> Filter:</div>
        {[
          { id: "", label: `All (${counts.total})` },
          { id: "prompt_history", label: `History (${items.filter((i)=>i.kind==="prompt_history").length})` },
          { id: "prompt_saved", label: `Prompts (${counts.prompt_saved})` },
          { id: "snippet", label: `Snippets (${counts.snippet})` },
          { id: "share", label: `Shares (${counts.share})` },
          { id: "analytics", label: `Analytics (${counts.analytics})` },
          { id: "ingest", label: `Ingest (${counts.ingest})` },
        ].map((f) => (
          <button
            key={f.id}
            onClick={() => setKind(f.id)}
            className={`text-xs px-2 py-1 rounded-full border ${kind === f.id ? "bg-teal-500/20 text-teal-200 border-teal-500/30" : "bg-white/5 text-gray-400 border-white/10 hover:bg-white/10"}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Timeline */}
      {items.length === 0 && !loading ? (
        <div className="text-sm text-gray-400 border border-dashed border-white/10 rounded p-6 text-center">
          No activity yet — chat, save prompts/snippets, share, or index a repo to see timeline.
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((it) => (
            <div key={it.id} className="rounded-lg border border-white/10 bg-white/[0.03] p-3 hover:bg-white/[0.05] transition-colors">
              <div className="flex items-start gap-2">
                <div className={`mt-0.5 p-1 rounded-full border ${kindColor(it.kind)}`}>{kindIcon(it.kind)}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-1.5 py-0.5 rounded border ${kindColor(it.kind)}`}>{it.kind}</span>
                    {it.sub_kind && <span className="text-xs text-gray-500">{it.sub_kind}</span>}
                    <span className="text-xs text-gray-500 ml-auto flex items-center gap-1"><Clock className="w-3 h-3" />{timeAgo(it.ts)} · {new Date(it.ts * 1000).toLocaleString()}</span>
                  </div>
                  <div className="text-sm font-medium text-white truncate mt-1">{it.title}</div>
                  {it.detail && <div className="text-xs text-gray-400 mt-1 line-clamp-2 break-words">{it.detail}</div>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="text-xs text-gray-500 text-center">
        Aggregates <code className="text-gray-400">prompt_library.json</code> · <code className="text-gray-400">snippet_vault.json</code> · <code className="text-gray-400">analytics_history.jsonl</code> · shares · ingests · $0
      </div>
    </div>
  );
}
