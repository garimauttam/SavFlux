/**
 * CommandPalette.tsx — ⌘K over everything, derived from `navigation.ts`.
 *
 * WHY IT CHANGED
 * --------------
 * The palette listed twelve actions by hand, each naming a tab that no longer
 * existed the moment a section moved. `onSetActiveTab("prompts")` compiled
 * perfectly and did nothing, because `prompts` is a sub-tab of the Library panel
 * now — so "Prompt library" in the palette was a dead entry, and there was no
 * test that could notice because the type still allowed the value.
 *
 * So there is one source of action metadata: `navigation.ts`. A destination the
 * rail can reach, the palette can reach, and a `g`+key can reach, and they are
 * generated from the same nine entries. Adding a section is one object, and the
 * three surfaces cannot disagree about what it is called or which key opens it.
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  Database,
  FileCode,
  Github,
  HelpCircle,
  Search,
  SunMoon,
} from "lucide-react";
import { IndexedFile, IndexedRepo } from "../types";
import { NAV_GROUPS, TABS, type Tab } from "../navigation";

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
  onOpenGitHub: () => void;
}

function fuzzyScore(query: string, target: string): number {
  const q = query.toLowerCase().trim();
  const t = target.toLowerCase();
  if (!q) return 0;
  if (t === q) return 100;
  if (t.startsWith(q)) return 80;
  if (t.includes(q)) return 60;
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length ? 30 : -1;
}

export function CommandPalette({
  open,
  onClose,
  indexedFiles,
  indexedRepos,
  activeRepoUrl,
  activeTab,
  onSetActiveTab,
  onSetActiveRepo,
  onNavigateToFile,
  onOpenGitHub,
}: Props) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      window.setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  const commands = useMemo(() => {
    const q = query.trim();
    type Item = {
      id: string;
      label: string;
      sub?: string;
      group: string;
      score: number;
      icon: React.ElementType;
      action: () => void;
    };
    const items: Item[] = [];

    // Destinations — label, icon, group and shortcut all come from navigation.ts.
    for (const group of NAV_GROUPS) {
      for (const t of group.items) {
        const score = q ? fuzzyScore(q, `${t.label} ${t.id} ${t.hint} g ${t.shortcut}`) : 10;
        if (score < 0) continue;
        items.push({
          id: `tab:${t.id}`,
          label: `Go to ${t.label}`,
          sub: `g ${t.shortcut} · ${t.hint}`,
          group: group.label,
          score: score + 5,
          icon: t.Icon,
          action: () => {
            onSetActiveTab(t.id);
            onClose();
          },
        });
      }
    }

    indexedFiles.slice(0, 400).forEach((f) => {
      const score = q ? fuzzyScore(q, `${f.file_name} ${f.source}`) : -1;
      if (!q || score >= 0) {
        items.push({
          id: `file:${f.source}`,
          label: f.file_name,
          sub: f.source,
          group: "Files",
          score: q ? score : 5,
          icon: FileCode,
          // Opens in the context panel beside the conversation rather than
          // navigating away from it — the old behaviour threw away the thread
          // you were reading to look at one line of a file.
          action: () => {
            onNavigateToFile(f.source);
            onClose();
          },
        });
      }
    });

    indexedRepos.forEach((r) => {
      const isActive = activeRepoUrl === r.repo_url;
      const score = q ? fuzzyScore(q, r.repo_url) : -1;
      if (!q || score >= 0) {
        items.push({
          id: `repo:${r.repo_url}`,
          label: isActive ? `${r.repo_url} (active)` : `Switch to ${r.repo_url}`,
          sub: `${r.chunk_count} chunks`,
          group: "Repositories",
          score: q ? score + (isActive ? 5 : 0) : 5,
          icon: Database,
          action: () => {
            onSetActiveRepo(r.repo_url);
            onSetActiveTab("agent");
            onClose();
          },
        });
      }
    });

    const actions: { id: string; label: string; sub: string; icon: React.ElementType; action: () => void }[] = [
      {
        id: "action:github",
        label: "Connect GitHub",
        sub: "browse repositories, branches and pull requests",
        icon: Github,
        action: () => {
          onClose();
          onOpenGitHub();
        },
      },
      {
        id: "action:theme",
        label: "Toggle theme",
        sub: "dark / light",
        icon: SunMoon,
        action: () => {
          onClose();
          document.documentElement.classList.toggle("dark");
        },
      },
      {
        id: "action:help",
        label: "Help — keyboard shortcuts",
        sub: "? opens this list too",
        icon: HelpCircle,
        action: () => {
          onClose();
          window.dispatchEvent(new CustomEvent("savflux:open-help"));
        },
      },
    ];
    for (const a of actions) {
      const score = q ? fuzzyScore(q, `${a.label} ${a.sub}`) : 5;
      if (score >= 0) {
        items.push({ ...a, group: "Actions", score, action: a.action });
      }
    }

    const filtered = q ? items.filter((i) => i.score >= 0) : items.slice(0, 12);
    filtered.sort((a, b) => b.score - a.score);
    return filtered.slice(0, 24);
  }, [query, indexedFiles, indexedRepos, activeRepoUrl, onSetActiveTab, onSetActiveRepo, onNavigateToFile, onOpenGitHub, onClose]);

  useEffect(() => setSelected(0), [query]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "j") {
      e.preventDefault();
      setSelected((s) => (s + 1) % Math.max(1, commands.length));
    } else if (e.key === "ArrowUp" || e.key === "k") {
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
    <div
      className="fixed inset-0 z-[90] flex items-start justify-center pt-[14vh]"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(3px)" }}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      onClick={onClose}
    >
      <div
        className="mx-4 w-full max-w-xl overflow-hidden rounded-2xl border sf-line shadow-2xl shadow-black/60 sf-overlay"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2.5 border-b sf-line px-4 py-3">
          <Search className="h-4 w-4 shrink-0 sf-mute" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search sections, files and repositories…"
            aria-label="Search"
            className="sf-text flex-1 bg-transparent text-[13.5px] outline-none placeholder:text-[var(--sf-text-mute)]"
          />
          <kbd className="sf-mute rounded border sf-line px-1.5 py-0.5 text-[10px]">Esc</kbd>
        </div>

        <div className="max-h-72 overflow-y-auto p-1.5">
          {commands.length === 0 ? (
            <p className="sf-mute px-3 py-8 text-center text-[13px]">
              Nothing matches “{query}”.
            </p>
          ) : (
            commands.map((c, idx) => {
              const Icon = c.icon;
              const isSel = idx === selected;
              return (
                <button
                  key={c.id}
                  onClick={c.action}
                  onMouseEnter={() => setSelected(idx)}
                  role="option"
                  aria-selected={isSel}
                  className={[
                    "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors",
                    isSel ? "sf-accent-soft" : "sf-dim hover:bg-[var(--sf-raised)]",
                  ].join(" ")}
                >
                  <Icon className={`h-4 w-4 shrink-0 ${isSel ? "sf-accent" : "sf-mute"}`} />
                  <span className="min-w-0 flex-1 truncate">{c.label}</span>
                  {c.sub && (
                    <span className="sf-mute hidden max-w-[45%] truncate text-[11px] sm:block">
                      {c.sub}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>

        <div className="sf-mute flex items-center justify-between border-t sf-line px-4 py-2 text-[11px]">
          <span>
            {commands.length} results · ↑↓ or j/k · Enter
          </span>
          <span className="sf-dim">{TABS.find((t) => t.id === activeTab)?.label}</span>
        </div>
      </div>
    </div>
  );
}

/**
 * Keyboard shortcuts, generated from the same list the rail renders.
 *
 * The previous version listed seventeen hardcoded `g`+key pairs, six of which
 * named sections that had been renamed. There is no way for a hand-written
 * list to stay true; this one cannot be wrong.
 */
