/**
 * App.tsx — Root component.
 *
 * Enhancements:
 * - Tracks `activeRepoUrl` — the repo the user has selected for scoped chat
 * - Fetches `indexedRepos` list alongside `indexedFiles`
 * - Passes `activeRepoUrl` + `setActiveRepoUrl` down to IngestPanel (for the
 *   repo selector + clear button) and ChatWindow (for scoped retrieval)
 */

import React, { useState, useEffect, useCallback } from "react";
import { MessageSquare, Zap, Wand2, Network, Bot, Activity, Building2, BarChart3, Bookmark, Code2, Clock, Layers } from "lucide-react";
import { IngestPanel } from "./components/IngestPanel";
import { ChatWindow } from "./components/ChatWindow";
import { ReviewPanel } from "./components/ReviewPanel";
import { AgentPanel } from "./components/AgentPanel";
import { CodeWriterPanel } from "./components/CodeWriterPanel";
import { GraphPanel } from "./components/GraphPanel";
import { HealthPanel } from "./components/HealthPanel";
import { OrgPanel } from "./components/OrgPanel";
import AnalyticsPanel from "./components/AnalyticsPanel";
import PromptLibrary from "./components/PromptLibrary";
import SnippetVault from "./components/SnippetVault";
import ActivityFeed from "./components/ActivityFeed";
import BulkOpsPanel from "./components/BulkOpsPanel";
import { ThemeToggle } from "./components/ThemeToggle";
import { MetricsBar } from "./components/MetricsBar";
import { ShareView } from "./components/ShareView";
import { CommandPalette, ShortcutsHelp } from "./components/CommandPalette";
import { IndexedFile, IndexedRepo } from "./types";
import { apiFetch } from "./api";

// localStorage helpers for persisting activeRepoUrl across page refreshes
const ACTIVE_REPO_KEY = "codesage:activeRepoUrl";
function loadActiveRepo(): string | null {
  try { return localStorage.getItem(ACTIVE_REPO_KEY); } catch { return null; }
}
function saveActiveRepo(url: string | null): void {
  try {
    if (url) localStorage.setItem(ACTIVE_REPO_KEY, url);
    else localStorage.removeItem(ACTIVE_REPO_KEY);
  } catch {}
}

type Tab = "chat" | "review" | "write" | "graph" | "health" | "org" | "agent" | "analytics" | "prompts" | "snippets" | "activity" | "bulk";

