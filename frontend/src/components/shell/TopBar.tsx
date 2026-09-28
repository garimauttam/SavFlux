/**
 * TopBar.tsx — identity, scope and account, in one 48px row.
 *
 * WHAT BELONGS HERE
 * -----------------
 * Three questions, asked constantly: *what am I working on*, *what is
 * answering*, and *am I connected to GitHub*. Those were previously spread
 * across a 401-line sidebar, a `.env` file and a tab, which meant the answer to
 * all three was "look around".
 *
 * Everything else the old strip carried — a ⌘K hint, a theme toggle — is here
 * too, but as controls rather than labels.
 *
 * The repo and branch pickers read the indexed list the workspace already holds
 * rather than fetching their own: two sources of truth for "which repo am I on"
 * is how the header and the agent end up disagreeing about the scope of a
 * question.
 */

import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Command,
  Cpu,
  ExternalLink,
  Github,
  GitBranch,
  Loader2,
  LogOut,
  Moon,
  PanelRight,
  Search,
  Sun,
} from "lucide-react";
import { repoDisplayName } from "../../lib/github";
import type { GitHubStatus, ModelStatus } from "../../types/workspace";

interface RepoOption {
  url: string;
  name: string;
}

interface TopBarProps {
  repos: RepoOption[];
  activeRepoUrl: string | null;
  onSelectRepo: (url: string | null) => void;
  branch: string;
  branches: string[];
  onSelectBranch: (branch: string) => void;
  branchesLoading: boolean;
  indexCount: number;
  github: GitHubStatus | null;
  models: ModelStatus | null;
  modelsBusy: boolean;
  onSelectModel: (name: string) => void;
  onConnectGitHub: () => void;
  onSignOut: () => void;
  onOpenCommandPalette: () => void;
  contextOpen: boolean;
  onToggleContext: () => void;
  isDark: boolean;
  onToggleTheme: () => void;
}

/** A dropdown that closes on outside click and Escape. */
function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);
  return ref;
}

