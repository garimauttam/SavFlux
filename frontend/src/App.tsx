/**
 * App.tsx — the workspace shell.
 *
 * WHAT THIS FILE IS
 * -----------------
 * A layout, not a product. It owns the four pieces of state every surface
 * agrees on — which repository, which destination, which context file, and
 * whether the drawer is open — and nothing else. The panels it renders were
 * written before the shell existed and are still reachable from it.
 *
 * THE LAYOUT
 * ----------
 *   ┌ rail ┬──────────────────────────────────────┬ context ┐
 *   │ nav  │ conversation / destination           │ evidence│
 *   │      ├──────────────────────────────────────┤         │
 *   │      │ composer                             │         │
 *   └──────┴──────────────────────────────────────┴─────────┘
 *   status bar: index · model · cost
 *
 * That replaces a 17-tab strip and a 401-line sidebar, which is the whole of
 * the "it looks like a pile of unrelated tools" complaint: the structure now
 * matches what the product does, and every surface is reachable from one of
 * three places.
 *
 * WHY STATE LIVES HERE
 * --------------------
 * "Which repository am I on" was previously held in App *and* in localStorage
 * *and* re-derived by three panels that each fetched their own copy. The header
 * and the agent could therefore disagree about the scope of a question. It is
 * read once here and passed down, and the panels that need it are told.
 */

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cloud, Cpu, Database, FolderTree, Github, HardDrive } from "lucide-react";
import { ActivityRail } from "./components/shell/ActivityRail";
import { TopBar } from "./components/shell/TopBar";
import { ContextPanel, useContextPanelOpen } from "./components/shell/ContextPanel";
import { AgentConversation } from "./components/agent/AgentConversation";
import { ConnectGitHubDialog } from "./components/github/ConnectGitHubDialog";
import { RepoBrowser } from "./components/github/RepoBrowser";
import { CommandPalette, ShortcutsHelp } from "./components/CommandPalette";
import { ReviewPanel } from "./components/ReviewPanel";
import { CodeWriterPanel } from "./components/CodeWriterPanel";
import { HealthPanel } from "./components/HealthPanel";
import FileTreePanel from "./components/FileTreePanel";
import { ChangesPanel } from "./components/ChangesPanel";
import { ShareView } from "./components/ShareView";
import { LibraryPanel } from "./components/shell/LibraryPanel";
import { ProfilePanel } from "./components/shell/ProfilePanel";
import { SettingsDialog } from "./components/shell/SettingsDialog";
import { useGitHub, useModels } from "./hooks/useIntegrations";
import { useTheme } from "./hooks/useTheme";
import { useAuthUserId } from "./lib/authUser";
import { branchStorageKey, loadRepoBranch, repositoryKey, type IndexedSelection } from "./lib/repositorySelection";
import { apiFetch } from "./api";
import { AuthGate } from "./components/AuthGate";
import { DEFAULT_TAB, SHORTCUT_BY_KEY, type Tab } from "./navigation";
import {
  OPEN_FILE_EVENT,
  openFileAt,
  parseOpenFileDetail,
  type OpenFileDetail,
} from "./lib/openFile";
import { repoDisplayName, repoSlugFromUrl } from "./lib/github";
import type { IndexedFile, IndexedRepo } from "./types";
import type { GitHubBranch } from "./types/workspace";

// The dependency graph is the one subtree whose weight earns loading on demand:
// it pulls in force-graph plus the d3 force/scale/zoom family and nothing else
// in the app touches them. Kept in step with the vendor grouping in
// src/lib/chunking.ts by a test.
const GraphPanel = lazy(() =>
  import("./components/GraphPanel").then((m) => ({ default: m.GraphPanel })),
);

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="p-6 space-y-3" role="status" aria-live="polite">
      <div className="sf-mute text-xs font-mono animate-pulse">{label}</div>
      <div className="h-64 rounded-xl sf-raised border sf-line animate-pulse" />
    </div>
  );
}