function App() {
  // Share route — https://savflux.app/s/{id} (P1 #5.5)
  const isShareRoute = (() => {
    try { return window.location.pathname.startsWith("/s/"); } catch { return false; }
  })();
  if (isShareRoute) return <ShareView />;
  const [indexedFiles, setIndexedFiles] = useState<IndexedFile[]>([]);
  const [indexedRepos, setIndexedRepos] = useState<IndexedRepo[]>([]);
  const [activeRepoUrl, setActiveRepoUrl] = useState<string | null>(loadActiveRepo);
  const [activeRepoUrls, setActiveRepoUrls] = useState<string[]>([]); // multi-repo cross-search
  const [activeTab, setActiveTab] = useState<Tab>("chat");
  // Source path of the file the user double-clicked in the graph — used to
  // pre-select it in the Review panel when navigating graph → review
  const [reviewTargetSource, setReviewTargetSource] = useState<string | null>(null);
  const [isPaletteOpen, setPaletteOpen] = useState(false);
  const [isHelpOpen, setHelpOpen] = useState(false);

  // useCallback with no deps — but we use the functional form of setActiveRepoUrl
  // so the callback always reads CURRENT state instead of a stale closure value.
  // Without this, fetchIndexedFiles() captures `activeRepoUrl` at render time.
  // If the user selects a repo and then triggers a re-fetch, the captured value
  // is stale and the auto-deselect/auto-select logic runs incorrectly.
  const fetchIndexedFiles = useCallback(async () => {
    try {
      const [filesRes, reposRes] = await Promise.all([
        apiFetch("/api/v1/chat/indexed-files"),
        apiFetch("/api/v1/ingest/repos"),
      ]);
      const filesData = await filesRes.json();
      const reposData = await reposRes.json();
      setIndexedFiles(filesData.files ?? []);

      const repos: IndexedRepo[] = reposData.repos ?? [];
      setIndexedRepos(repos);

      // Use functional updater — reads current activeRepoUrl, not the closure value.
      setActiveRepoUrl((current) => {
        const next =
          repos.length === 1 && !current ? repos[0].repo_url :
          current && !repos.find((r) => r.repo_url === current) ? null :
          current;
        saveActiveRepo(next);
        return next;
      });
    } catch {
      // Silently fail — app works even if this endpoint is temporarily down
    }
  }, []); // setActiveRepoUrl and setIndexedFiles are stable — no deps needed

  useEffect(() => {
    fetchIndexedFiles();
  }, [fetchIndexedFiles]);

  // P2 Command Palette + Shortcuts (⌘K, /, ?, g + c/r/g/h/a)
  useEffect(() => {
    let gPressed = false;
    let gTimer: number | null = null;
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const isInput = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      // Palette: ⌘K / Ctrl+K
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      // "/" to open palette when not typing
      if (!isInput && e.key === "/" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (isPaletteOpen || isHelpOpen) return;
      // "?" help (Shift+?)
      if (e.key === "?" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        setHelpOpen(true);
        return;
      }
      // g sequence: g then c/r/g/h/a/w
      if (e.key === "g" && !isInput && !e.metaKey && !e.ctrlKey) {
        gPressed = true;
        if (gTimer) window.clearTimeout(gTimer);
        gTimer = window.setTimeout(() => { gPressed = false; }, 800);
        return;
      }
      if (gPressed && !isInput) {
        const map: Record<string, typeof activeTab> = { c: "chat", r: "review", w: "write", g: "graph", h: "health", o: "org", a: "agent", n: "analytics", p: "prompts", s: "snippets", y: "activity", b: "bulk" };
        const tab = map[e.key.toLowerCase()];
        if (tab) {
          e.preventDefault();
          setActiveTab(tab);
          gPressed = false;
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      if (gTimer) window.clearTimeout(gTimer);
    };
  }, [isPaletteOpen, isHelpOpen, activeTab]);

  const tabs: { id: Tab; label: string; Icon: React.ElementType; color: string }[] = [
    { id: "chat",   label: "Chat",         Icon: MessageSquare, color: "text-blue-400"   },
    { id: "review", label: "Code Review",  Icon: Zap,           color: "text-yellow-400" },
    { id: "write",  label: "Code Writer",  Icon: Wand2,         color: "text-green-400"  },
    { id: "graph",  label: "Dep. Graph",   Icon: Network,       color: "text-purple-400" },
    { id: "health", label: "Health",       Icon: Activity,      color: "text-emerald-400"},
    { id: "org",    label: "Org",          Icon: Building2,     color: "text-cyan-400"   },
    { id: "analytics", label: "Analytics", Icon: BarChart3,    color: "text-indigo-400" },
    { id: "prompts",   label: "Prompts",    Icon: Bookmark,     color: "text-amber-400"  },
    { id: "snippets",  label: "Snippets",   Icon: Code2,        color: "text-violet-400" },
    { id: "activity",  label: "Activity",   Icon: Clock,        color: "text-teal-400"   },
    { id: "bulk",      label: "Bulk",       Icon: Layers,       color: "text-blue-400"   },
    { id: "agent",  label: "Agent",        Icon: Bot,           color: "text-pink-400"   },
  ];

  return (
    <div className="flex h-screen bg-gray-950 text-white overflow-hidden">
      <IngestPanel
        indexedFiles={indexedFiles}
        indexedRepos={indexedRepos}
        activeRepoUrl={activeRepoUrl}
        activeRepoUrls={activeRepoUrls}
        onSetActiveRepo={(url) => { saveActiveRepo(url); setActiveRepoUrl(url); }}
        onSetActiveRepoUrls={setActiveRepoUrls}
        onFilesUpdated={fetchIndexedFiles}
      />

      <div className="flex flex-col flex-1 overflow-hidden">
        <div className="flex items-center border-b border-gray-700 bg-gray-900 px-4 gap-1">
          {tabs.map(({ id, label, Icon, color }) => (
            <button
              key={id}
              onClick={() => setActiveTab(id)}
              className={`flex items-center gap-1.5 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
                activeTab === id
                  ? `${color} border-current`
                  : "text-gray-500 border-transparent hover:text-gray-300"
              }`}
            >
              <Icon className="w-4 h-4" />
              {label}
            </button>
          ))}
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => setPaletteOpen(true)}
              title="Command palette (⌘K)"
              className="hidden sm:flex items-center gap-1.5 text-xs text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 border border-gray-700 px-2.5 py-1 rounded-full"
            >
              <span className="text-gray-500">⌘K</span>
            </button>
            <ThemeToggle compact />
          </div>
        </div>

        <div className="flex-1 overflow-hidden">
          {activeTab === "chat" && (
            <ChatWindow
              activeRepoUrl={activeRepoUrl}
              hasIndexedFiles={indexedFiles.length > 0}
              activeRepoUrls={activeRepoUrls.length > 0 ? activeRepoUrls : null}
            />
          )}
          {activeTab === "review" && (
            <ReviewPanel
              indexedFiles={indexedFiles}
              initialSelectedSource={reviewTargetSource}
              onInitialSourceConsumed={() => setReviewTargetSource(null)}
            />
          )}
          {activeTab === "write"  && <CodeWriterPanel indexedFiles={indexedFiles} />}
          {activeTab === "agent"  && <AgentPanel />}
          {activeTab === "graph"  && (
            <GraphPanel
              indexedRepos={indexedRepos}
              activeRepoUrl={activeRepoUrl}
              onNavigateToReview={(source) => {
                setReviewTargetSource(source);
                setActiveTab("review");
              }}
            />
          )}
          {activeTab === "health" && <HealthPanel activeRepoUrl={activeRepoUrl} />}
          {activeTab === "org" && (
            <OrgPanel
              indexedRepos={indexedRepos}
              activeRepoUrl={activeRepoUrl}
              activeRepoUrls={activeRepoUrls}
              onSetActiveRepo={(url) => { saveActiveRepo(url); setActiveRepoUrl(url); }}
              onSetActiveRepoUrls={setActiveRepoUrls}
              onFilesUpdated={fetchIndexedFiles}
            />
          )}
          {activeTab === "analytics" && <AnalyticsPanel />}
          {activeTab === "prompts" && <PromptLibrary onUsePrompt={(text) => { setActiveTab("chat"); window.dispatchEvent(new CustomEvent("savflux:use-prompt", { detail: text })); }} />}
          {activeTab === "snippets" && <SnippetVault />}
          {activeTab === "activity" && <ActivityFeed />}
          {activeTab === "bulk" && <BulkOpsPanel onFilesUpdated={() => window.location.reload()} />}
        </div>
        <MetricsBar />
      </div>
      <CommandPalette
        open={isPaletteOpen}
        onClose={() => setPaletteOpen(false)}
        indexedFiles={indexedFiles}
        indexedRepos={indexedRepos}
        activeRepoUrl={activeRepoUrl}
        activeTab={activeTab}
        onSetActiveTab={setActiveTab}
        onSetActiveRepo={(url) => { saveActiveRepo(url); setActiveRepoUrl(url); }}
        onNavigateToFile={(source) => setReviewTargetSource(source)}
      />
      <ShortcutsHelp open={isHelpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}

export default App;