export function TopBar({
  repos,
  activeRepoUrl,
  onSelectRepo,
  branch,
  branches,
  onSelectBranch,
  branchesLoading,
  indexCount,
  github,
  models,
  modelsBusy,
  onSelectModel,
  onConnectGitHub,
  onSignOut,
  onOpenCommandPalette,
  contextOpen,
  onToggleContext,
  isDark,
  onToggleTheme,
}: TopBarProps) {
  const [openMenu, setOpenMenu] = useState<"repo" | "branch" | "model" | "account" | null>(null);
  const [modelQuery, setModelQuery] = useState("");
  const close = () => setOpenMenu(null);
  const menuRef = useDismiss(openMenu !== null, close);

  const connected = Boolean(github?.connected && github?.valid);
  const modelState = models?.available ? "good" : models?.reachable ? "warn" : "bad";

  const filteredModels = (models?.models ?? []).filter((m) =>
    modelQuery ? m.name.toLowerCase().includes(modelQuery.toLowerCase()) : true,
  );
  const suggestions = (models?.suggested ?? []).filter((s) =>
    modelQuery ? s.name.toLowerCase().includes(modelQuery.toLowerCase()) : true,
  );

  return (
    <header className="sf-surface flex h-12 shrink-0 items-center gap-2 border-b sf-line px-3">
      {/* Scope — which repository the agent is looking at */}
      <div className="relative" ref={menuRef}>
        <button
          type="button"
          onClick={() => setOpenMenu(openMenu === "repo" ? null : "repo")}
          className="sf-btn sf-btn-secondary max-w-[260px]"
          aria-haspopup="listbox"
          aria-expanded={openMenu === "repo"}
          title={activeRepoUrl ? repoDisplayName(activeRepoUrl) : "No repository selected"}
        >
          <Github className="h-3.5 w-3.5 shrink-0 opacity-70" />
          <span className="truncate">
            {activeRepoUrl ? repoDisplayName(activeRepoUrl) : "No repository"}
          </span>
          <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
        </button>

        {openMenu === "repo" && (
          <div
            role="listbox"
            className="absolute left-0 top-11 z-50 w-[320px] overflow-hidden rounded-xl border sf-line shadow-2xl shadow-black/40 sf-overlay"
          >
            <div className="border-b sf-line px-3 py-2 text-[11px] sf-mute">
              {repos.length} indexed {repos.length === 1 ? "repository" : "repositories"}
              {indexCount > 0 && ` · ${indexCount} files`}
            </div>
            <div className="max-h-72 overflow-y-auto p-1">
              {repos.length === 0 && (
                <div className="px-3 py-4 text-center text-[12px] sf-mute">
                  Nothing indexed yet.
                  <button
                    type="button"
                    onClick={() => { close(); onConnectGitHub(); }}
                    className="mt-2 block w-full sf-btn sf-btn-primary"
                  >
                    Connect a repository
                  </button>
                </div>
              )}
              {repos.map((r) => {
                const selected = r.url === activeRepoUrl;
                return (
                  <button
                    key={r.url}
                    role="option"
                    aria-selected={selected}
                    onClick={() => { onSelectRepo(r.url); close(); }}
                    className={[
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[12.5px] transition-colors",
                      selected ? "sf-accent-soft" : "hover:bg-[var(--sf-raised)]",
                    ].join(" ")}
                  >
                    <Check
                      className={`h-3.5 w-3.5 shrink-0 ${selected ? "sf-accent" : "opacity-0"}`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="sf-text block truncate font-medium">{r.name}</span>
                      <span className="sf-mute block truncate text-[11px]">{r.url}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Branch — only meaningful once a repository is selected */}
      {activeRepoUrl && (
        <div className="relative">
          <button
            type="button"
            onClick={() => setOpenMenu(openMenu === "branch" ? null : "branch")}
            className="sf-btn sf-btn-ghost max-w-[180px]"
            aria-haspopup="listbox"
            aria-expanded={openMenu === "branch"}
            title="Branch used for the next index"
          >
            <GitBranch className="h-3.5 w-3.5 shrink-0 opacity-70" />
            <span className="truncate">{branch || "default"}</span>
            <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
          </button>
          {openMenu === "branch" && (
            <div
              role="listbox"
              className="absolute left-0 top-10 z-50 w-[280px] overflow-hidden rounded-xl border sf-line shadow-2xl shadow-black/40 sf-overlay"
            >
              <div className="border-b sf-line px-3 py-2 text-[11px] sf-mute">
                {branchesLoading
                  ? "Loading branches…"
                  : branches.length
                    ? "Branch to index"
                    : "No branch list yet — index the repository to read its branches from GitHub."}
              </div>
              <div className="max-h-72 overflow-y-auto p-1">
                {branches.map((b) => (
                  <button
                    key={b}
                    role="option"
                    aria-selected={b === branch}
                    onClick={() => { onSelectBranch(b); close(); }}
                    className={[
                      "sf-mono flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12px] transition-colors",
                      b === branch ? "sf-accent-soft sf-accent" : "hover:bg-[var(--sf-raised)]",
                    ].join(" ")}
                  >
                    <GitBranch className="h-3 w-3 shrink-0 opacity-50" />
                    <span className="truncate">{b}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="flex-1" />

      {/* Search */}
      <button
        type="button"
        onClick={onOpenCommandPalette}
        className="sf-btn sf-btn-ghost hidden md:inline-flex"
        title="Search files, sections and commands"
      >
        <Search className="h-3.5 w-3.5 opacity-70" />
        <span className="sf-mute text-[12px]">Search</span>
        <kbd className="sf-mute ml-1 inline-flex items-center gap-0.5 rounded border sf-line px-1 py-px text-[10px]">
          <Command className="h-2.5 w-2.5" />K
        </kbd>
      </button>

      {/* Model — the $0 promise, made visible */}
      <div className="relative">
        <button
          type="button"
          onClick={() => { setOpenMenu(openMenu === "model" ? null : "model"); setModelQuery(""); }}
          className="sf-btn sf-btn-secondary max-w-[230px]"
          aria-haspopup="listbox"
          aria-expanded={openMenu === "model"}
          title={models?.hint ?? "Which model is answering"}
        >
          {modelsBusy ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
          ) : (
            <span
              aria-hidden
              className={[
                "h-1.5 w-1.5 shrink-0 rounded-full",
                modelState === "good" ? "bg-emerald-400"
                  : modelState === "warn" ? "bg-amber-400"
                  : "bg-rose-400",
              ].join(" ")}
              style={modelState === "good" ? { boxShadow: "0 0 0 3px rgba(52,211,153,0.15)" } : undefined}
            />
          )}
          <Cpu className="h-3.5 w-3.5 shrink-0 opacity-60" />
          <span className="truncate">
            {models?.chat_model ?? (models === null ? "Checking…" : "No model")}
          </span>
          <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
        </button>

        {openMenu === "model" && (
          <div className="absolute right-0 top-11 z-50 w-[380px] overflow-hidden rounded-xl border sf-line shadow-2xl shadow-black/40 sf-overlay">
            <div className="border-b sf-line p-3">
              <p className="sf-text text-[12.5px] font-medium">
                {models?.provider_label ?? "Local model"}
                {models?.free && <span className="sf-chip sf-chip-good ml-2">free</span>}
              </p>
              <p className="sf-mute mt-0.5 text-[11px]">
                {models?.available
                  ? "Answers run on this machine. Nothing is billed."
                  : models?.hint ?? "Checking the local model runtime…"}
              </p>
              {models && !models.available && (
                <code className="sf-mono sf-raised mt-2 block w-full overflow-x-auto rounded-lg border sf-line px-2 py-1.5 text-[11px] sf-text">
                  {models.reachable
                    ? `ollama pull ${models.chat_model}`
                    : "ollama serve"}
                </code>
              )}
            </div>

            <div className="border-b sf-line p-2">
              <input
                autoFocus
                value={modelQuery}
                onChange={(e) => setModelQuery(e.target.value)}
                placeholder="Filter installed models…"
                className="sf-input"
                aria-label="Filter models"
              />
            </div>

            <div className="max-h-72 overflow-y-auto p-1">
              {filteredModels.length === 0 && suggestions.length === 0 && (
                <p className="px-3 py-4 text-center text-[12px] sf-mute">
                  {models?.reachable
                    ? "No models installed on this machine."
                    : "Ollama is not reachable, so no models can be listed."}
                </p>
              )}
              {filteredModels.map((m) => {
                const active = m.name === models?.chat_model;
                return (
                  <button
                    key={m.name}
                    role="option"
                    aria-selected={active}
                    onClick={() => { void onSelectModel(m.name); close(); }}
                    className={[
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors",
                      active ? "sf-accent-soft" : "hover:bg-[var(--sf-raised)]",
                    ].join(" ")}
                  >
                    <Check className={`h-3.5 w-3.5 shrink-0 ${active ? "sf-accent" : "opacity-0"}`} />
                    <span className="min-w-0 flex-1">
                      <span className="sf-mono sf-text block truncate text-[12px]">{m.name}</span>
                      <span className="sf-mute block text-[11px]">
                        {[m.parameter_size, m.quantization, m.size_gb ? `${m.size_gb} GB` : null]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </span>
                  </button>
                );
              })}

              {suggestions.length > 0 && (
                <>
                  <div className="sf-mute px-2.5 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-[0.08em]">
                    Not installed — pull to use
                  </div>
                  {suggestions.map((s) => (
                    <div
                      key={s.name}
                      className="flex items-center gap-2 rounded-lg px-2.5 py-2"
                      title="Run the pull command, then pick it here"
                    >
                      <span className="sf-mono sf-dim min-w-0 flex-1 truncate text-[12px]">
                        {s.name}
                      </span>
                      <span className="sf-mute hidden text-[11px] sm:block">{s.why}</span>
                      <code className="sf-mono sf-raised shrink-0 rounded border sf-line px-1.5 py-0.5 text-[10px] sf-dim">
                        ollama pull
                      </code>
                    </div>
                  ))}
                </>
              )}
            </div>

            {models && (
              <div className="sf-mute border-t sf-line px-3 py-2 text-[11px]">
                Retrieval embeds locally with{" "}
                <span className="sf-mono sf-dim">{models.embedding_model}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Account */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpenMenu(openMenu === "account" ? null : "account")}
          className="sf-iconbtn h-8 w-8 overflow-hidden"
          aria-label={connected ? `GitHub account ${github?.user?.login}` : "Connect GitHub"}
          title={connected ? `Signed in as ${github?.user?.login}` : "Connect GitHub"}
        >
          {connected && github?.user?.avatar_url ? (
            <img
              src={github.user.avatar_url}
              alt=""
              className="h-6 w-6 rounded-full ring-1 ring-white/15"
              referrerPolicy="no-referrer"
            />
          ) : (
            <Github className="h-[18px] w-[18px]" />
          )}
        </button>

        {openMenu === "account" && (
          <div className="absolute right-0 top-11 z-50 w-[280px] overflow-hidden rounded-xl border sf-line shadow-2xl shadow-black/40 sf-overlay">
            {connected ? (
              <>
                <div className="border-b sf-line p-3">
                  <p className="sf-text text-[13px] font-medium">{github?.user?.name || github?.user?.login}</p>
                  <p className="sf-mute text-[11px]">
                    @{github?.user?.login}
                    {github?.source === "env" && " · from GITHUB_TOKEN"}
                  </p>
                </div>
                <a
                  href={github?.user?.html_url ?? "https://github.com"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="sf-btn sf-btn-ghost mt-1 w-full justify-start px-3"
                >
                  <ExternalLink className="h-3.5 w-3.5" /> Open profile
                </a>
                <button
                  type="button"
                  onClick={() => { close(); onConnectGitHub(); }}
                  className="sf-btn sf-btn-ghost w-full justify-start px-3"
                >
                  <Github className="h-3.5 w-3.5" /> Connect a different account
                </button>
              </>
            ) : (
              <div className="p-3">
                <p className="sf-text text-[13px] font-medium">Not connected to GitHub</p>
                <p className="sf-mute mt-1 text-[11.5px] leading-relaxed">
                  {github?.message ??
                    "Connect an account to browse your repositories, index private ones, and open pull requests."}
                </p>
                <button
                  type="button"
                  onClick={() => { close(); onConnectGitHub(); }}
                  className="sf-btn sf-btn-primary mt-3 w-full"
                >
                  <Github className="h-3.5 w-3.5" /> Connect GitHub
                </button>
              </div>
            )}
            <div className="border-t sf-line p-1">
              <button
                type="button"
                onClick={() => { close(); onSignOut(); }}
                className="sf-btn sf-btn-ghost w-full justify-start px-3 text-[var(--sf-text-dim)]"
              >
                <LogOut className="h-3.5 w-3.5" /> Sign out of SavFlux
              </button>
            </div>
          </div>
        )}
      </div>

      <button
        type="button"
        onClick={onToggleContext}
        aria-pressed={contextOpen}
        aria-label={contextOpen ? "Hide the context panel" : "Show the context panel"}
        title={contextOpen ? "Hide the context panel" : "Show cited code beside the conversation"}
        className={[
          "sf-iconbtn h-8 w-8",
          contextOpen ? "sf-accent-soft sf-accent" : "",
        ].join(" ")}
      >
        <PanelRight className="h-4 w-4" />
      </button>

      <button
        type="button"
        onClick={onToggleTheme}
        aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
        title={isDark ? "Light theme" : "Dark theme"}
        className="sf-iconbtn h-8 w-8"
      >
        {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
      </button>
    </header>
  );
}
