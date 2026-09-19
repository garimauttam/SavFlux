/**
 * CommandPalette.tsx — P2 Command Palette + Shortcuts (⌘K)
 *
 * $0, no deps, ~10KB. Fuzzy palette over:
 *  - Tabs: Chat / Review / Write / Graph / Health / Agent
 *  - Indexed files (filter by name, jump to Review)
 *  - Indexed repos (switch active repo)
 *  - Actions: Re-index, Health report, Export, Watcher toggle, Theme
 *  - Help (?), with vim-style j/k + Enter
 *
 * Opens on ⌘K / Ctrl+K / "/" (when not typing). Closable via Esc.
 */

import React, { useEffect, useMemo, useState } from "react";
import { Search, MessageSquare, Zap, Wand2, Network, Bot, Activity, FileCode, Database, HelpCircle, BarChart3, Bookmark, Code2, Clock, Layers, FolderTree } from "lucide-react";
import { IndexedFile, IndexedRepo } from "../types";

type Tab = "chat" | "review" | "write" | "graph" | "health" | "org" | "agent" | "analytics" | "prompts" | "snippets" | "activity" | "bulk" | "explorer";

interface Props {
  open: boolean;
  onClose: () => void;
  indexedFiles: IndexedFile[];
  indexedRepos: IndexedRepo[];
  activeRepoUrl: string | null;
  activeTab: Tab;
  onSetActiveTab: (t: Tab) => void;
  onSetActiveRepo: (url: string | null) => void;
  onNavigateToFile: (source: string) => void;
}

function fuzzyScore(query: string, target: string): number {
  const q = query.toLowerCase().trim();
  const t = target.toLowerCase();
  if (!q) return 0;
  if (t === q) return 100;
  if (t.startsWith(q)) return 80;
  if (t.includes(q)) return 60;
  // Subsequence bonus
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  if (qi === q.length) return 30;
  return -1;
}

