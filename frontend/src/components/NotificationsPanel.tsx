/**
 * NotificationsPanel.tsx — P2 Notifications Center ($0, local file)
 *
 * Features:
 *  - List notifications (filter by kind, unread only)
 *  - Unread badge + mark read / mark all read
 *  - Create test notification (for demo)
 *  - Delete / clear, $0 no deps
 *  - Polls every 15s + on window focus
 */

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "../api";
import { Bell, CheckCheck, Trash2, Filter, X, AlertCircle, CheckCircle, Info, AlertTriangle } from "lucide-react";

type Notif = {
  id: string;
  title: string;
  message: string;
  kind: string;
  level: string;
  read: boolean;
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

function levelIcon(level: string) {
  switch (level) {
    case "success": return <CheckCircle className="w-3.5 h-3.5 text-emerald-400" />;
    case "warning": return <AlertTriangle className="w-3.5 h-3.5 text-amber-400" />;
    case "error": return <AlertCircle className="w-3.5 h-3.5 text-red-400" />;
    default: return <Info className="w-3.5 h-3.5 text-blue-400" />;
  }
}

function levelColor(level: string): string {
  switch (level) {
    case "success": return "border-emerald-500/20 bg-emerald-500/10";
    case "warning": return "border-amber-500/20 bg-amber-500/10";
    case "error": return "border-red-500/20 bg-red-500/10";
    default: return "border-blue-500/20 bg-blue-500/10";
  }
}

const KINDS = ["general","ingest","review","write","chat","health","watcher","bulk","prompt","snippet","activity","file_tree","diff","explorer","system"];

export default function NotificationsPanel() {
  const [notifs, setNotifs] = useState<Notif[]>([]);
  const [kind, setKind] = useState<string>("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchNotifs = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const params = new URLSearchParams();
      params.set("limit", "100");
      if (kind) params.set("kind", kind);
      if (unreadOnly) params.set("unread_only", "true");
      const res = await apiFetch(`/api/v1/notifications?${params.toString()}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setNotifs(data.notifications || []);
    } catch (e: any) {
      setError(e?.message || "Failed to load notifications");
    } finally {
      setLoading(false);
    }
  }, [kind, unreadOnly]);

  useEffect(() => {
    fetchNotifs();
    const id = setInterval(fetchNotifs, 15000);
    const onFocus = () => fetchNotifs();
    window.addEventListener("focus", onFocus);
    return () => { clearInterval(id); window.removeEventListener("focus", onFocus); };
  }, [fetchNotifs]);

  const unread = notifs.filter((n) => !n.read).length;

  const handleMarkRead = async (id: string, read: boolean) => {
    try {
      const res = await apiFetch(`/api/v1/notifications/${id}/read`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ read }) });
      if (res.ok) fetchNotifs();
    } catch {}
  };

  const handleMarkAllRead = async () => {
    try {
      const res = await apiFetch("/api/v1/notifications/read-all", { method: "POST" });
      if (res.ok) fetchNotifs();
    } catch {}
  };

  const handleDelete = async (id: string) => {
    try {
      const res = await apiFetch(`/api/v1/notifications/${id}`, { method: "DELETE" });
      if (res.ok) setNotifs((prev) => prev.filter((n) => n.id !== id));
    } catch {}
  };

  const handleClear = async () => {
    if (!confirm(kind ? `Clear ${kind} notifications?` : "Clear ALL notifications?")) return;
    try {
      const params = new URLSearchParams();
      if (kind) params.set("kind", kind);
      const res = await apiFetch(`/api/v1/notifications?${params.toString()}`, { method: "DELETE" });
      if (res.ok) fetchNotifs();
    } catch {}
  };

  const handleCreateTest = async () => {
    try {
      const res = await apiFetch("/api/v1/notifications", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "Test notification", message: "This is a demo notification — $0 local", kind: "system", level: "info" }),
      });
      if (res.ok) fetchNotifs();
    } catch {}
  };

  return (
    <div className="space-y-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bell className="w-5 h-5 text-blue-400" />
          <h2 className="text-base font-bold text-blue-300">Notifications</h2>
          <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-200">$0</span>
          {unread > 0 && <span className="text-xs px-1.5 py-0.5 rounded-full bg-red-500 text-white font-bold">{unread} unread</span>}
          {loading && <span className="text-xs text-gray-500">loading…</span>}
        </div>
        <div className="flex items-center gap-2">
          <button onClick={handleCreateTest} className="text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Test</button>
          <button onClick={handleMarkAllRead} disabled={!unread} className="text-xs px-2 py-1.5 rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-200 disabled:opacity-40 flex items-center gap-1"><CheckCheck className="w-3 h-3" /> Mark all read</button>
          <button onClick={fetchNotifs} className="text-xs px-2 py-1.5 rounded bg-white/10 hover:bg-white/20 text-gray-200">Refresh</button>
        </div>
      </div>

      {error && <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/20 rounded p-2">{error}</div>}

      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <div className="flex items-center gap-1 text-xs text-gray-400"><Filter className="w-3 h-3" /> Filter:</div>
        <select value={kind} onChange={(e) => setKind(e.target.value)} className="px-2 py-1.5 rounded bg-black/30 border border-white/10 text-xs text-white">
          <option value="">all kinds</option>
          {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <label className="flex items-center gap-1 text-xs text-gray-400">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} className="accent-blue-500" /> Unread only
        </label>
        <button onClick={handleClear} className="ml-auto text-xs px-2 py-1.5 rounded bg-red-500/15 hover:bg-red-500/25 text-red-300 flex items-center gap-1"><Trash2 className="w-3 h-3" /> Clear {kind || "all"}</button>
      </div>

      {/* List */}
      {notifs.length === 0 && !loading ? (
        <div className="text-sm text-gray-500 border border-dashed border-white/10 rounded p-6 text-center">No notifications — ingest, review, or create a test above.</div>
      ) : (
        <div className="space-y-2">
          {notifs.map((n) => (
            <div key={n.id} className={`rounded-lg border p-3 ${levelColor(n.level)} ${n.read ? "opacity-60" : ""}`}>
              <div className="flex items-start gap-2">
                <div className="mt-0.5">{levelIcon(n.level)}</div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-white truncate">{n.title}</span>
                    <span className="text-xs px-1.5 py-0.5 rounded bg-white/10 text-gray-300">{n.kind}</span>
                    <span className={`text-xs px-1.5 py-0.5 rounded ${n.level === "error" ? "bg-red-500/20 text-red-200" : n.level === "success" ? "bg-emerald-500/20 text-emerald-200" : "bg-blue-500/15 text-blue-200"}`}>{n.level}</span>
                    {!n.read && <span className="w-2 h-2 rounded-full bg-blue-500 animate-pulse" title="Unread" />}
                    <span className="text-xs text-gray-500 ml-auto">{timeAgo(n.ts)}</span>
                  </div>
                  {n.message && <div className="text-xs text-gray-300 mt-1 whitespace-pre-wrap break-words">{n.message}</div>}
                  {n.meta && Object.keys(n.meta).length > 0 && <div className="text-xs text-gray-500 mt-1 font-mono truncate">{JSON.stringify(n.meta).slice(0, 120)}</div>}
                </div>
                <div className="flex flex-col gap-1 shrink-0">
                  <button onClick={() => handleMarkRead(n.id, !n.read)} className={`text-xs px-2 py-1 rounded ${n.read ? "bg-white/10 text-gray-400 hover:bg-white/20" : "bg-blue-500 hover:bg-blue-400 text-white"}`}>
                    {n.read ? "Unread" : "Read"}
                  </button>
                  <button onClick={() => handleDelete(n.id)} className="p-1 rounded hover:bg-white/10 text-gray-400 hover:text-red-300 flex justify-center"><X className="w-3 h-3" /></button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="text-xs text-gray-500 text-center">Stored at <code className="text-gray-400">chroma_data/notifications.json</code> (500 cap) · unread {unread}/{notifs.length} · $0</div>
    </div>
  );
}
