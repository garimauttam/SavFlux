import { useEffect, useId, useRef, useState } from "react";
import { GitBranch, Loader2, RefreshCw, Search } from "lucide-react";
import { apiFetch } from "../api";
import { apiError } from "../hooks/useIntegrations";
import { repoSlugFromUrl } from "../lib/github";

interface RemoteBranch {
  name: string;
  protected: boolean;
}

/** Keyed by repository: old requests and selections cannot leak into a new URL. */
function RemoteBranches({ slug, busy, branch, onChange }: {
  slug: string;
  busy: boolean;
  branch: string;
  onChange: (branch: string) => void;
}) {
  const id = useId();
  const [branches, setBranches] = useState<RemoteBranch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nextPage, setNextPage] = useState<number | null>(null);
  const [request, setRequest] = useState({ page: 1, attempt: 0 });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(null);
    // Avoid one GitHub API call per keystroke while entering the URL.
    const timer = window.setTimeout(async () => {
      try {
        const path = slug.split("/").map(encodeURIComponent).join("/");
        const response = await apiFetch(`/api/v1/github/repos/${path}/branches?page=${request.page}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(await apiError(response, "Could not load remote branches"));
        const data = await response.json();
        if (!active) return;
        const items: RemoteBranch[] = data.branches ?? [];
        setBranches((previous) => {
          const combined = request.page === 1 ? items : [...previous, ...items];
          return [...new Map(combined.map((item) => [item.name, item])).values()];
        });
        setNextPage(data.next_page ?? null);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Could not load remote branches.");
      } finally {
        if (active) setLoading(false);
      }
    }, 350);
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [slug, request]);

  return (
    <div className="space-y-1.5 text-left">
      <label htmlFor={id} className="sf-dim flex items-center gap-1.5 text-[12px] font-medium">
        <GitBranch className="h-3.5 w-3.5" /> Remote branch
      </label>
      <select id={id} value={branch} onChange={(event) => onChange(event.target.value)}
        disabled={busy || loading} aria-describedby={`${id}-hint`}
        className="sf-input w-full min-w-0">
        <option value="">Repository default branch</option>
        {branches.map((item) => (
          <option key={item.name} value={item.name}>
            {item.name}{item.protected ? " (protected)" : ""}
          </option>
        ))}
      </select>
      <p id={`${id}-hint`} className="sf-mute text-[11.5px]">
        {branch ? `Index remote branch: ${branch}` : "Uses the repository’s default branch unless you choose another."}
      </p>
      {loading && <p role="status" className="sf-mute flex items-center gap-1.5 text-[11.5px]">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading remote branches…
      </p>}
      {!loading && !error && branches.length === 0 && (
        <p role="status" className="sf-mute text-[11.5px]">No remote branches found. The repository may be empty.</p>
      )}
      {error && <div role="alert" className="text-[11.5px] text-red-300">
        <p>{error}</p>
        <p>You can still try indexing the default branch. For private repositories, connect GitHub with repository access.</p>
        <button type="button" disabled={busy} className="sf-btn sf-btn-ghost mt-1"
          onClick={() => setRequest((current) => ({ ...current, attempt: current.attempt + 1 }))}>
          <RefreshCw className="h-3 w-3" /> Retry branches
        </button>
      </div>}
      {nextPage && !loading && !error && (
        <button type="button" disabled={busy} className="sf-btn sf-btn-ghost"
          onClick={() => setRequest({ page: nextPage, attempt: 0 })}>
          Load more branches
        </button>
      )}
    </div>
  );
}

/** Shared by the Agent landing page, empty panels, and the repository browser. */
export function RepositoryIndexForm({ busy, onIndex }: {
  busy: boolean;
  onIndex: (url: string, branch: string) => Promise<boolean>;
}) {
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("");
  const submitLock = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const locked = busy || submitting;
  const slug = repoSlugFromUrl(url);

  return (
    <form className="w-full space-y-2.5" onSubmit={async (event) => {
      event.preventDefault();
      if (locked || submitLock.current || !url.trim()) return;
      submitLock.current = true;
      setSubmitting(true);
      try {
        // Repositories remains mounted after indexing, unlike the Agent empty
        // state. Reset its draft only when the caller confirms completion.
        if (await onIndex(url.trim(), branch)) {
          setUrl("");
          setBranch("");
        }
      } finally {
        submitLock.current = false;
        setSubmitting(false);
      }
    }}>
      <div className="flex gap-2">
        <input value={url} onChange={(event) => {
          setUrl(event.target.value);
          setBranch("");
        }} placeholder="https://github.com/owner/repo" aria-label="Public repository URL"
          className="sf-input min-w-0 flex-1" disabled={locked} />
        <button type="submit" disabled={locked || !url.trim()} className="sf-btn sf-btn-primary shrink-0">
          {locked ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
          Index
        </button>
      </div>
      {slug ? <RemoteBranches key={slug} slug={slug} busy={locked} branch={branch} onChange={setBranch} /> : (
        <p className="sf-mute text-left text-[11.5px]">Paste a GitHub repository URL to choose a remote branch.</p>
      )}
    </form>
  );
}
