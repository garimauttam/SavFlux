import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  ExternalLink,
  FileCode2,
  GitBranch,
  GitCommitHorizontal,
  GitCompare,
  Github,
  Loader2,
} from "lucide-react";
import { apiFetch } from "../api";
import type { GitHubBranch } from "../types/workspace";
import { repoSlugFromUrl } from "../lib/github";

interface CompareFile {
  filename: string;
  previous_filename?: string | null;
  status: string;
  additions: number;
  deletions: number;
  changes: number;
  patch: string | null;
  patch_truncated: boolean;
}

interface CompareCommit {
  sha: string;
  message: string;
  author?: string | null;
  date?: string | null;
  url?: string | null;
}

interface BranchComparison {
  repo: string;
  base: string;
  head: string;
  status: string;
  ahead_by: number;
  behind_by: number;
  total_commits: number;
  changed_files: number;
  files: CompareFile[];
  files_truncated: boolean;
  commits: CompareCommit[];
  commits_truncated: boolean;
}

interface BranchComparePanelProps {
  activeRepoUrl: string | null;
  selectedBranch: string;
  defaultBranch: string;
  branches: GitHubBranch[];
  branchesLoading: boolean;
  githubConnected: boolean;
  onConnectGitHub: () => void;
}

function displayDate(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleDateString();
}

function statusLabel(result: BranchComparison): string {
  if (result.ahead_by === 0 && result.behind_by === 0)
    return "Branches are identical";
  if (result.ahead_by > 0 && result.behind_by > 0)
    return "Branches have diverged";
  if (result.ahead_by > 0) return "Compare branch is ahead";
  return "Compare branch is behind";
}

