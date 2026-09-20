/**
 * CreatePRDialog.tsx — The confirmation step before anything reaches GitHub.
 *
 * WHY A DIALOG AND NOT A BUTTON
 * Everything else in SavFlux is reversible: a review is text, a patch is a file
 * on the user's disk. Opening a pull request is not. It notifies people, starts
 * CI, and lands in a repository's history. So the last screen before it happens
 * shows exactly what would be pushed — the diff, the branch, the target repo —
 * and requires an explicit acknowledgement before the button enables.
 *
 * The digest shown here is the same token the request carries. The backend
 * recomputes it from the diff it receives and refuses to push when they differ,
 * so a preview that went stale while this dialog was open cannot be approved
 * into a different change.
 *
 * THE SECOND GATE, AND WHY IT ASKS FOR A SENTENCE
 * "I read this diff" is not the same as "this change should go out". The risk
 * gate scores the change deterministically — parsed findings, blast radius,
 * sensitive paths, whether a verifier proved the patch applies — and above the
 * threshold it wants an approval bound to this exact diff plus a written reason.
 * The reason is a required field rather than a checkbox because a checkbox is
 * what you click to make a dialog go away; a sentence is what you write when you
 * have actually decided something. Above the block threshold there is no box to
 * tick at all: the dialog offers the patch and the `gh` command, and the user
 * does it themselves.
 */

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  ExternalLink,
  GitPullRequest,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  X,
} from "lucide-react";
import { DiffBlock } from "./DiffBlock";
import type { ChangeRisk, CreatePRResult, PatchPayload, PolicyDecision } from "../hooks/useApplyFix";

/** Longer than the ledger's minimum; a reason below this cannot be submitted. */
const MIN_APPROVAL_REASON = 12;

const LEVEL_STYLE: Record<string, string> = {
  low: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  medium: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  high: "border-orange-500/30 bg-orange-500/10 text-orange-300",
  critical: "border-red-500/30 bg-red-500/10 text-red-300",
};

/**
 * The gate, rendered as evidence rather than as a verdict.
 *
 * Each signal names its own measurement and its own weight, so a user who
 * disagrees with "risk 7/10" can see which number to argue with and where it
 * came from. A score with no breakdown trains people to click through it.
 */