function activeRepoKey(userId: string): string {
  return `savflux:${userId}:activeRepoUrl`;
}
function loadActiveRepo(userId: string): string | null {
  try { return localStorage.getItem(activeRepoKey(userId)); }
  catch { return null; }
}

/** Share route — https://savflux.app/s/{id}. */
function App() {
  const isShareRoute = (() => {
    try {
      return window.location.pathname.startsWith("/s/");
    } catch {
      return false;
    }
  })();
  return isShareRoute ? <ShareView /> : <AuthGate><Workspace /></AuthGate>;
}

function Workspace() {
  const userId = useAuthUserId();
  const [indexedFiles, setIndexedFiles] = useState<IndexedFile[]>([]);
  const [indexedRepos, setIndexedRepos] = useState<IndexedRepo[]>([]);
  const [activeRepoUrl, setActiveRepoUrl] = useState<string | null>(() => loadActiveRepo(userId));
  const [branchSelections, setBranchSelections] = useState<Record<string, string>>({});
  const branch = activeRepoUrl
    ? branchSelections[repositoryKey(activeRepoUrl)] ?? loadRepoBranch(userId, activeRepoUrl)
    : "";
  const rememberBranch = useCallback((url: string, selected: string) => {
    setBranchSelections((current) => ({ ...current, [repositoryKey(url)]: selected }));
    try { localStorage.setItem(branchStorageKey(userId, url), selected); }
    catch { /* The in-memory choice still works when storage is unavailable. */ }
  }, [userId]);
  const [branches, setBranches] = useState<GitHubBranch[]>([]);
  const [defaultBranch, setDefaultBranch] = useState("");
  const [branchesLoading, setBranchesLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>(DEFAULT_TAB);
  const [contextTarget, setContextTarget] = useState<OpenFileDetail | null>(null);
  const [isPaletteOpen, setPaletteOpen] = useState(false);
  const [isHelpOpen, setHelpOpen] = useState(false);
  const [isGitHubOpen, setGitHubOpen] = useState(false);
  const [isSettingsOpen, setSettingsOpen] = useState(false);
  const [contextOpen, setContextOpen] = useContextPanelOpen();

  const github = useGitHub();
  const models = useModels();
  const theme = useTheme();

  const indexRead = useRef(0);
  const fetchIndexed = useCallback(async (selection?: IndexedSelection) => {
    const requestId = ++indexRead.current;
    // This callback runs only after successful ingestion, never on selection or
    // a failed clone. Update the header even if the follow-up index read fails.
    if (selection) {
      rememberBranch(selection.repoUrl, selection.branch);
      setActiveRepoUrl(selection.repoUrl);
      try { localStorage.setItem(activeRepoKey(userId), selection.repoUrl); }
      catch { /* private mode */ }
    }
    try {
      const [filesRes, reposRes] = await Promise.all([
        apiFetch("/api/v1/chat/indexed-files"),
        apiFetch("/api/v1/ingest/repos"),
      ]);
      if (!filesRes.ok || !reposRes.ok) throw new Error("Could not refresh the index");
      const filesData = await filesRes.json();
      const reposData = await reposRes.json();
      if (requestId !== indexRead.current) return;
      setIndexedFiles(filesData.files ?? []);
      const repos: IndexedRepo[] = reposData.repos ?? [];
      setIndexedRepos(repos);
      setActiveRepoUrl((current) => {
        const next =
          selection
            ? repos.find((repo) => repositoryKey(repo.repo_url) === repositoryKey(selection.repoUrl))?.repo_url ?? selection.repoUrl
            : repos.length === 1 && !current
            ? repos[0].repo_url
            : current && !repos.some((r) => repositoryKey(r.repo_url) === repositoryKey(current))
              ? null
              : current;
        try {
          if (next) localStorage.setItem(activeRepoKey(userId), next);
          else localStorage.removeItem(activeRepoKey(userId));
        } catch {
          /* private mode */
        }
        return next;
      });
    } catch {
      // The shell must render even if this endpoint is down — an unreachable
      // index is a state to show, not a reason to show nothing.
    }
  }, [rememberBranch, userId]);

  useEffect(() => {
    void fetchIndexed();
    return () => { indexRead.current += 1; };
  }, [fetchIndexed]);

  // Public repos can list refs without a connected GitHub account. Load every
  // page, and never replace an explicit selection just because a lookup failed
  // or the branch isn't on the first page.
  useEffect(() => {
    const slug = repoSlugFromUrl(activeRepoUrl);
    setBranches([]);
    setDefaultBranch("");
    setBranchesLoading(false);
    if (!slug) return;
    let cancelled = false;
    const controller = new AbortController();
    const path = slug.split("/").map(encodeURIComponent).join("/");
    setBranchesLoading(true);
    void (async () => {
      try {
        const response = await apiFetch(`/api/v1/github/repos/${path}`, { signal: controller.signal });
        if (!response.ok) return;
        const data = await response.json();
        if (!cancelled) setDefaultBranch(data.default_branch ?? "");
      } catch { /* Default is optional; keep the user's explicit choice. */ }
    })();
    void (async () => {
      try {
        let page: number | null = 1;
        const items: GitHubBranch[] = [];
        while (page !== null) {
          const response = await apiFetch(`/api/v1/github/repos/${path}/branches?page=${page}`, { signal: controller.signal });
          if (!response.ok) break;
          const data = await response.json();
          if (cancelled) return;
          items.push(...(data.branches ?? []));
          setBranches([...new Map(items.map((item) => [item.name, item])).values()]);
          page = data.next_page ?? null;
        }
      } catch { /* Keep any already loaded refs and the saved selection. */ }
      finally { if (!cancelled) setBranchesLoading(false); }
    })();
    return () => { cancelled = true; controller.abort(); };
  }, [activeRepoUrl, github.status?.connected]);

  // "Show me this code" — raised by citations anywhere in the app, and by the
  // file tree. The context panel is the single place that answers it now, so
  // clicking a citation never navigates away from the conversation.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = parseOpenFileDetail((e as CustomEvent).detail);
      if (!detail) return;
      setContextTarget(detail);
      setContextOpen(true);
    };
    window.addEventListener(OPEN_FILE_EVENT as any, handler);
    return () => window.removeEventListener(OPEN_FILE_EVENT as any, handler);
  }, [setContextOpen]);

  // ⌘K, `/`, `?`, and the `g`+key sequences.
  useEffect(() => {
    let gPressed = false;
    let gTimer: number | null = null;

    /**
     * Is the user typing?
     *
     * Both `e.target` and `document.activeElement` are consulted, because they
     * are not always the same element and a shortcut that fires while someone
     * is typing is the worst kind of bug: the question "why is the JWT
     * signature validated there?" threw the keyboard reference over the answer
     * because the `?` arm had no input guard at all, and the `/` arm guards
     * only `e.target` — which is `BODY` for a synthesised event, for some
     * IME compositions, and for anything assistive technology dispatches.
     */
    const isTyping = (target: EventTarget | null): boolean => {
      const el = (x: unknown): HTMLElement | null =>
        x instanceof HTMLElement ? x : null;
      for (const candidate of [el(target), el(document.activeElement)]) {
        if (!candidate) continue;
        if (
          candidate.tagName === "INPUT" ||
          candidate.tagName === "TEXTAREA" ||
          candidate.isContentEditable
        ) {
          return true;
        }
      }
      return false;
    };

    const handler = (e: KeyboardEvent) => {
      const isInput = isTyping(e.target);
      const modalOpen = isPaletteOpen || isHelpOpen || isGitHubOpen || isSettingsOpen;

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (gPressed && !isInput && !modalOpen && !e.metaKey && !e.ctrlKey) {
        const pending = SHORTCUT_BY_KEY[e.key.toLowerCase()];
        gPressed = false;
        if (gTimer) { window.clearTimeout(gTimer); gTimer = null; }
        if (pending) {
          e.preventDefault();
          setActiveTab(pending);
          return;
        }
      }
      if (!isInput && !modalOpen && e.key === "/" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (modalOpen) return;
      // `?` was reachable while typing. Every other single-key shortcut is
      // guarded by `isInput` and this one was not, so asking "where do we
      // validate the JWT signature?" in the composer threw up the keyboard
      // reference over the answer — which is exactly the kind of thing that
      // makes a product feel broken rather than unfinished.
      if (!isInput && e.key === "?" && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        setHelpOpen(true);
        return;
      }
      if (e.key === "g" && !isInput && !e.metaKey && !e.ctrlKey) {
        gPressed = true;
        if (gTimer) window.clearTimeout(gTimer);
        gTimer = window.setTimeout(() => { gPressed = false; }, 800);
      }
    };
    window.addEventListener("keydown", handler);
    return () => {
      window.removeEventListener("keydown", handler);
      if (gTimer) window.clearTimeout(gTimer);
    };
  }, [isGitHubOpen, isHelpOpen, isPaletteOpen, isSettingsOpen]);

  const repoOptions = useMemo(
    () =>
      indexedRepos.map((r) => ({
        url: r.repo_url,
        name: repoDisplayName(r.repo_url),
      })),
    [indexedRepos],
  );

  const selectRepo = useCallback((url: string | null) => {
    setActiveRepoUrl(url);
    try {
      if (url) localStorage.setItem(activeRepoKey(userId), url);
      else localStorage.removeItem(activeRepoKey(userId));
    } catch { /* private mode */ }
  }, [userId]);

  const selectBranch = useCallback((selected: string) => {
    if (activeRepoUrl) rememberBranch(activeRepoUrl, selected);
  }, [activeRepoUrl, rememberBranch]);

  const hasIndex = indexedFiles.length > 0;
  const githubConnected = Boolean(github.status?.connected && github.status?.valid);

  const renderSettings = (embedded = false) => (
    <SettingsDialog
        embedded={embedded}
        open={embedded || isSettingsOpen}
        onClose={() => setSettingsOpen(false)}
        models={models.models}
        onSelectModel={(name) => void models.select(name)}
        onSelectMixtureModels={models.selectMixture}
        onSelectProvider={models.selectProvider}
        onForgetProviderKey={models.forgetProviderKey}
        providerBusy={models.busy}
        providerError={models.error}
        onOpenGitHub={() => {
          setSettingsOpen(false);
          setGitHubOpen(true);
        }}
        contextOpen={contextOpen}
        onToggleContext={() => setContextOpen(!contextOpen)}
      />
  );

  const body = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        className="min-h-0 flex-1 overflow-hidden"
        role="tabpanel"
        id={`savflux-panel-${activeTab}`}
        aria-labelledby={`savflux-tab-${activeTab}`}
      >
        {activeTab === "agent" && (
          <AgentConversation
            activeRepoUrl={activeRepoUrl}
            hasIndexedFiles={hasIndex}
            models={models.models}
            onOpenGitHub={() => setGitHubOpen(true)}
            // The empty state can index a public URL or an upload, so it has to
            // tell the app the index changed — otherwise the button is pressed,
            // the work happens, and the page still says there is nothing here.
            onIndexed={fetchIndexed}
          />
        )}
        {activeTab === "profile" && <ProfilePanel github={github.status} onConnectGitHub={() => setGitHubOpen(true)} modelsContent={renderSettings(true)} isDark={theme.isDark} onToggleTheme={theme.toggle} />}
        {activeTab === "review" && <ReviewPanel indexedFiles={indexedFiles} onIndexed={fetchIndexed} />}
        {activeTab === "write" && <CodeWriterPanel indexedFiles={indexedFiles} />}
        {activeTab === "explorer" && <FileTreePanel onOpenFile={(src) => openFileAt(src)} onIndexed={fetchIndexed} />}
        {activeTab === "graph" && (
          <Suspense fallback={<PanelLoader label="Loading the dependency graph…" />}>
            <GraphPanel
              indexedRepos={indexedRepos}
              activeRepoUrl={activeRepoUrl}
              onIndexed={fetchIndexed}
              onNavigateToReview={(source) => {
                openFileAt({ source });
                setActiveTab("review");
              }}
            />
          </Suspense>
        )}
        {activeTab === "health" && <HealthPanel activeRepoUrl={activeRepoUrl} />}
        {activeTab === "repos" && (
          <RepoBrowser
            status={github.status}
            onConnect={() => setGitHubOpen(true)}
            indexedRepos={indexedRepos}
            indexedFiles={indexedFiles}
            activeRepoUrl={activeRepoUrl}
            onSelectRepo={selectRepo}
            onIndexed={fetchIndexed}
            models={models.models}
            branch={branch}
          />
        )}
        {activeTab === "changes" && (
          <ChangesPanel
            activeRepoUrl={activeRepoUrl}
            selectedBranch={branch}
            defaultBranch={defaultBranch}
            branches={branches}
            branchesLoading={branchesLoading}
            githubConnected={githubConnected}
            onConnectGitHub={() => setGitHubOpen(true)}
          />
        )}
        {activeTab === "library" && (
          <LibraryPanel
            onIndexed={fetchIndexed}
            onUsePrompt={(text) => {
              setActiveTab("agent");
              window.dispatchEvent(new CustomEvent("savflux:use-prompt", { detail: text }));
            }}
          />
        )}
      </div>
      <StatusFooter
        indexCount={indexedFiles.length}
        repoCount={indexedRepos.length}
        activeRepoUrl={activeRepoUrl}
        models={models.models}
        githubConnected={githubConnected}
        onOpenModels={() => setSettingsOpen(true)}
        onOpenGitHub={() => setGitHubOpen(true)}
      />
    </div>
  );

  return (
    <div className="flex h-screen overflow-hidden" style={{ background: "var(--sf-canvas)", color: "var(--sf-text)" }}>
      <ActivityRail
        active={activeTab}
        onSelect={setActiveTab}
        hasIndex={hasIndex}
        githubConnected={githubConnected}
        onConnectGitHub={() => setGitHubOpen(true)}
        onOpenSettings={() => setActiveTab("profile")}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          repos={repoOptions}
          activeRepoUrl={activeRepoUrl}
          onSelectRepo={selectRepo}
          branch={branch || defaultBranch}
          branches={branches.map((b) => b.name)}
          onSelectBranch={selectBranch}
          branchesLoading={branchesLoading}
          indexCount={indexedFiles.length}
          github={github.status}
          models={models.models}
          modelsBusy={models.busy}
          onSelectModel={(name) => void models.select(name)}
          onConnectGitHub={() => setGitHubOpen(true)}
          onOpenProfile={() => setActiveTab("profile")}
          onSignOut={() => window.dispatchEvent(new Event("savflux:signout"))}
          onOpenCommandPalette={() => setPaletteOpen(true)}
          contextOpen={contextOpen}
          onToggleContext={() => setContextOpen(!contextOpen)}
          isDark={theme.isDark}
          onToggleTheme={theme.toggle}
        />

        <div className="relative flex min-h-0 flex-1">
          {body}
          <ContextPanel
            open={contextOpen}
            onClose={() => {
              if (contextTarget) {
                setContextTarget(null);
                return;
              }
              setContextOpen(false);
            }}
            target={contextTarget}
            onClearTarget={() => setContextTarget(null)}
            indexedFiles={indexedFiles}
            repoUrl={activeRepoUrl}
          />
        </div>
      </div>

      <CommandPalette
        open={isPaletteOpen}
        onClose={() => setPaletteOpen(false)}
        indexedFiles={indexedFiles}
        indexedRepos={indexedRepos}
        activeRepoUrl={activeRepoUrl}
        activeTab={activeTab}
        onSetActiveTab={(t) => setActiveTab(t as Tab)}
        onSetActiveRepo={selectRepo}
        onNavigateToFile={(source) => {
          setContextTarget({ source });
          setContextOpen(true);
        }}
        onOpenGitHub={() => setGitHubOpen(true)}
      />
      <ShortcutsHelp open={isHelpOpen} onClose={() => setHelpOpen(false)} />
      <ConnectGitHubDialog
        open={isGitHubOpen}
        onClose={() => setGitHubOpen(false)}
        status={github.status}
        busy={github.busy}
        error={github.error}
        onConnect={github.connect}
        onDisconnect={github.disconnect}
      />
      {renderSettings()}
    </div>
  );
}

