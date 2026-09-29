/**
 * RepoBrowser.tsx — the Repositories destination.
 *
 * This is what replaces the old 401-line `IngestPanel` sidebar. The sidebar did
 * four unrelated jobs stacked vertically — paste a URL, upload files, pick a
 * repo, wipe the index — which is why it was 401 lines and why nothing on it
 * looked related to anything else. Split by intent, each is small:
 *
 *   Connect  → a dialog, opened from the top bar
 *   Browse   → this panel
 *   Index    → one button per repository
 *   Clear    → one button per indexed repository, in the same row
 *
 * The important behaviour change: the list comes from GitHub, so the user picks
 * a repository by name rather than by correctly-typed URL, and the branch comes
 * from the same repository instead of being a second free-text field that is
 * usually wrong.
 */

import { readIngestStream } from "../../lib/ingestStream";
import type { OnIndexed } from "../../lib/repositorySelection";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Cloud,
  Github,
  GitBranch,
  Globe,
  HardDrive,
  Loader2,
  Lock,
  RefreshCw,
  Search,
  Server,
  Trash2,
  Upload,
} from "lucide-react";
import { RepositoryIndexForm } from "../RepositoryIndexForm";
import { repoDisplayName, repoSlugFromUrl } from "../../lib/github";
import { apiFetch } from "../../api";
import { apiError } from "../../hooks/useIntegrations";
import type { GitHubRepo, GitHubStatus, ModelStatus } from "../../types/workspace";

interface RepoBrowserProps {
  status: GitHubStatus | null;
  onConnect: () => void;
  indexedRepos: { repo_url: string; chunk_count: number }[];
  indexedFiles: { file_name: string; repo_url: string; source: string }[];
  activeRepoUrl: string | null;
  branch: string;
  onSelectRepo: (url: string | null) => void;
  onIndexed: OnIndexed;
  models: ModelStatus | null;
}

function isIndexed(repo: GitHubRepo, indexed: string[]): boolean {
  return indexed.some((u) => u.toLowerCase().includes(repo.full_name.toLowerCase()));
}

