/**
 * useApplyFix.ts — Verified fixes → patch → pull request, as one state machine.
 *
 * Three backend calls, in the only order that makes sense:
 *
 *   1. POST /review/autofix-set   — deterministic repairs over the selected files.
 *      Every fix is re-parsed and re-analysed server-side before it is returned,
 *      so everything in `fixes` below has already been proved. No LLM, ~1ms/file.
 *   2. (same response)            — the endpoint also returns one patch: a unified
 *      diff, a digest, a suggested branch and a ready PR body.
 *   3. POST /review/create-pr     — with the digest from step 2. The backend
 *      refuses to push without a digest that matches the diff, so a stale preview
 *      cannot become a pull request.
 *
 * The hook deliberately never auto-runs step 3. Creating a PR is the one thing
 * here that cannot be undone, so it stays behind an explicit confirmation.
 */

import { useCallback, useState } from "react";
import { apiFetch } from "../api";

export interface AppliedFix {
  rule_id: string;
  line: number;
  description: string;
  before: string;
  after: string;
}

/** One file's outcome from /review/autofix-set. */
export interface FileFixResult {
  path: string;
  fixed: boolean;
  fixes: AppliedFix[];
  skipped: string[];
  findings_before: number;
  findings_after: number;
  score_before: number;
  score_after: number;
  patch: PatchPayload | null;
}

export interface PatchFileSummary {
  path: string;
  status: "added" | "modified" | "deleted";
  additions: number;
  deletions: number;
}

export interface PatchPayload {
  diff: string;
  digest: string;
  files_changed: number;
  additions: number;
  deletions: number;
  files: PatchFileSummary[];
  /** Present on patches built by the set/single endpoints, not on a bare build. */
  title?: string;
  suggested_branch?: string;
  pr_body?: string;
  /** The gate's assessment of this change, computed when the patch was built. */
  risk?: ChangeRisk;
  /** What the policy would do with it — the token below is bound to this diff. */
  policy?: PolicyDecision;
  /**
   * Whether the backend proved this patch applies. Present at *build* time, not
   * only after a push attempt: the dialog can then promise a patch that applies
   * instead of finding out at the gate.
   */
  verification?: PatchVerification;
}

/**
 * One measurement behind a risk score, with the evidence that produced it.
 *
 * The gate is only defensible if a user can see *why* a change scored what it
 * scored — "risk 7/10" on its own is an oracle, and people route around oracles.
 */
export interface RiskSignal {
  name: string;
  weight: number;
  detail: string;
  evidence?: Record<string, unknown>;
}

/** A deterministic assessment of how risky a change is. Higher score = riskier. */
export interface ChangeRisk {
  policy_version?: number;
  score: number;
  level: "low" | "medium" | "high" | "critical";
  signals: RiskSignal[];
  scope?: { files?: string[]; file_count?: number; additions?: number; deletions?: number;
            graph_available?: boolean };
  digest?: string;
  notes?: string[];
  summary?: string;
}

/** What the policy decided, and the token an approval would need to carry. */
export interface PolicyDecision {
  action?: string;
  score?: number;
  level?: string;
  /** "allowed" | "approval_required" | "blocked" */
  allowed?: boolean;
  requires_approval?: boolean;
  approved?: boolean;
  blocked?: boolean;
  approval_token?: string;
  approval_threshold?: number;
  block_threshold?: number;
  reason?: string;
  signals?: RiskSignal[];
}

/** Whether a verifier proved the patch applies (`git apply` in a scratch repo). */
export interface PatchVerification {
  verified: boolean | null;
  detail: string;
  files?: number;
}

/** Response from /review/create-pr — either a created PR or a manual plan. */
export interface CreatePRResult {
  status: "created" | "manual";
  repo: string;
  head?: string;
  base?: string;
  number?: number;
  url?: string;
  reason?: string;
  gh_command?: string;
  patch?: string | null;
  digest?: string;
  risk?: ChangeRisk;
  policy?: PolicyDecision;
  verification?: PatchVerification;
}