/* ── Status bar ───────────────────────────────────────────────────────────── */

function StatusFooter({
  indexCount,
  repoCount,
  activeRepoUrl,
  models,
  githubConnected,
  onOpenModels,
  onOpenGitHub,
}: {
  indexCount: number;
  repoCount: number;
  activeRepoUrl: string | null;
  models: ReturnType<typeof useModels>["models"];
  githubConnected: boolean;
  onOpenModels: () => void;
  onOpenGitHub: () => void;
}) {
  return (
    <footer
      className="sf-surface flex h-[26px] shrink-0 items-center gap-4 border-t sf-line px-3 text-[11px]"
      aria-label="Status"
    >
      <span className="sf-mute inline-flex items-center gap-1.5" title="Indexed files">
        <HardDrive className="h-3 w-3" />
        {indexCount} {indexCount === 1 ? "file" : "files"}
        {repoCount > 0 && ` · ${repoCount} ${repoCount === 1 ? "repo" : "repos"}`}
      </span>

      {activeRepoUrl && (
        <span className="sf-mute hidden min-w-0 items-center gap-1.5 sm:inline-flex">
          <Database className="h-3 w-3 shrink-0" />
          <span className="truncate">
            {activeRepoUrl.replace(/^https?:\/\//, "").replace(/\.git$/, "")}
          </span>
        </span>
      )}

      <span className="flex-1" />

      <button
        type="button"
        onClick={onOpenModels}
        className="sf-mute inline-flex items-center gap-1.5 transition-colors hover:text-[var(--sf-text)]"
        title={models?.available ? "Model settings" : models?.hint ?? "Model settings"}
      >
        <Cpu className="h-3 w-3" />
        {models ? (models.provider === "ollama" ? models.chat_model : models.provider_model) : "no model"}
        {models && !models.available && (
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: models.provider === "ollama" && !models.reachable ? "var(--sf-bad)" : "var(--sf-warn)" }}
            aria-label={models.kind === "provider_key_missing" ? "provider API key missing" : models.reachable ? "model not installed" : "model runtime not running"}
          />
        )}
      </button>

      <button
        type="button"
        onClick={onOpenGitHub}
        className="sf-mute inline-flex items-center gap-1.5 transition-colors hover:text-[var(--sf-text)]"
        title={githubConnected ? "GitHub connected" : "Connect GitHub"}
      >
        {githubConnected ? <Github className="h-3 w-3" /> : <Cloud className="h-3 w-3" />}
        {githubConnected ? "GitHub" : "not connected"}
      </button>

      {indexCount === 0 && (
        <span className="sf-mute hidden items-center gap-1.5 md:inline-flex">
          <FolderTree className="h-3 w-3" />
          nothing indexed
        </span>
      )}
    </footer>
  );
}

export default App;
