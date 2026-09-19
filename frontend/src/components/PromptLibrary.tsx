/**
 * PromptLibrary.tsx — Saved prompts + usage history ($0, local file backend).
 *
 * Save reusable prompts, one-click "Use" to drop them into the chat input
 * (via the savflux:use-prompt event → ChatWindow), and browse history.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Bookmark, Loader2, Plus, Play, Trash2, Clock, X, Check,
} from "lucide-react";
import { apiFetch } from "../api";

interface Prompt {
  id: string;
  title: string;
  text: string;
  kind: string;
  tags: string[];
  use_count: number;
  created_at: number;
}

interface HistoryRow {
  id: string;
  text: string;
  kind: string;
  ts: number;
}

interface PromptLibraryProps {
  onUsePrompt: (text: string) => void;
}

const KINDS = ["general", "chat", "review", "write", "explain", "test", "doc"];

export default function PromptLibrary({ onUsePrompt }: PromptLibraryProps) {
  const [prompts, setPrompts] = useState<Prompt[]>([]);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [kind, setKind] = useState("");
  const [loading, setLoading] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [tab, setTab] = useState<"saved" | "history">("saved");
  // form state
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [formKind, setFormKind] = useState("general");
  const [tags, setTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [usedId, setUsedId] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [pRes, hRes] = await Promise.all([
        apiFetch(kind ? `/api/v1/prompts?kind=${encodeURIComponent(kind)}` : "/api/v1/prompts"),
        apiFetch("/api/v1/prompts/history?limit=50"),
      ]);
      if (pRes.ok) setPrompts((await pRes.json()).prompts ?? []);
      if (hRes.ok) setHistory((await hRes.json()).history ?? []);
    } catch {}
    finally {
      setLoading(false);
    }
  }, [kind]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const save = async () => {
    if (!text.trim() || saving) return;
    setSaving(true);
    try {
      const res = await apiFetch("/api/v1/prompts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: text.trim(),
          title: title.trim() || undefined,
          kind: formKind,
          tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        }),
      });
      if (!res.ok) throw new Error();
      setText(""); setTitle(""); setTags(""); setFormKind("general");
      setShowForm(false);
      fetchAll();
    } catch {}
    finally {
      setSaving(false);
    }
  };

  const use = async (p: Prompt) => {
    try {
      await apiFetch(`/api/v1/prompts/${p.id}/use`, { method: "POST" });
    } catch {}
    setUsedId(p.id);
    window.setTimeout(() => setUsedId(null), 1500);
    onUsePrompt(p.text);
  };

  const remove = async (id: string) => {
    if (!window.confirm("Delete this prompt?")) return;
    try {
      await apiFetch(`/api/v1/prompts/${id}`, { method: "DELETE" });
      fetchAll();
    } catch {}
  };

  const clearHistory = async () => {
    if (!window.confirm("Clear prompt history?")) return;
    try {
      await apiFetch("/api/v1/prompts/history", { method: "DELETE" });
      fetchAll();
    } catch {}
  };

  return (
    <div className="h-full overflow-y-auto bg-gray-950 p-6">
      <div className="mx-auto max-w-3xl space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Bookmark className="w-5 h-5 text-amber-400" />
            <h2 className="text-lg font-semibold text-white">Prompt Library</h2>
          </div>
          <button
            onClick={() => setShowForm((v) => !v)}
            className="flex items-center gap-1.5 rounded-lg bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-500"
          >
            {showForm ? <X className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
            {showForm ? "Cancel" : "New prompt"}
          </button>
        </div>

        <div className="flex items-center gap-2">
          <div className="flex gap-1 rounded-lg border border-gray-700 bg-gray-900 p-1">
            {(["saved", "history"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium capitalize ${
                  tab === t ? "bg-gray-700 text-white" : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {t === "history" && <Clock className="w-3.5 h-3.5" />}
                {t}
              </button>
            ))}
          </div>
          {tab === "saved" && (
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="rounded-lg border border-gray-700 bg-gray-900 px-2 py-1.5 text-xs text-gray-300"
            >
              <option value="">All kinds</option>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          )}
          {tab === "history" && history.length > 0 && (
            <button onClick={clearHistory} className="ml-auto text-xs text-gray-500 hover:text-red-300">
              Clear history
            </button>
          )}
        </div>

        {showForm && (
          <div className="space-y-2 rounded-xl border border-gray-800 bg-gray-900 p-4">
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Title (optional)"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-amber-500 focus:outline-none"
            />
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Prompt text… (supports @file scope when used in chat)"
              rows={4}
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white placeholder-gray-500 focus:border-amber-500 focus:outline-none"
            />
            <div className="flex gap-2">
              <select
                value={formKind}
                onChange={(e) => setFormKind(e.target.value)}
                className="rounded-lg border border-gray-700 bg-gray-800 px-2 py-2 text-xs text-gray-300"
              >
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
              <input
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder="tags, comma separated"
                className="flex-1 rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-xs text-white placeholder-gray-500 focus:border-amber-500 focus:outline-none"
              />
              <button
                onClick={save}
                disabled={saving || !text.trim()}
                className="flex items-center gap-1.5 rounded-lg bg-amber-600 px-4 py-2 text-xs font-medium text-white hover:bg-amber-500 disabled:bg-gray-700"
              >
                {saving && <Loader2 className="w-3.5 h-3.5 animate-spin" />} Save
              </button>
            </div>
          </div>
        )}

        {loading && (
          <p className="flex items-center gap-2 text-sm text-gray-500">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading…
          </p>
        )}

        {tab === "saved" && (
          <div className="space-y-2">
            {prompts.map((p) => (
              <div key={p.id} className="rounded-xl border border-gray-800 bg-gray-900 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-white">{p.title}</p>
                    <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs text-gray-400">{p.text}</p>
                    <p className="mt-2 text-[11px] text-gray-600">
                      {p.kind} · used {p.use_count}×
                      {p.tags.length > 0 && ` · ${p.tags.join(", ")}`}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <button
                      onClick={() => use(p)}
                      title="Send to chat input"
                      className="flex items-center gap-1 rounded-lg bg-amber-600/20 border border-amber-500/40 px-2.5 py-1.5 text-xs text-amber-200 hover:bg-amber-600/30"
                    >
                      {usedId === p.id ? <Check className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                      Use
                    </button>
                    <button
                      onClick={() => remove(p.id)}
                      title="Delete prompt"
                      className="rounded-lg border border-gray-700 bg-gray-800 p-1.5 text-gray-500 hover:text-red-300"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </div>
            ))}
            {!loading && prompts.length === 0 && (
              <p className="rounded-xl border border-dashed border-gray-800 p-6 text-center text-sm text-gray-600">
                No saved prompts yet — save your best ones for one-click reuse.
              </p>
            )}
          </div>
        )}

        {tab === "history" && (
          <div className="space-y-1.5">
            {history.map((h) => (
              <button
                key={h.id}
                onClick={() => onUsePrompt(h.text)}
                title="Reuse this prompt"
                className="block w-full truncate rounded-lg border border-gray-800 bg-gray-900 px-3 py-2 text-left text-xs text-gray-400 hover:border-amber-500/40 hover:text-gray-200"
              >
                <span className="mr-2 text-gray-600">{new Date(h.ts * 1000).toLocaleString()}</span>
                {h.text}
              </button>
            ))}
            {!loading && history.length === 0 && (
              <p className="rounded-xl border border-dashed border-gray-800 p-6 text-center text-sm text-gray-600">
                Nothing used yet.
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