export function BranchComparePanel({
  activeRepoUrl,
  selectedBranch,
  defaultBranch,
  branches,
  branchesLoading,
  githubConnected,
  onConnectGitHub,
}: BranchComparePanelProps) {
  const statusId = useId();
  const requestRef = useRef<AbortController | null>(null);
  const previousSelection = useRef({ repo: "", branch: "" });
  const [base, setBase] = useState("");
  const [head, setHead] = useState("");
  const [result, setResult] = useState<BranchComparison | null>(null);
  const [expandedFile, setExpandedFile] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const slug = useMemo(() => repoSlugFromUrl(activeRepoUrl), [activeRepoUrl]);
  const branchNames = useMemo(
    () => branches.map((branch) => branch.name),
    [branches],
  );

  // Preserve manual selections across refreshed branch lists, but re-seed refs
  // when switching repositories or explicitly selecting a new top-bar branch.
  useEffect(() => {
    const newRepo = previousSelection.current.repo !== (slug ?? "");
    const newBranch = previousSelection.current.branch !== selectedBranch;
    previousSelection.current = { repo: slug ?? "", branch: selectedBranch };
    const initialBase = branchNames.includes(defaultBranch)
      ? defaultBranch
      : (branchNames[0] ?? "");
    setBase((current) =>
      !newRepo && branchNames.includes(current) ? current : initialBase,
    );
    setHead((current) => {
      if (
        (newRepo || newBranch || !branchNames.includes(current)) &&
        selectedBranch !== initialBase &&
        branchNames.includes(selectedBranch)
      )
        return selectedBranch;
      if (!newRepo && branchNames.includes(current)) return current;
      return branchNames.find((name) => name !== initialBase) ?? initialBase;
    });
  }, [slug, branchNames, defaultBranch, selectedBranch]);

  // Never show a response for old refs under the newly selected labels.
  useEffect(() => {
    requestRef.current?.abort();
    setLoading(false);
    setResult(null);
    setError("");
    setExpandedFile(null);
    return () => requestRef.current?.abort();
  }, [slug, base, head]);

  const blockedReason = loading
    ? "Comparing remote branches…"
    : branchesLoading
      ? "Loading branches from GitHub…"
      : !branchNames.length
        ? "No branches available. Check repository access or connect GitHub for private repositories and higher API limits."
        : !base || !head
          ? "Select a base branch and a compare branch."
          : base === head
            ? "Choose two different branches to compare."
            : "";

  async function compare() {
    if (!slug) {
      setError("Choose a GitHub repository before comparing branches.");
      return;
    }
    if (!base || !head || base === head) {
      setError("Choose two different branches to compare.");
      return;
    }
    if (blockedReason) return;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError("");
    setResult(null);
    setExpandedFile(null);
    try {
      const response = await apiFetch("/api/v1/github/compare", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo: slug, base, head }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail =
          typeof body.detail === "string"
            ? body.detail
            : `GitHub comparison failed (HTTP ${response.status}).`;
        const guidance =
          response.status === 404
            ? " Check that both refs exist; for a private repository, connect a GitHub account with read access."
            : response.status === 429
              ? " GitHub's API limit was reached. Wait before retrying, or connect GitHub for higher public-read limits."
              : response.status === 401 || response.status === 403
                ? " Check your SavFlux sign-in and GitHub connection permissions."
                : "";
        throw new Error(detail + guidance);
      }
      if (!controller.signal.aborted) setResult(body as BranchComparison);
    } catch (cause) {
      if (!controller.signal.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not compare these branches.",
        );
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  const compareUrl =
    slug && base && head
      ? `https://github.com/${slug}/compare/${encodeURIComponent(base)}...${encodeURIComponent(head)}`
      : null;

  if (!activeRepoUrl) {
    return (
      <div className="mx-auto flex max-w-2xl flex-col items-center rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] px-8 py-14 text-center">
        <GitCompare className="mb-4 h-7 w-7 text-[var(--sf-accent)]" />
        <h2 className="text-lg font-semibold">Compare repository branches</h2>
        <p className="mt-2 max-w-md text-sm leading-relaxed text-[var(--sf-text-dim)]">
          Select a repository in the top bar to compare its GitHub branches,
          review changed files, and see commits ahead or behind.
        </p>
      </div>
    );
  }

  if (!slug) {
    return (
      <div className="mx-auto max-w-2xl rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-6">
        <h2 className="font-semibold">
          Branch comparison needs a GitHub repository
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-[var(--sf-text-dim)]">
          The selected source is a local upload or does not identify a GitHub
          repo. Use Indexed files for indexed sources, or select a GitHub
          repository above.
        </p>
      </div>
    );
  }

  return (
    <section className="mx-auto w-full max-w-5xl space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-[var(--sf-accent)]">
            <GitCompare className="h-5 w-5" />
            <h2 className="text-lg font-semibold text-[var(--sf-text)]">
              Branch comparison
            </h2>
            <span className="rounded-full border border-[var(--sf-line)] bg-[var(--sf-raised)] px-2 py-0.5 text-[10px] font-medium text-[var(--sf-text-mute)]">
              READ ONLY
            </span>
          </div>
          <p className="mt-1 text-xs text-[var(--sf-text-dim)]">
            Compare remote GitHub refs for{" "}
            <span className="font-medium text-[var(--sf-text)]">{slug}</span>.
            This does not change either branch.
          </p>
        </div>
        {!githubConnected && (
          <button
            type="button"
            onClick={onConnectGitHub}
            className="inline-flex items-center gap-2 rounded-lg border border-[var(--sf-line)] px-3 py-2 text-xs text-[var(--sf-text)] hover:bg-[var(--sf-raised)]"
          >
            <Github className="h-3.5 w-3.5" /> Connect GitHub
          </button>
        )}
      </header>

      <div className="rounded-2xl border border-[var(--sf-line)] bg-[var(--sf-surface)] p-4 sm:p-5">
        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto] md:items-end">
          <label className="block min-w-0 text-xs font-medium text-[var(--sf-text-dim)]">
            Base branch
            <span className="relative mt-1.5 block">
              <GitBranch className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--sf-text-mute)]" />
              <select
                aria-label="Base branch"
                value={base}
                onChange={(event) => setBase(event.target.value)}
                disabled={!branches.length}
                className="h-10 w-full min-w-0 appearance-none rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-canvas)] pl-9 pr-9 text-sm text-[var(--sf-text)] outline-none focus:border-[var(--sf-accent)] disabled:opacity-60"
              >
                {branches.length === 0 && (
                  <option value="">
                    {branchesLoading
                      ? "Loading branches…"
                      : "No branches available"}
                  </option>
                )}
                {branches.map((branch) => (
                  <option key={branch.name} value={branch.name}>
                    {branch.name}
                    {branch.name === defaultBranch ? " · default" : ""}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--sf-text-mute)]" />
            </span>
          </label>
          <span className="hidden pb-3 text-xs text-[var(--sf-text-mute)] md:block">
            …
          </span>
          <label className="block min-w-0 text-xs font-medium text-[var(--sf-text-dim)]">
            Compare branch
            <span className="relative mt-1.5 block">
              <GitBranch className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--sf-text-mute)]" />
              <select
                aria-label="Compare branch"
                value={head}
                onChange={(event) => setHead(event.target.value)}
                disabled={!branches.length}
                className="h-10 w-full min-w-0 appearance-none rounded-lg border border-[var(--sf-line-strong)] bg-[var(--sf-canvas)] pl-9 pr-9 text-sm text-[var(--sf-text)] outline-none focus:border-[var(--sf-accent)] disabled:opacity-60"
              >
                {branches.length === 0 && (
                  <option value="">
                    {branchesLoading
                      ? "Loading branches…"
                      : "No branches available"}
                  </option>
                )}
                {branches.map((branch) => (
                  <option key={branch.name} value={branch.name}>
                    {branch.name}
                    {branch.name === defaultBranch ? " · default" : ""}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--sf-text-mute)]" />
            </span>
          </label>
          <button
            type="button"
            onClick={() => void compare()}
            disabled={Boolean(blockedReason)}
            aria-describedby={statusId}
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-[var(--sf-accent)] px-4 text-sm font-semibold text-white transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <GitCompare className="h-4 w-4" />
            )}
            {loading ? "Comparing…" : "Compare"}
          </button>
        </div>
        <p
          id={statusId}
          role="status"
          className="mt-3 text-xs leading-relaxed text-[var(--sf-text-dim)]"
        >
          {blockedReason || `Ready to compare ${base} → ${head}.`}
        </p>
        {!githubConnected && (
          <p className="mt-2 text-xs leading-relaxed text-[var(--sf-text-mute)]">
            Public repositories can be compared without connecting GitHub.
            Connect for private repository access or higher API limits.
          </p>
        )}
        {error && (
          <p
            role="alert"
            className="mt-3 rounded-lg border border-red-400/20 bg-red-400/5 px-3 py-2 text-xs leading-relaxed text-red-300"
          >
            {error}
          </p>
        )}
      </div>

      {result && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)] px-4 py-3">
            <div>
              <p className="text-sm font-semibold text-[var(--sf-text)]">
                {statusLabel(result)}
              </p>
              <p className="mt-1 break-all text-xs text-[var(--sf-text-dim)]">
                {result.base} → {result.head}
              </p>
              <p className="mt-0.5 text-xs text-[var(--sf-text-dim)]">
                {result.changed_files} changed{" "}
                {result.changed_files === 1 ? "file" : "files"} ·{" "}
                {result.total_commits}{" "}
                {result.total_commits === 1 ? "commit" : "commits"}
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <span className="inline-flex items-center gap-1 text-emerald-400">
                <ArrowUp className="h-3.5 w-3.5" /> {result.ahead_by} ahead
              </span>
              <span className="inline-flex items-center gap-1 text-amber-300">
                <ArrowDown className="h-3.5 w-3.5" /> {result.behind_by} behind
              </span>
              {compareUrl && (
                <a
                  href={compareUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-[var(--sf-accent)] hover:underline"
                >
                  Open on GitHub <ExternalLink className="h-3 w-3" />
                </a>
              )}
            </div>
          </div>

          {result.files.length === 0 ? (
            <div className="rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)] px-4 py-8 text-center text-sm text-[var(--sf-text-dim)]">
              No file changes between these refs.
            </div>
          ) : (
            <section className="overflow-hidden rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)]">
              <div className="flex items-center justify-between border-b border-[var(--sf-line)] px-4 py-3">
                <h3 className="text-sm font-semibold text-[var(--sf-text)]">
                  Changed files
                </h3>
                <span className="text-[11px] text-[var(--sf-text-mute)]">
                  Select a file to inspect its patch
                </span>
              </div>
              <div className="divide-y divide-[var(--sf-line)]">
                {result.files.map((file) => {
                  const open = expandedFile === file.filename;
                  return (
                    <article key={file.filename}>
                      <button
                        type="button"
                        aria-expanded={open}
                        onClick={() =>
                          setExpandedFile(open ? null : file.filename)
                        }
                        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left transition hover:bg-[var(--sf-raised)]"
                      >
                        <FileCode2 className="h-4 w-4 shrink-0 text-[var(--sf-text-mute)]" />
                        <span className="min-w-0 flex-1 truncate font-mono text-xs text-[var(--sf-text)]">
                          {file.filename}
                        </span>
                        {file.previous_filename && (
                          <span className="text-[10px] text-[var(--sf-text-mute)]">
                            renamed from {file.previous_filename}
                          </span>
                        )}
                        <span className="rounded bg-[var(--sf-raised)] px-1.5 py-0.5 text-[10px] text-[var(--sf-text-dim)]">
                          {file.status}
                        </span>
                        <span className="text-xs">
                          <span className="text-emerald-400">
                            +{file.additions}
                          </span>{" "}
                          <span className="text-rose-400">
                            −{file.deletions}
                          </span>
                        </span>
                        <ChevronDown
                          className={`h-3.5 w-3.5 text-[var(--sf-text-mute)] transition ${open ? "rotate-180" : ""}`}
                        />
                      </button>
                      {open && (
                        <div className="border-t border-[var(--sf-line)] bg-[var(--sf-canvas)] px-3 py-3 sm:px-4">
                          {file.patch ? (
                            <pre className="max-h-[480px] overflow-auto rounded-lg border border-[var(--sf-line)] p-3 sf-code-typography">
                              {file.patch.split("\n").map((line, index) => {
                                const color = line.startsWith("+")
                                  ? "text-emerald-300 bg-emerald-500/5"
                                  : line.startsWith("-")
                                    ? "text-rose-300 bg-rose-500/5"
                                    : line.startsWith("@@")
                                      ? "text-cyan-300 bg-cyan-500/5"
                                      : "text-[var(--sf-text-dim)]";
                                return (
                                  <span
                                    key={index}
                                    className={`block whitespace-pre ${color}`}
                                  >
                                    {line || " "}
                                  </span>
                                );
                              })}
                            </pre>
                          ) : (
                            <p className="text-xs text-[var(--sf-text-dim)]">
                              GitHub did not provide a text patch for this file
                              (it may be binary or too large). Open the
                              comparison on GitHub for its full representation.
                            </p>
                          )}
                          {file.patch_truncated && (
                            <p className="mt-2 text-[11px] text-amber-300">
                              Patch preview was shortened to keep the response
                              bounded. Open the comparison on GitHub for the
                              full diff.
                            </p>
                          )}
                        </div>
                      )}
                    </article>
                  );
                })}
              </div>
              {result.files_truncated && (
                <p className="border-t border-[var(--sf-line)] px-4 py-3 text-xs text-amber-300">
                  Showing the first 100 files. Open the GitHub comparison for
                  the complete file list.
                </p>
              )}
            </section>
          )}

          {result.commits.length > 0 && (
            <details className="rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)]">
              <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-semibold text-[var(--sf-text)]">
                <GitCommitHorizontal className="h-4 w-4 text-[var(--sf-text-mute)]" />{" "}
                Commits in comparison{" "}
                <span className="text-xs font-normal text-[var(--sf-text-mute)]">
                  ({result.commits.length}
                  {result.commits_truncated ? "+" : ""})
                </span>
              </summary>
              <div className="divide-y divide-[var(--sf-line)] border-t border-[var(--sf-line)]">
                {result.commits.map((commit) => (
                  <div
                    key={commit.sha}
                    className="flex flex-wrap items-baseline gap-x-2 gap-y-1 px-4 py-2.5 text-xs"
                  >
                    <code className="text-[var(--sf-accent)]">
                      {commit.sha}
                    </code>
                    <span className="min-w-0 flex-1 truncate text-[var(--sf-text)]">
                      {commit.message}
                    </span>
                    <span className="text-[var(--sf-text-mute)]">
                      {commit.author}
                      {commit.date ? ` · ${displayDate(commit.date)}` : ""}
                    </span>
                  </div>
                ))}
              </div>
              {result.commits_truncated && (
                <p className="px-4 py-2 text-[11px] text-[var(--sf-text-mute)]">
                  Showing the first 50 commits.
                </p>
              )}
            </details>
          )}
        </div>
      )}

      {!result && !error && !branchesLoading && branches.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-[var(--sf-line)] bg-[var(--sf-surface)] px-4 py-3 text-xs leading-relaxed text-[var(--sf-text-mute)]">
          <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--sf-accent)]" />
          SavFlux reads the comparison from GitHub; no branch is checked out,
          changed, or pushed.
        </div>
      )}
    </section>
  );
}