/** A file to attempt repairs on: repo-relative `path` plus its index id. */
export interface FixTarget {
  path: string;
  source?: string;
  fileName?: string;
}

export interface FixSetOptions {
  title?: string;
  summary?: string;
  /** Repo the files came from, so index lookups can be exact. */
  repoUrl?: string;
}

export interface FixSetState {
  results: FileFixResult[];
  errors: { path: string; reason: string }[];
  patch: PatchPayload | null;
  scanned: number;
  fixedCount: number;
}

const EMPTY: FixSetState = { results: [], errors: [], patch: null, scanned: 0, fixedCount: 0 };

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (Array.isArray(detail)) {
      return detail.map((item: { msg?: string }) => item.msg ?? "Validation error").join("; ");
    }
    if (typeof detail === "string" && detail) return detail;
  } catch {
    // Non-JSON error body — fall through to the generic message.
  }
  return fallback;
}

export function useApplyFix() {
  const [state, setState] = useState<FixSetState>(EMPTY);
  const [isFixing, setIsFixing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [prResult, setPrResult] = useState<CreatePRResult | null>(null);
  const [isCreatingPR, setIsCreatingPR] = useState(false);

  /** Step 1+2: repair the set and build the patch. */
  const applyFixes = useCallback(async (targets: FixTarget[], options: FixSetOptions = {}) => {
    if (targets.length === 0) return;
    setIsFixing(true);
    setError(null);
    setPrResult(null);
    try {
      const response = await apiFetch("/api/v1/review/autofix-set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: targets.map((t) => ({
            path: t.path,
            // The exact index id lets the backend find the file with one
            // metadata query instead of scanning the collection.
            source: t.source,
          })),
          title: options.title ?? "",
          summary: options.summary ?? "",
          repo_url: options.repoUrl ?? null,
        }),
      });
      if (!response.ok) throw new Error(await readError(response, `Server error ${response.status}`));

      const data = await response.json();
      setState({
        results: data.files ?? [],
        errors: data.errors ?? [],
        patch: data.patch ?? null,
        scanned: data.scanned ?? 0,
        fixedCount: data.fixed_count ?? 0,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not apply fixes.");
      setState(EMPTY);
    } finally {
      setIsFixing(false);
    }
  }, []);

  /**
   * Step 3: open the PR.
   *
   * `patch.digest` travels with the request as proof that this exact diff was
   * on screen. The backend compares it against the diff it received and answers
   * with a plan instead of a push when they disagree.
   */
  const createPR = useCallback(
    async (payload: {
      repo: string; head: string; base: string; title: string; body: string;
      /**
       * Required when the risk gate flags the change. Both come from the patch
       * response: the token is bound to that exact diff and that exact
       * assessment, so a diff that moves invalidates the approval rather than
       * silently inheriting it.
       */
      approvalToken?: string;
      approvalReason?: string;
    }) => {
      if (!state.patch) return null;
      setIsCreatingPR(true);
      setError(null);
      try {
        const response = await apiFetch("/api/v1/review/create-pr", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...payload,
            diff: state.patch.diff,
            confirm_digest: state.patch.digest,
            approval_token: payload.approvalToken ?? "",
            approval_reason: payload.approvalReason ?? "",
          }),
        });
        if (!response.ok) throw new Error(await readError(response, `Server error ${response.status}`));
        const data: CreatePRResult = await response.json();
        setPrResult(data);
        return data;
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not create the pull request.");
        return null;
      } finally {
        setIsCreatingPR(false);
      }
    },
    [state.patch],
  );

  const reset = useCallback(() => {
    setState(EMPTY);
    setError(null);
    setPrResult(null);
  }, []);

  return { ...state, isFixing, error, applyFixes, createPR, prResult, isCreatingPR, reset };
}
