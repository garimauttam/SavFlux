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
import { MessageSquare, Zap, Wand2, Network } from "lucide-react";
import { IngestPanel } from "./components/IngestPanel";
import { ChatWindow } from "./components/ChatWindow";
import { ReviewPanel } from "./components/ReviewPanel";
import { CodeWriterPanel } from "./components/CodeWriterPanel";
import { GraphPanel } from "./components/GraphPanel";
import { MetricsBar } from "./components/MetricsBar";
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

type Tab = "chat" | "review" | "write" | "graph";

function App() {
  const [indexedFiles, setIndexedFiles] = useState<IndexedFile[]>([]);
  const [indexedRepos, setIndexedRepos] = useState<IndexedRepo[]>([]);
  const [activeRepoUrl, setActiveRepoUrl] = useState<string | null>(loadActiveRepo);
  const [activeRepoUrls, setActiveRepoUrls] = useState<string[]>([]); // multi-repo cross-search
  const [activeTab, setActiveTab] = useState<Tab>("chat");
  // Source path of the file the user double-clicked in the graph — used to
  // pre-select it in the Review panel when navigating graph → review
  const [reviewTargetSource, setReviewTargetSource] = useState<string | null>(null);

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

  const tabs: { id: Tab; label: string; Icon: React.ElementType; color: string }[] = [
    { id: "chat",   label: "Chat",         Icon: MessageSquare, color: "text-blue-400"   },
    { id: "review", label: "Code Review",  Icon: Zap,           color: "text-yellow-400" },
    { id: "write",  label: "Code Writer",  Icon: Wand2,         color: "text-green-400"  },
    { id: "graph",  label: "Dep. Graph",   Icon: Network,       color: "text-purple-400" },
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
        </div>
        <MetricsBar />
      </div>
    </div>
  );
}

export default App;
