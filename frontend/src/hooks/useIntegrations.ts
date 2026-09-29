/**
 * hooks/useIntegrations.ts — GitHub connection and local-model state.
 *
 * Both live here rather than in their panels for one reason: the top bar, the
 * command palette and the panels themselves all need to read the same state,
 * and a panel that fetched its own copy would show "connect GitHub" next to an
 * account that is already connected. One fetch, one owner, many readers.
 *
 * Both hooks fail *visibly*. The previous app's sidebar swallowed every error
 * from `/ingest/repos` and rendered an empty repo list, which looks identical
 * to "you have no repositories" — the single most misleading state a product
 * can be in.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";
import type { GitHubRepo, GitHubStatus, ModelStatus } from "../types/workspace";

/** Pull the server's `detail` out of a FastAPI error, whatever shape it has. */
export async function apiError(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return body.detail[0].msg;
  } catch {
    /* not JSON — the status line is all we have */
  }
  return `${fallback} (HTTP ${res.status})`;
}

export interface GitHubState {
  status: GitHubStatus | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  connect: (token: string) => Promise<boolean>;
  disconnect: () => Promise<void>;
}

export function useGitHub(): GitHubState {
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A status request that resolves after the user has already connected
  // something would put the app back to "not connected" — the classic
  // last-response-wins bug, and here it would be visible as the header
  // un-connecting itself.
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++seq.current;
    try {
      const res = await apiFetch("/api/v1/github/status");
      if (!res.ok) throw new Error(await apiError(res, "Could not reach the SavFlux API"));
      const data: GitHubStatus = await res.json();
      if (id !== seq.current) return;
      setStatus(data);
      setError(null);
    } catch (e) {
      if (id !== seq.current) return;
      setError(e instanceof Error ? e.message : "Could not check the GitHub connection.");
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const connect = useCallback(
    async (token: string) => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiFetch("/api/v1/github/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        if (!res.ok) {
          setError(await apiError(res, "GitHub rejected that token."));
          return false;
        }
        await refresh();
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not reach the SavFlux API.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const disconnect = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/github/connect", { method: "DELETE" });
      if (!res.ok) {
        setError(await apiError(res, "Could not disconnect."));
        return;
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  return { status, loading, busy, error, refresh, connect, disconnect };
}

export interface ModelsState {
  models: ModelStatus | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  select: (chat: string) => Promise<boolean>;
  selectMixture: (models: string[]) => Promise<boolean>;
  selectProvider: (provider: ModelStatus["provider"], apiKey: string, model: string) => Promise<boolean>;
  forgetProviderKey: (provider: Exclude<ModelStatus["provider"], "ollama">) => Promise<boolean>;
}

export function useModels(): ModelsState {
  const [models, setModels] = useState<ModelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++seq.current;
    try {
      const res = await apiFetch("/api/v1/models");
      if (!res.ok) throw new Error(await apiError(res, "Could not read the model list"));
      const data: ModelStatus = await res.json();
      if (id !== seq.current) return;
      setModels(data);
      setError(null);
    } catch (e) {
      if (id !== seq.current) return;
      setError(e instanceof Error ? e.message : "Could not read the model list.");
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const changed = () => { void refresh(); };
    window.addEventListener("savflux:models-changed", changed);
    return () => window.removeEventListener("savflux:models-changed", changed);
  }, [refresh]);

  const select = useCallback(
    async (chat: string) => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiFetch("/api/v1/models/select", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat }),
        });
        if (!res.ok) {
          setError(await apiError(res, "Could not switch model"));
          return false;
        }
        setModels((await res.json()) as ModelStatus);
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not reach the SavFlux API.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const selectMixture = useCallback(
    async (summaryMixtureModels: string[]) => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiFetch("/api/v1/models/select", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ summary_mixture_models: summaryMixtureModels }),
        });
        if (!res.ok) {
          setError(await apiError(res, "Could not update the MoA model set"));
          return false;
        }
        setModels((await res.json()) as ModelStatus);
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not reach the SavFlux API.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const selectProvider = useCallback(
    async (provider: ModelStatus["provider"], apiKey: string, model: string) => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiFetch("/api/v1/models/provider", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, api_key: apiKey || undefined, model }),
        });
        if (!res.ok) {
          setError(await apiError(res, "Could not configure this model provider"));
          return false;
        }
        setModels((await res.json()) as ModelStatus);
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not reach the SavFlux API.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const forgetProviderKey = useCallback(
    async (provider: Exclude<ModelStatus["provider"], "ollama">) => {
      setBusy(true);
      setError(null);
      try {
        const res = await apiFetch(`/api/v1/models/provider/key/${provider}`, { method: "DELETE" });
        if (!res.ok) {
          setError(await apiError(res, "Could not remove the saved provider key"));
          return false;
        }
        await refresh();
        return true;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not reach the SavFlux API.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  return { models, loading, busy, error, refresh, select, selectMixture, selectProvider, forgetProviderKey };
}

/**
 * Can the connected account push to this repository?
 *
 * The answer comes from GitHub, not from the product's guess at which scope was
 * granted: `permissions.push` is computed server-side from the token's
 * fine-grained grants, the collaborator's role and branch protection. A UI
 * dropdown cannot be that accurate, and being wrong about it costs the user a
 * round trip to a 403 on someone else's repository.
 *
 * Three states, and the third is the important one:
 *
 *   `true`  — GitHub says this account can push. Offer Create PR.
 *   `false` — GitHub says it cannot. Do not offer it; say why instead.
 *   `null`  — we do not know yet, or cannot know: not connected, offline, or the
 *             repository is not on GitHub. Offer the control anyway, because the
 *             `gh` CLI path still works with no token at all, and a button that
 *             disappears while a request is in flight is worse than one that is
 *             briefly optimistic.
 *
 * So `false` is the only value that removes functionality, and it is only
 * reachable from a real answer.
 */
export interface RepoWriteAccess {
  canWrite: boolean | null;
  loading: boolean;
}

export function useRepoWriteAccess(slug: string | null | undefined): RepoWriteAccess {
  const [canWrite, setCanWrite] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!slug) {
      setCanWrite(null);
      return;
    }
    // Same last-response-wins guard as `useGitHub`: switching repositories
    // mid-flight must not leave the previous repository's answer on screen,
    // which would offer a push the new repository cannot accept.
    let cancelled = false;
    setLoading(true);
    setCanWrite(null);
    (async () => {
      const [owner, name] = slug.split("/");
      if (!owner || !name) {
        setCanWrite(null);
        setLoading(false);
        return;
      }
      try {
        // Each segment encoded on its own. `encodeURIComponent` over the whole
        // slug turns "pallets/click" into "pallets%2Fclick", which collapses
        // two path segments into one and 404s a route declared as
        // `/repos/{owner}/{name}` — a silent failure that would have left this
        // hook permanently "unknown" and the gate permanently inert.
        const res = await apiFetch(
          `/api/v1/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`,
        );
        if (cancelled) return;
        // 409 is the documented "not connected" answer, not a failure: the
        // repository may still be perfectly readable with no token at all.
        if (!res.ok) {
          setCanWrite(null);
          return;
        }
        const data = (await res.json()) as GitHubRepo;
        // No `permissions` object means GitHub did not tell us — an
        // unauthenticated read of a public repository looks exactly like this.
        // `data.permissions?.push === true` would collapse that to `false` and
        // delete the Create PR button for every public-repository user, which
        // is the precise inversion this hook exists to avoid.
        setCanWrite(data.permissions ? data.permissions.push === true : null);
      } catch {
        if (!cancelled) setCanWrite(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [slug]);

  return { canWrite, loading };
}