function RiskPanel({ risk, policy, verification }: {
  risk: ChangeRisk;
  policy?: PolicyDecision;
  verification?: { verified: boolean | null; detail: string } | null;
}) {
  const style = LEVEL_STYLE[risk.level] ?? LEVEL_STYLE.medium;
  return (
    <div className={`rounded-xl border px-3 py-2.5 ${style}`}>
      <div className="flex items-center gap-2 text-xs font-medium">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
        <span>
          Risk {risk.score}/10 · {risk.level}
        </span>
        {policy?.approval_threshold !== undefined && (
          <span className="text-[11px] opacity-80">
            (approval at {policy.approval_threshold}, blocked at {policy.block_threshold})
          </span>
        )}
        {verification && (
          <span className="ml-auto text-[11px] font-normal opacity-80">
            {verification.verified === true
              ? "✓ patch verified"
              : verification.verified === false
                ? "✗ patch does not apply"
                : "patch not verified"}
          </span>
        )}
      </div>

      {risk.signals.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {risk.signals.map((signal) => (
            <li key={signal.name} className="flex items-start gap-2 text-[11px] opacity-90">
              <span className="mt-px shrink-0 rounded bg-black/30 px-1 font-mono">+{signal.weight}</span>
              <span>
                <span className="font-medium">{signal.detail}</span>
                <span className="opacity-70"> — {signal.name}</span>
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-[11px] opacity-80">No risk signals fired for this change.</p>
      )}

      {verification?.verified === false && verification.detail && (
        <pre className="mt-2 overflow-x-auto rounded-lg border border-red-500/20 bg-black/40 px-2 py-1.5 text-[10px] font-mono text-red-200/80">
          {verification.detail.slice(0, 400)}
        </pre>
      )}

      {risk.notes?.map((note) => (
        <p key={note} className="mt-1.5 text-[11px] opacity-70">
          Note: {note}
        </p>
      ))}
    </div>
  );
}

interface CreatePRDialogProps {
  open: boolean;
  onClose: () => void;
  patch: PatchPayload;
  /** Repo prefilled from the indexed files, e.g. "owner/name". */
  defaultRepo?: string;
  /** True when the backend has a GITHUB_TOKEN; otherwise we say so up front. */
  isCreating: boolean;
  result: CreatePRResult | null;
  error: string | null;
  onCreate: (payload: {
    repo: string;
    head: string;
    base: string;
    title: string;
    body: string;
    /** Supplied only when the risk gate demanded an approval for this diff. */
    approvalToken?: string;
    approvalReason?: string;
  }) => void;
}

/** "https://github.com/owner/repo" → "owner/repo"; already-slug values pass through. */
export function repoSlugFromUrl(repoUrl: string | undefined | null): string {
  if (!repoUrl) return "";
  const match = /github\.com[/:]([^/]+)\/([^/\s?#]+)/.exec(repoUrl);
  if (!match) return "";
  return `${match[1]}/${match[2].replace(/\.git$/, "")}`;
}

export function CreatePRDialog({
  open,
  onClose,
  patch,
  defaultRepo,
  isCreating,
  result,
  error,
  onCreate,
}: CreatePRDialogProps) {
  const [repo, setRepo] = useState(defaultRepo ?? "");
  const [head, setHead] = useState(patch.suggested_branch ?? "savflux/fixes");
  const [base, setBase] = useState("main");
  const [title, setTitle] = useState(patch.title ?? "SavFlux: apply verified fixes");
  const [body, setBody] = useState(patch.pr_body ?? "");
  const [showBody, setShowBody] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [copied, setCopied] = useState(false);
  //: The written reason the risk gate requires. Empty until the user decides
  //: something; a disabled button with a visible requirement beats a dialog that
  //: rejects the request one round-trip later.
  const [approvalReason, setApprovalReason] = useState("");

  // Re-seed whenever a different patch is presented: a stale branch name or PR
  // body from the previous fix set is worse than no default at all.
  useEffect(() => {
    if (!open) return;
    setRepo(defaultRepo ?? "");
    setHead(patch.suggested_branch ?? "savflux/fixes");
    setTitle(patch.title ?? "SavFlux: apply verified fixes");
    setBody(patch.pr_body ?? "");
    setAcknowledged(false);
    setApprovalReason("");
  }, [open, defaultRepo, patch.suggested_branch, patch.title, patch.pr_body, patch.digest]);

  const filesChanged = useMemo(
    () => patch.files?.map((f) => f.path).join(", ") ?? `${patch.files_changed} file(s)`,
    [patch],
  );
  // The gate, read from the patch the backend scored. Absent means the patch did
  // not come from an endpoint that assesses (a bare /build-patch), in which case
  // the dialog behaves as it always did and the backend still applies the policy.
  const policy = patch.policy;
  // Verification comes from the last push attempt when there was one, and from
  // the build itself otherwise — the same check either way.
  const verification = result?.verification ?? patch.verification;
  const blocked = Boolean(policy?.blocked);
  // Two different refusals land in `blocked`, and conflating them would be a lie:
  // one is "this change is too dangerous to push", the other is "this change does
  // not apply, so pushing it would not do what you asked". The second is not a
  // judgement about risk at all.
  const integrityFailure = verification?.verified === false;
  const needsApproval = Boolean(policy?.requires_approval) && !blocked;
  const reasonIsSufficient = approvalReason.trim().length >= MIN_APPROVAL_REASON;

  const canSubmit =
    Boolean(repo.trim()) &&
    Boolean(head.trim()) &&
    acknowledged &&
    !isCreating &&
    !blocked &&
    (!needsApproval || reasonIsSufficient);

  if (!open) return null;

  const copyCommand = async () => {
    if (!result?.gh_command) return;
    try {
      await navigator.clipboard.writeText(result.gh_command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — the command is on screen to copy by hand */
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 p-4 overflow-y-auto">
      <div className="w-full max-w-3xl rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-800 px-5 py-3">
          <div className="flex items-center gap-2">
            <GitPullRequest className="h-4 w-4 text-violet-400" />
            <h3 className="text-sm font-semibold text-white">
              {result?.status === "created" ? "Pull request opened" : "Review before opening a pull request"}
            </h3>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-white transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Result: created */}
        {result?.status === "created" ? (
          <div className="space-y-4 p-5">
            <div className="flex items-start gap-3 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
              <div className="text-sm text-emerald-200">
                <p className="font-medium">
                  Opened #{result.number} on {result.repo}
                </p>
                {result.url && (
                  <a
                    href={result.url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1 inline-flex items-center gap-1 text-xs text-emerald-300 underline hover:text-emerald-200"
                  >
                    {result.url} <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
            </div>
            <button
              onClick={onClose}
              className="rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-200 hover:bg-gray-700 transition-colors"
            >
              Done
            </button>
          </div>
        ) : result?.status === "manual" ? (
          /* Result: manual plan — no token, or the API refused. Nothing was pushed. */
          <div className="space-y-4 p-5">
            <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
              <div className="text-sm text-amber-200">
                <p className="font-medium">Nothing was pushed.</p>
                <p className="mt-1 text-xs text-amber-200/80">{result.reason}</p>
              </div>
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-gray-400">
                  Run this where the branch exists:
                </span>
                <button
                  onClick={copyCommand}
                  className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-white transition-colors"
                >
                  {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
                  {copied ? "Copied" : "Copy command"}
                </button>
              </div>
              <pre className="overflow-x-auto rounded-lg border border-gray-800 bg-black/50 p-3 text-[11px] font-mono text-gray-300">
                {result.gh_command}
              </pre>
              <p className="mt-1 text-[11px] text-gray-500">
                `head` must already exist on the remote — apply the patch below and push the branch first.
              </p>
            </div>

            <DiffBlock
              diff={patch.diff}
              downloadName={`${(patch.suggested_branch ?? "savflux-fixes").replace(/\//g, "-")}.patch`}
              maxHeightClass="max-h-72"
              copyLabel="Copy patch"
            />

            <button
              onClick={onClose}
              className="rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-200 hover:bg-gray-700 transition-colors"
            >
              Close
            </button>
          </div>
        ) : (
          /* The confirmation form */
          <div className="space-y-4 p-5">
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Repository (owner/name)</span>
                <input
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                  placeholder="octocat/hello-world"
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white placeholder-gray-600 focus:border-violet-500 focus:outline-none"
                />
                {defaultRepo && repo !== defaultRepo && (
                  <button
                    onClick={() => setRepo(defaultRepo)}
                    className="mt-1 text-[11px] text-violet-300 hover:text-violet-200"
                  >
                    Use indexed repo ({defaultRepo})
                  </button>
                )}
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Pull request title</span>
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white focus:border-violet-500 focus:outline-none"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Head branch (must exist on the remote)</span>
                <input
                  value={head}
                  onChange={(e) => setHead(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-xs text-white focus:border-violet-500 focus:outline-none"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs text-gray-400">Base branch</span>
                <input
                  value={base}
                  onChange={(e) => setBase(e.target.value)}
                  className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-xs text-white focus:border-violet-500 focus:outline-none"
                />
              </label>
            </div>

            <div>
              <button
                onClick={() => setShowBody((v) => !v)}
                className="text-xs text-gray-400 hover:text-gray-200 transition-colors"
              >
                {showBody ? "Hide" : "Edit"} PR description ({body.split("\n").length} lines)
              </button>
              {showBody && (
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  rows={8}
                  className="mt-2 w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-[11px] text-gray-200 focus:border-violet-500 focus:outline-none"
                />
              )}
            </div>

            {patch.risk && (
              <RiskPanel risk={patch.risk} policy={policy} verification={verification} />
            )}

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-medium text-gray-400">
                  Exactly what will be pushed — {patch.files_changed} file(s), +{patch.additions}/−
                  {patch.deletions}
                </span>
                <span className="font-mono text-[10px] text-gray-600">digest {patch.digest}</span>
              </div>
              <p className="mb-2 truncate text-[11px] text-gray-500" title={filesChanged}>
                {filesChanged}
              </p>
              <DiffBlock
                diff={patch.diff}
                downloadName={`${(patch.suggested_branch ?? "savflux-fixes").replace(/\//g, "-")}.patch`}
                maxHeightClass="max-h-72"
                copyLabel="Copy patch"
              />
            </div>

            <label className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-950 px-3 py-2 text-xs text-gray-300">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
                className="mt-0.5 h-3.5 w-3.5 accent-violet-500"
              />
              <span>
                I have read this diff and want it pushed to <strong>{repo || "the repository"}</strong> on
                branch <code className="font-mono">{head}</code>.
              </span>
            </label>

            {needsApproval && !blocked && (
              <div className="rounded-xl border border-orange-500/30 bg-orange-500/5 px-3 py-2.5">
                <p className="text-xs font-medium text-orange-200">
                  This change needs an approval — risk {patch.risk?.score}/10
                </p>
                <p className="mt-1 text-[11px] text-orange-200/80">
                  {policy?.reason}
                </p>
                <textarea
                  value={approvalReason}
                  onChange={(e) => setApprovalReason(e.target.value)}
                  rows={2}
                  placeholder="Why should this change go out? (recorded in the policy ledger)"
                  className="mt-2 w-full rounded-lg border border-orange-500/30 bg-gray-900 px-3 py-2 text-xs text-gray-100 placeholder-gray-600 focus:border-orange-400 focus:outline-none"
                />
                <p className="mt-1 text-[11px] text-orange-200/70">
                  {reasonIsSufficient
                    ? "Approval will be bound to this exact diff; editing it afterwards invalidates it."
                    : `At least ${MIN_APPROVAL_REASON} characters, please — this is the record of who approved what.`}
                </p>
              </div>
            )}

            {blocked && (
              <div className="flex items-start gap-2 rounded-xl border border-red-500/30 bg-red-500/10 px-3 py-2.5 text-xs text-red-200">
                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>
                  {integrityFailure ? (
                    <>
                      <span className="font-medium">
                        This patch does not apply — SavFlux will not push it.
                      </span>{" "}
                      Not because it is risky, but because pushing it would not do what it says.
                      Apply the patch by hand and open the PR with the command below.
                    </>
                  ) : (
                    policy?.reason ??
                    "This change is above the block threshold. SavFlux will not push it; use the command in the panel below."
                  )}
                </span>
              </div>
            )}

            {error && (
              <div className="flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                {error}
              </div>
            )}

            <div className="flex items-center justify-end gap-2">
              <button
                onClick={onClose}
                className="rounded-lg px-4 py-2 text-sm text-gray-400 hover:text-white transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() =>
                  onCreate({
                    repo: repo.trim(),
                    head: head.trim(),
                    base: base.trim(),
                    title,
                    body,
                    approvalToken: needsApproval ? policy?.approval_token : undefined,
                    approvalReason: needsApproval ? approvalReason.trim() : undefined,
                  })
                }
                disabled={!canSubmit}
                title={
                  blocked
                    ? integrityFailure
                      ? "This patch does not apply — push it yourself with the command below"
                      : "This change is above the block threshold — push it yourself with the command below"
                    : !acknowledged
                      ? "Confirm you have reviewed the diff first"
                      : needsApproval && !reasonIsSufficient
                        ? "An approval reason is required for this change"
                        : undefined
                }
                className="flex items-center gap-2 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-violet-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-500"
              >
                {isCreating ? <Loader2 className="h-4 w-4 animate-spin" /> : <GitPullRequest className="h-4 w-4" />}
                {isCreating ? "Opening…" : blocked ? "Blocked by policy" : "Open pull request"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default CreatePRDialog;
