/**
 * App.tsx — Root component.
 *
 * Enhancements:
 * - Tracks `activeRepoUrl` — the repo the user has selected for scoped chat
 * - Fetches `indexedRepos` list alongside `indexedFiles`
 * - Passes `activeRepoUrl` + `setActiveRepoUrl` down to IngestPanel (for the
 *   repo selector + clear button) and ChatWindow (for scoped retrieval)
 */

import { Suspense, lazy, useState, useEffect, useCallback } from "react";
import { IngestPanel } from "./components/IngestPanel";
import { ChatWindow } from "./components/ChatWindow";
import { ReviewPanel } from "./components/ReviewPanel";
import { AgentPanel } from "./components/AgentPanel";
import { CodeWriterPanel } from "./components/CodeWriterPanel";
import { HealthPanel } from "./components/HealthPanel";
import { OrgPanel } from "./components/OrgPanel";
import AnalyticsPanel from "./components/AnalyticsPanel";
import PromptLibrary from "./components/PromptLibrary";
import SnippetVault from "./components/SnippetVault";
import ActivityFeed from "./components/ActivityFeed";
import BulkOpsPanel from "./components/BulkOpsPanel";
import FileTreePanel from "./components/FileTreePanel";
import DiffViewer from "./components/DiffViewer";
import NotificationsPanel from "./components/NotificationsPanel";
import SlashCommandsPanel from "./components/SlashCommandsPanel";
import TimeMachinePanel from "./components/TimeMachinePanel";
import { ThemeToggle } from "./components/ThemeToggle";
import { TabBar } from "./components/TabBar";
import { MetricsBar } from "./components/MetricsBar";
import { ShareView } from "./components/ShareView";
import { CommandPalette, ShortcutsHelp } from "./components/CommandPalette";
import { IndexedFile, IndexedRepo } from "./types";
import { DEFAULT_TAB, SHORTCUT_BY_KEY, TABS, type Tab } from "./navigation";
import { OPEN_FILE_EVENT, openFileAt, parseOpenFileDetail } from "./lib/openFile";
import { apiFetch } from "./api";

// The dependency-graph panel is the one section whose weight earns loading on demand: it
// pulls in force-graph plus the d3 force/scale/zoom family, and nothing else in the app
// touches them, so they can all stay out of the first-paint bundle. Kept in step with
// the vendor grouping in src/lib/chunking.ts by a test — the moment this import stops
// being dynamic, that whole subtree is back on the critical path and every number in the
// build output still looks fine.
const GraphPanel = lazy(() =>
  import("./components/GraphPanel").then((m) => ({ default: m.GraphPanel })),
);

/** Stand-in while a lazily imported panel's chunk is in flight. */
function PanelLoader({ label }: { label: string }) {
  return (
    <div className="p-6 space-y-3" role="status" aria-live="polite">
      <div className="text-xs text-gray-500 font-mono animate-pulse">{label}</div>
      <div className="h-64 rounded-xl bg-gray-900/60 border border-gray-800 animate-pulse" />
    </div>
  );
}


// localStorage helpers for persisting activeRepoUrl across page refreshes
const ACTIVE_REPO_KEY = "savflux:activeRepoUrl";
function loadActiveRepo(): string | null {
  try { return localStorage.getItem(ACTIVE_REPO_KEY); } catch { return null; }
}
function saveActiveRepo(url: string | null): void {
  try {
    if (url) localStorage.setItem(ACTIVE_REPO_KEY, url);
    else localStorage.removeItem(ACTIVE_REPO_KEY);
  } catch {}
}

/**
 * Share route — https://savflux.app/s/{id} (P1 #5.5).
 *
 * The early return used to sit above the workspace's hooks. React only
 * tolerates that because a share URL never turns into the app mid-session; the
 * moment the path can change (client-side routing, a "back to app" link on the
 * share page) the hook count changes between renders and React throws. This
 * component owns no hooks, so the workspace's hook order can no longer depend
 * on the route.
 */
function App() {
  const isShareRoute = (() => {
    try { return window.location.pathname.startsWith("/s/"); } catch { return false; }
  })();
  return isShareRoute ? <ShareView /> : <Workspace />;
}