export function RepoBrowser({
  status,
  onConnect,
  indexedRepos,
  indexedFiles,
  activeRepoUrl,
  branch,
  onSelectRepo,
  onIndexed,
  models,
}: RepoBrowserProps) {
  const [repos, setRepos] = useState<GitHubRepo[]>([]);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ingesting, setIngesting] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ repo: string; message: string } | null>(null);
  const [clearing, setClearing] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const connected = Boolean(status?.connected && status?.valid);
  const indexedUrls = indexedRepos.map((r) => r.repo_url);

  const load = useCallback(async () => {
    if (!connected) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/v1/github/repos?per_page=60${query ? `&q=${encodeURIComponent(query)}` : ""}`,
      );
      if (!res.ok) throw new Error(await apiError(res, "Could not load repositories"));
      const data = await res.json();
      setRepos(data.repos ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load repositories.");
    } finally {
      setLoading(false);
    }
  }, [connected, query]);

  useEffect(() => {
    if (!connected) return;
    // Debounced: a search per keystroke hits GitHub's rate limit, which for an
    // unauthenticated-looking token is 60 requests an hour.
    const t = window.setTimeout(() => void load(), query ? 350 : 0);
    return () => window.clearTimeout(t);
  }, [connected, load, query]);

  const ingest = useCallback(
    async (repo: GitHubRepo) => {
      if (ingesting) return;
      setIngesting(repo.full_name);
      setError(null);
      setProgress({ repo: repo.full_name, message: "Starting…" });
      try {
        const res = await apiFetch("/api/v1/ingest/github", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            repo_url: repo.clone_url ?? `https://github.com/${repo.full_name}`,
            branch: repoSlugFromUrl(activeRepoUrl)?.toLowerCase() === repo.full_name.toLowerCase() ? branch : "",
          }),
        });
        if (!res.ok) throw new Error(await apiError(res, "Could not start the index"));
        await readIngestStream(res, (event) => {
          if (!event.message) return;
          setProgress({
            repo: repo.full_name,
            message: event.files_indexed ? `${event.message} · ${event.files_indexed} files` : event.message,
          });
        });
        onIndexed({
          repoUrl: repo.clone_url ?? `https://github.com/${repo.full_name}`,
          branch: repoSlugFromUrl(activeRepoUrl)?.toLowerCase() === repo.full_name.toLowerCase() ? branch : "",
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Ingest failed.");
      } finally {
        setIngesting(null);
        setProgress(null);
      }
    },
    [activeRepoUrl, branch, ingesting, onIndexed, onSelectRepo],
  );

  const ingestUrl = useCallback(async (url: string, selectedBranch: string) => {
    if (!url || ingesting) return false;
    setIngesting(url);
    setError(null);
    setProgress({ repo: url, message: "Cloning…" });
    try {
      const res = await apiFetch("/api/v1/ingest/github", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo_url: url,
          branch: selectedBranch,
        }),
      });
      if (!res.ok) throw new Error(await apiError(res, "Could not start the index"));
      await readIngestStream(res, (event) => {
        if (!event.message) return;
        setProgress({
          repo: url,
          message: event.files_indexed ? `${event.message} · ${event.files_indexed} files` : event.message,
        });
      });
      onIndexed({ repoUrl: url, branch: selectedBranch });
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Indexing failed.");
      return false;
    } finally {
      setIngesting(null);
      setProgress(null);
    }
  }, [ingesting, onIndexed]);

  const clearRepo = useCallback(
    async (url: string) => {
      setClearing(url);
      setError(null);
      try {
        // The endpoint takes `repo_url` as a query parameter, not a body — a
        // body here is silently ignored, which looks like "Clear" did nothing.
        const res = await apiFetch(
          `/api/v1/ingest/clear?repo_url=${encodeURIComponent(url)}`,
          { method: "DELETE" },
        );
        if (!res.ok) throw new Error(await apiError(res, "Could not clear the index"));
        onIndexed();
        if (url === activeRepoUrl) onSelectRepo(null);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not clear the index.");
      } finally {
        setClearing(null);
      }
    },
    [activeRepoUrl, onIndexed, onSelectRepo],
  );

  const upload = useCallback(
    async (files: FileList | null) => {
      if (!files?.length) return;
      setError(null);
      const body = new FormData();
      Array.from(files).forEach((f) => body.append("files", f));
      try {
        setProgress({ repo: files.length === 1 ? files[0].name : `${files.length} files`, message: "Uploading…" });
        const res = await apiFetch("/api/v1/ingest/files", { method: "POST", body });
        if (!res.ok) throw new Error(await apiError(res, "Upload failed"));
        onIndexed();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Upload failed.");
      } finally {
        setProgress(null);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [onIndexed],
  );

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-3xl space-y-6">
          <header>
            <h1 className="sf-text text-[17px] font-semibold tracking-tight">Repositories</h1>
            <p className="sf-mute mt-1 text-[12.5px]">
              Index a repository and the agent can read it, review it, and open pull requests against it.
            </p>
          </header>

          {error && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-xl border px-3.5 py-2.5 text-[12.5px]"
              style={{
                borderColor: "rgba(248,113,113,0.3)",
                background: "rgba(248,113,113,0.1)",
                color: "#fca5a5",
              }}
            >
              <AlertTriangle className="mt-px h-4 w-4 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {progress && (
            <div className="flex items-center gap-2.5 rounded-xl border sf-line sf-raised px-3.5 py-2.5 text-[12.5px]">
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin sf-accent" />
              <span className="sf-text">
                <span className="sf-mono">{progress.repo}</span> — {progress.message}
              </span>
            </div>
          )}

          {!connected ? (
            <div className="rounded-2xl border sf-line sf-surface p-8 text-center">
              <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-purple-500/10 text-purple-300">
                <Github className="h-6 w-6" />
              </div>
              <h2 className="sf-text mt-4 text-[15px] font-semibold">Connect your GitHub account</h2>
              <p className="sf-mute mx-auto mt-1.5 max-w-md text-[12.5px] leading-relaxed">
                SavFlux reads your repository list, branches, private code and pull requests directly
                from GitHub. A token with the <code className="sf-mono">repo</code> scope is enough —
                it stays on this machine.
              </p>
              <button type="button" onClick={onConnect} className="sf-btn sf-btn-primary mt-5">
                <Github className="h-3.5 w-3.5" /> Connect GitHub
              </button>
              {status?.message && !status?.valid && (
                <p className="mt-4 text-[12px]" style={{ color: "var(--sf-warn)" }}>
                  {status.message}
                </p>
              )}
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search className="sf-mute pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
                  <input
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search your repositories…"
                    className="sf-input pl-9"
                    aria-label="Search repositories"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => void load()}
                  disabled={loading}
                  className="sf-iconbtn h-9 w-9 border sf-line"
                  title="Refresh"
                  aria-label="Refresh repositories"
                >
                  <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                </button>
              </div>

              {repos.length === 0 && !loading && (
                <div className="rounded-2xl border sf-line sf-surface px-6 py-10 text-center">
                  <p className="sf-dim text-[13px]">
                    {query ? `No repositories match “${query}”.` : "No repositories found for this account."}
                  </p>
                </div>
              )}

              <ul className="space-y-2">
                {repos.map((repo) => {
                  const done = isIndexed(repo, indexedUrls);
                  const active = activeRepoUrl?.toLowerCase().includes(repo.full_name.toLowerCase());
                  const busy = ingesting === repo.full_name;
                  return (
                    <li
                      key={repo.full_name}
                      className={[
                        "rounded-xl border sf-line sf-surface p-3.5 transition-colors",
                        active ? "border-[var(--sf-accent-line)]" : "hover:border-[var(--sf-line-strong)]",
                      ].join(" ")}
                    >
                      <div className="flex items-start gap-3">
                        <div className="sf-iconbtn mt-0.5 h-8 w-8 shrink-0">
                          {repo.private ? <Lock className="h-4 w-4" /> : <Globe className="h-4 w-4" />}
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                            <a
                              href={repo.html_url ?? `https://github.com/${repo.full_name}`}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="sf-text truncate text-[13.5px] font-medium hover:underline"
                            >
                              {repo.full_name}
                            </a>
                            {done && (
                              <span className="sf-chip sf-chip-good">
                                <Check className="h-2.5 w-2.5" /> indexed
                              </span>
                            )}
                            {repo.archived && <span className="sf-chip">archived</span>}
                            {repo.language && <span className="sf-mute text-[11.5px]">{repo.language}</span>}
                          </div>
                          {repo.description && (
                            <p className="sf-mute mt-1 line-clamp-2 text-[12px] leading-relaxed">
                              {repo.description}
                            </p>
                          )}
                          <div className="sf-mute mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px]">
                            {repo.default_branch && (
                              <span className="inline-flex items-center gap-1">
                                <GitBranch className="h-3 w-3" /> {repo.default_branch}
                              </span>
                            )}
                            <span>★ {repo.stars}</span>
                            {repo.pushed_at && (
                              <span>updated {new Date(repo.pushed_at).toLocaleDateString()}</span>
                            )}
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => void ingest(repo)}
                            disabled={Boolean(ingesting) || Boolean(!models?.available && !done)}
                            title={
                              !models?.available && !done
                                ? `Indexing embeds locally and needs the model running. ${models?.hint ?? ""}`
                                : done
                                  ? "Re-index from GitHub"
                                  : "Index this repository"
                            }
                            className="sf-btn sf-btn-secondary"
                          >
                            {busy ? (
                              <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            ) : done ? (
                              <RefreshCw className="h-3.5 w-3.5" />
                            ) : (
                              <Cloud className="h-3.5 w-3.5" />
                            )}
                            {busy ? "Indexing" : done ? "Re-index" : "Index"}
                          </button>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </>
          )}

          {/* Indexed right now — the repositories already in the local index. */}
          {indexedRepos.length > 0 && (
            <section>
              <h2 className="sf-mute mb-2 text-[11px] font-semibold uppercase tracking-[0.08em]">
                In this index
              </h2>
              <ul className="space-y-1.5">
                {indexedRepos.map((r) => {
                  const name = repoDisplayName(r.repo_url);
                  const count = indexedFiles.filter((f) => f.repo_url === r.repo_url).length;
                  return (
                    <li
                      key={r.repo_url}
                      className="flex items-center gap-3 rounded-xl border sf-line sf-surface px-3.5 py-2.5"
                    >
                      <HardDrive className="h-4 w-4 shrink-0 sf-mute" />
                      <button
                        type="button"
                        onClick={() => onSelectRepo(r.repo_url)}
                        className="min-w-0 flex-1 text-left"
                      >
                        <span className="sf-mono sf-text block truncate text-[12.5px]">{name}</span>
                        <span className="sf-mute text-[11.5px]">
                          {count} {count === 1 ? "file" : "files"} · {r.chunk_count} chunks
                        </span>
                      </button>
                      {r.repo_url === activeRepoUrl && <span className="sf-chip sf-chip-accent">active</span>}
                      <button
                        type="button"
                        onClick={() => void clearRepo(r.repo_url)}
                        disabled={clearing === r.repo_url}
                        className="sf-iconbtn h-7 w-7"
                        title="Remove from the index"
                        aria-label={`Remove ${name} from the index`}
                      >
                        {clearing === r.repo_url ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Trash2 className="h-3.5 w-3.5" />
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {/* No account: a public URL or a folder of files still works. */}
          <section>
            <h2 className="sf-mute mb-2 text-[11px] font-semibold uppercase tracking-[0.08em]">
              Without a GitHub connection
            </h2>
            <div className="rounded-xl border sf-line sf-surface p-3.5">
              <p className="sf-mute text-[12px]">
                Index a public repository by URL, or upload files directly. Both are free and need no
                GitHub connection.
              </p>
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <RepositoryIndexForm busy={!!ingesting} onIndex={ingestUrl} />
                <button
                  type="button"
                  className="sf-btn sf-btn-secondary"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Upload className="h-3.5 w-3.5" /> Upload files
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) => void upload(e.target.files)}
                  aria-label="Upload files"
                />
              </div>
              {models && !models.available && (
                <p className="sf-mute mt-2.5 flex items-start gap-1.5 text-[11.5px]">
                  <Server className="mt-px h-3.5 w-3.5 shrink-0" />
                  {models.hint}
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
