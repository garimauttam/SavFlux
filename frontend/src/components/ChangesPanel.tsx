import { useState } from "react";
import { FileDiff, GitBranch, GitCompare } from "lucide-react";
import { AgentWorkspacePanel } from "./AgentWorkspacePanel";
import { BranchComparePanel } from "./BranchComparePanel";
import DiffViewer from "./DiffViewer";
import type { GitHubBranch } from "../types/workspace";

type View = "branches" | "workspace" | "files";

interface ChangesPanelProps {
  activeRepoUrl: string | null;
  selectedBranch: string;
  defaultBranch: string;
  branches: GitHubBranch[];
  branchesLoading: boolean;
  githubConnected: boolean;
  onConnectGitHub: () => void;
}

export function ChangesPanel(props: ChangesPanelProps) {
  const [view, setView] = useState<View>("branches");
  return (
    <div className="h-full overflow-y-auto px-4 py-5 sm:px-6">
      <div className="mx-auto mb-5 flex w-full max-w-5xl flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-[var(--sf-text-mute)]">
            Ship · Review before you push
          </p>
          <p className="mt-1 text-sm text-[var(--sf-text-dim)]">
            Review remote branch changes, persistent local Agent edits, or
            indexed file snapshots.
          </p>
        </div>
        <div
          className="flex flex-wrap rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-1"
          role="tablist"
          aria-label="Changes view"
        >
          <button
            type="button"
            role="tab"
            aria-selected={view === "branches"}
            onClick={() => setView("branches")}
            className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition ${view === "branches" ? "bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]" : "text-[var(--sf-text-dim)] hover:text-[var(--sf-text)]"}`}
          >
            <GitCompare className="h-3.5 w-3.5" /> Branches
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "workspace"}
            onClick={() => setView("workspace")}
            className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition ${view === "workspace" ? "bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]" : "text-[var(--sf-text-dim)] hover:text-[var(--sf-text)]"}`}
          >
            <GitBranch className="h-3.5 w-3.5" /> Agent workspace
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "files"}
            onClick={() => setView("files")}
            className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition ${view === "files" ? "bg-[var(--sf-accent-soft)] text-[var(--sf-accent)]" : "text-[var(--sf-text-dim)] hover:text-[var(--sf-text)]"}`}
          >
            <FileDiff className="h-3.5 w-3.5" /> Indexed files
          </button>
        </div>
      </div>
      <div
        role="tabpanel"
        aria-label={
          view === "branches"
            ? "Branch comparison"
            : view === "workspace"
              ? "Local Agent workspace"
              : "Indexed file comparison"
        }
      >
        {view === "branches" ? (
          <BranchComparePanel key={props.activeRepoUrl} {...props} />
        ) : view === "workspace" ? (
          <AgentWorkspacePanel repoUrl={props.activeRepoUrl} />
        ) : (
          <DiffViewer
            key={props.activeRepoUrl}
            activeRepoUrl={props.activeRepoUrl}
          />
        )}
      </div>
    </div>
  );
}