function Workspace() {
  const [indexedFiles, setIndexedFiles] = useState<IndexedFile[]>([]);
  const [indexedRepos, setIndexedRepos] = useState<IndexedRepo[]>([]);
  const [activeRepoUrl, setActiveRepoUrl] = useState<string | null>(loadActiveRepo);
  const [activeRepoUrls, setActiveRepoUrls] = useState<string[]>([]); // multi-repo cross-search
  const [activeTab, setActiveTab] = useState<Tab>(DEFAULT_TAB);
  // Source path of the file the user double-clicked in the graph — used to
  // pre-select it in the Review panel when navigating graph → review
  const [reviewTargetSource, setReviewTargetSource] = useState<string | null>(null);
  // Line span to scroll to / highlight when the Review panel opens a file.
  // Set when navigation came from a line-precise citation; null for a plain open.
  const [reviewTargetLines, setReviewTargetLines] = useState<
    { start: number; end: number; ranges?: string } | null
  >(null);
  const [historyTargetSource, setHistoryTargetSource] = useState<string | null>(null);
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

  // P2 Notifications — poll unread count for tab badge (optional, not blocking)
  // (Badge is shown inside NotificationsPanel; global polling could be added here if desired)

  // Open a file in Review — raised by the file tree, the graph, and by
  // line-precise chat citations (which also carry the span to scroll to).
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = parseOpenFileDetail((e as CustomEvent).detail);
      if (!detail) return;
      setReviewTargetSource(detail.source);
      setReviewTargetLines(
        typeof detail.startLine === "number"
          ? {
              start: detail.startLine,
              end: detail.endLine ?? detail.startLine,
              ranges: detail.lineRanges,
            }
          : null,
      );
      setActiveTab("review");
    };
    window.addEventListener(OPEN_FILE_EVENT as any, handler);
    return () => window.removeEventListener(OPEN_FILE_EVENT as any, handler);
  }, []);

  // P1 Time Machine — open file history from Code Writer
  useEffect(() => {
    const handler = (e: Event) => {
      const src = (e as CustomEvent).detail as string;
      if (src) { setHistoryTargetSource(src); setActiveTab("history"); }
    };
    window.addEventListener("savflux:open-history" as any, handler);
    return () => window.removeEventListener("savflux:open-history" as any, handler);
  }, []);

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
      // A pending `g` sequence outranks every single-key shortcut below.
      // Two sections are named by a key that is itself a shortcut — `g g` for
      // Dep. Graph and `g /` for Slash — and both used to be swallowed by the
      // branches underneath: the `g` arm reset the prefix instead of consuming
      // it, and `/` opened the palette. Neither tab could be reached by its
      // documented shortcut.
      if (gPressed && !isInput && !isPaletteOpen && !isHelpOpen && !e.metaKey && !e.ctrlKey) {
        const pending = SHORTCUT_BY_KEY[e.key.toLowerCase()];
        gPressed = false;
        if (gTimer) { window.clearTimeout(gTimer); gTimer = null; }
        if (pending) {
          e.preventDefault();
          setActiveTab(pending);
          return;
        }
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
      // Start a `g` sequence. Consumed above, so this arm only ever sees a
      // leading `g`. The shortcut map is derived from TABS, so a new section
      // gets its `g`+key from its own entry instead of a second edit here.
      if (e.key === "g" && !isInput && !e.metaKey && !e.ctrlKey) {
        gPressed = true;
        if (gTimer) window.clearTimeout(gTimer);
        gTimer = window.setTimeout(() => { gPressed = false; }, 800);
        return;
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      if (gTimer) window.clearTimeout(gTimer);
    };
  }, [isPaletteOpen, isHelpOpen]);


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
        <div className="flex items-center gap-1 border-b border-gray-700 bg-gray-900 pl-2 pr-4">
          {/* 17 sections do not fit on a laptop: the strip scrolls and exposes
              prev/next arrows instead of clipping the tail off-screen. */}
          <TabBar tabs={TABS} activeTab={activeTab} onSelect={setActiveTab} />
          <div className="ml-auto flex shrink-0 items-center gap-2">
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

        <div
          className="flex-1 overflow-hidden"
          role="tabpanel"
          id={`savflux-panel-${activeTab}`}
          aria-labelledby={`savflux-tab-${activeTab}`}
        >
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
              initialTargetLines={reviewTargetLines}
              onInitialSourceConsumed={() => {
                setReviewTargetSource(null);
                setReviewTargetLines(null);
              }}
            />
          )}
          {activeTab === "write"  && <CodeWriterPanel indexedFiles={indexedFiles} />}
          {activeTab === "agent"  && <AgentPanel />}
          {activeTab === "graph"  && (
            <Suspense fallback={<PanelLoader label="Loading the dependency graph\u2026" />}>
              <GraphPanel
                indexedRepos={indexedRepos}
                activeRepoUrl={activeRepoUrl}
                onNavigateToReview={(source) => {
                  setReviewTargetSource(source);
                  setActiveTab("review");
                }}
              />
            </Suspense>
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
          {activeTab === "explorer" && <FileTreePanel onOpenFile={(src) => openFileAt(src)} />}
          {activeTab === "diff" && <DiffViewer />}
          {activeTab === "notifications" && <NotificationsPanel />}
          {activeTab === "history" && (
            <TimeMachinePanel
              initialSource={historyTargetSource}
              onInitialSourceConsumed={() => setHistoryTargetSource(null)}
            />
          )}
          {activeTab === "slash" && <SlashCommandsPanel onUse={(prompt)=>{ setActiveTab("chat"); window.dispatchEvent(new CustomEvent("savflux:use-prompt", {detail: prompt})); }} />}
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