export function ShortcutsHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-[90] flex items-center justify-center p-4"
      style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(3px)" }}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
      onClick={onClose}
    >
      <div
        className="mx-4 w-full max-w-md rounded-2xl border sf-line p-5 shadow-2xl shadow-black/60 sf-surface"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
      >
        <h3 className="sf-text mb-3 flex items-center gap-2 text-[14px] font-semibold">
          <BookOpen className="h-4 w-4 sf-accent" /> Keyboard shortcuts
        </h3>
        <div className="space-y-1.5 text-[12.5px]">
          <Row label="Open the palette" keys="⌘K · Ctrl+K · /" />
          <Row label="Close anything" keys="Esc" />
          <Row label="Move through the palette" keys="↑ ↓ · j k" />
          <div className="my-2 h-px sf-line" />
          {NAV_GROUPS.map((group) => (
            <div key={group.id} className="space-y-1">
              <p className="sf-mute pt-1 text-[10px] font-semibold uppercase tracking-[0.08em]">
                {group.label}
              </p>
              {group.items.map((t) => (
                <Row key={t.id} label={t.label} keys={`g ${t.shortcut}`} />
              ))}
            </div>
          ))}
        </div>
        <button type="button" onClick={onClose} className="sf-btn sf-btn-secondary mt-4 w-full">
          Close
        </button>
      </div>
    </div>
  );
}

function Row({ label, keys }: { label: string; keys: string }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="sf-dim">{label}</span>
      <kbd className="sf-mono sf-raised shrink-0 rounded border sf-line px-1.5 py-0.5 text-[11px] sf-text">
        {keys}
      </kbd>
    </div>
  );
}