export function CommandPalette({ open, onClose, indexedFiles, indexedRepos, activeRepoUrl, activeTab, onSetActiveTab, onSetActiveRepo, onNavigateToFile }: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  // Build command list
  const commands = useMemo(() => {
    const q = query.trim();
    type Item = { id: string; label: string; sub?: string; score: number; icon: React.ElementType; action: () => void };
    const items: Item[] = [];

    // Tabs
    const tabDefs: { id: Tab; label: string; icon: React.ElementType; keys: string }[] = [
      { id: "chat", label: "Chat", icon: MessageSquare, keys: "g c" },
      { id: "review", label: "Code Review", icon: Zap, keys: "g r" },
      { id: "write", label: "Code Writer", icon: Wand2, keys: "g w" },
      { id: "graph", label: "Dep. Graph", icon: Network, keys: "g g" },
      { id: "health", label: "Health", icon: Activity, keys: "g h" },
      { id: "org", label: "Org", icon: Database, keys: "g o" },
      { id: "analytics", label: "Analytics", icon: BarChart3, keys: "g n" },
      { id: "prompts", label: "Prompts", icon: Bookmark, keys: "g p" },
      { id: "snippets", label: "Snippets", icon: Code2, keys: "g s" },
      { id: "activity", label: "Activity", icon: Clock, keys: "g y" },
      { id: "bulk", label: "Bulk", icon: Layers, keys: "g b" },
      { id: "explorer", label: "Explorer", icon: FolderTree, keys: "g e" },
      { id: "agent", label: "Agent", icon: Bot, keys: "g a" },
    ];
    tabDefs.forEach((t) => {
      const score = q ? fuzzyScore(q, `${t.label} ${t.id} ${t.keys}`) : 10;
      if (score >= 0) items.push({ id: `tab:${t.id}`, label: `Go to ${t.label}`, sub: t.keys, score: score + 5, icon: t.icon, action: () => { onSetActiveTab(t.id); onClose(); } });
    });

    // Indexed files
    indexedFiles.slice(0, 200).forEach((f) => {
      const score = q ? fuzzyScore(q, `${f.file_name} ${f.source} ${f.language}`) : -1;
      if (!q || score >= 0) {
        items.push({
          id: `file:${f.source}`,
          label: f.file_name,
          sub: f.source,
          score: q ? score : 5,
          icon: FileCode,
          action: () => {
            onNavigateToFile(f.source);
            onSetActiveTab("review");
            onClose();
          },
        });
      }
    });

    // Indexed repos
    indexedRepos.forEach((r) => {
      const isActive = activeRepoUrl === r.repo_url;
      const score = q ? fuzzyScore(q, r.repo_url) : -1;
      if (!q || score >= 0) {
        items.push({
          id: `repo:${r.repo_url}`,
          label: isActive ? `Repo: ${r.repo_url} (active)` : `Switch to ${r.repo_url}`,
          sub: `${r.chunk_count} chunks`,
          score: q ? score + (isActive ? 5 : 0) : 5,
          icon: Database,
          action: () => {
            onSetActiveRepo(r.repo_url);
            onSetActiveTab("chat");
            onClose();
          },
        });
      }
    });

    // Actions
    const actions: { label: string; sub: string; icon: React.ElementType; action: () => void }[] = [
      { label: "Health report", sub: "Open Health tab", icon: Activity, action: () => { onSetActiveTab("health"); onClose(); } },
      { label: "Toggle theme", sub: "Dark / Light / System", icon: Search, action: () => { onClose(); document.dispatchEvent(new KeyboardEvent("keydown", { key: "t" })); } },
      { label: "Prompt library", sub: "g p — saved prompts & history", icon: Bookmark, action: () => { onSetActiveTab("prompts"); onClose(); } },
      { label: "Snippet vault", sub: "g s — saved code snippets", icon: Code2, action: () => { onSetActiveTab("snippets"); onClose(); } },
      { label: "Activity feed", sub: "g y — unified timeline", icon: Clock, action: () => { onSetActiveTab("activity"); onClose(); } },
      { label: "Bulk operations", sub: "g b — file manager", icon: Layers, action: () => { onSetActiveTab("bulk"); onClose(); } },
      { label: "File explorer", sub: "g e — folder tree", icon: FolderTree, action: () => { onSetActiveTab("explorer"); onClose(); } },
      { label: "Help — shortcuts", sub: "? to show help", icon: HelpCircle, action: () => { onClose(); document.dispatchEvent(new KeyboardEvent("keydown", { key: "?" })); } },
    ];
    actions.forEach((a) => {
      const score = q ? fuzzyScore(q, `${a.label} ${a.sub}`) : 5;
      if (score >= 0) items.push({ id: `action:${a.label}`, label: a.label, sub: a.sub, score, icon: a.icon, action: a.action });
    });

    // Filter and sort: if query, keep only scored >=0, else show top 12
    let filtered = q ? items.filter((i) => i.score >= 0) : items.slice(0, 12);
    filtered.sort((a, b) => b.score - a.score);
    return filtered.slice(0, 24);
  }, [query, indexedFiles, indexedRepos, activeRepoUrl, onSetActiveTab, onSetActiveRepo, onNavigateToFile, onClose]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => (s + 1) % Math.max(1, commands.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => (s - 1 + commands.length) % Math.max(1, commands.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      commands[selected]?.action();
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[20vh] bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div
        className="w-full max-w-lg bg-gray-800 border border-gray-700 rounded-xl shadow-2xl overflow-hidden mx-4"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-700">
          <Search className="w-4 h-4 text-gray-500" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search files, repos, tabs… (⌘K, j/k, Enter)"
            className="flex-1 bg-transparent text-sm text-white placeholder-gray-500 focus:outline-none"
          />
          <span className="text-xs text-gray-600">Esc</span>
        </div>

        <div className="max-h-64 overflow-y-auto">
          {commands.length === 0 ? (
            <div className="px-3 py-6 text-center text-sm text-gray-500">No matches</div>
          ) : (
            commands.map((c, idx) => {
              const Icon = c.icon;
              const isSel = idx === selected;
              return (
                <button
                  key={c.id}
                  onClick={() => c.action()}
                  onMouseEnter={() => setSelected(idx)}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors ${isSel ? "bg-purple-600/20 text-white" : "text-gray-400 hover:bg-gray-700/60 hover:text-gray-200"}`}
                >
                  <Icon className={`w-4 h-4 shrink-0 ${isSel ? "text-purple-400" : "text-gray-600"}`} />
                  <span className="truncate flex-1">{c.label}</span>
                  {c.sub && <span className="text-xs text-gray-600 truncate max-w-[160px]">{c.sub}</span>}
                </button>
              );
            })
          )}
        </div>

        <div className="px-3 py-2 border-t border-gray-700 flex items-center justify-between text-xs text-gray-600">
          <span>
            {commands.length} results · <span className="text-gray-500">↑↓ j/k · Enter</span>
          </span>
          <span className="hidden sm:inline">Active: {activeTab}</span>
        </div>
      </div>
    </div>
  );
}

export function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-md bg-gray-800 border border-gray-700 rounded-xl shadow-2xl p-5 mx-4" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
          <HelpCircle className="w-4 h-4 text-purple-400" /> Keyboard Shortcuts
        </h3>
        <div className="space-y-2 text-sm">
          <div className="flex justify-between"><span className="text-gray-400">Open palette</span><span className="font-mono text-gray-300">⌘K / Ctrl+K / /</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Navigate</span><span className="font-mono text-gray-300">j / k / ↑ ↓</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Select</span><span className="font-mono text-gray-300">Enter</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Close</span><span className="font-mono text-gray-300">Esc</span></div>
          <div className="border-t border-gray-700 my-2" />
          <div className="flex justify-between"><span className="text-gray-400">Go Chat</span><span className="font-mono text-gray-300">g c</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Review</span><span className="font-mono text-gray-300">g r</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Graph</span><span className="font-mono text-gray-300">g g</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Health</span><span className="font-mono text-gray-300">g h</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Org</span><span className="font-mono text-gray-300">g o</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Analytics</span><span className="font-mono text-gray-300">g n</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Prompts</span><span className="font-mono text-gray-300">g p</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Snippets</span><span className="font-mono text-gray-300">g s</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Activity</span><span className="font-mono text-gray-300">g y</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Bulk</span><span className="font-mono text-gray-300">g b</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Explorer</span><span className="font-mono text-gray-300">g e</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Go Agent</span><span className="font-mono text-gray-300">g a</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Help</span><span className="font-mono text-gray-300">?</span></div>
        </div>
        <button onClick={onClose} className="mt-4 w-full py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm text-gray-300">
          Close
        </button>
      </div>
    </div>
  );
}
