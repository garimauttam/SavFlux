/**
 * ApplyFixPanel.tsx — "Prove it, fix it, ship it" on one screen.
 *
 * The review agent's job used to stop at describing a problem. This panel is the
 * last mile: it asks the backend for the deterministic repairs it can prove,
 * shows each before/after pair, and hands back a patch — with an optional path to
 * a pull request.
 *
 * WHY THE FIXES AND THE DIFF BOTH APPEAR
 * A patch alone is not reviewable by anyone who does not read diffs for a living,
 * and a list of "fixed 3 issues" is not evidence. The per-rule before/after makes
 * the change legible; the diff underneath is what actually gets applied. The
 * panel is careful to say when it fixed nothing — an empty result reported as
 * success is how autofix tools lose trust.
 *
 * Everything here is $0 and offline: `ast`-based repairs, stdlib diffs, no model
 * call. The whole panel is a few milliseconds of CPU per file, so it can be
 * offered on every review rather than rationed.
 */

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FileCode2,
  GitPullRequest,
  Loader2,
  ShieldCheck,
  SkipForward,
  Wand2,
} from "lucide-react";
import { IndexedFile } from "../types";
import { DiffBlock } from "./DiffBlock";
import { CreatePRDialog, repoSlugFromUrl } from "./CreatePRDialog";
import { useApplyFix, FixTarget } from "../hooks/useApplyFix";

interface ApplyFixPanelProps {
  /** Files that were just reviewed. Python files only reach the backend. */
  files: IndexedFile[];
  /** Refreshed after a PR is opened, so the parent can update its view. */
  onPROpened?: () => void;
}

/**
 * Repo-relative path for a source id.
 *
 * The patch names files, and a diff header of
 * `a/https://github.com/o/r::src/auth.py` is not a patch anyone can apply. Source
 * ids from a cloned repo carry the relative path after `::`; local ingests store
 * an absolute path, so the longest shared prefix of the set is stripped instead.
 */
export function relativeRepoPath(source: string, commonPrefix = ""): string {
  if (!source) return "";
  const separator = source.indexOf("::");
  if (separator !== -1) return source.slice(separator + 2);
  if (commonPrefix && source.startsWith(commonPrefix)) {
    return source.slice(commonPrefix.length).replace(/^\/+/, "");
  }
  const lastSlash = source.lastIndexOf("/");
  return lastSlash >= 0 ? source.slice(lastSlash + 1) : source;
}

function commonPrefixOf(sources: string[]): string {
  if (sources.length === 0) return "";
  let prefix = sources[0];
  for (const source of sources.slice(1)) {
    while (!source.startsWith(prefix)) {
      prefix = prefix.slice(0, prefix.lastIndexOf("/"));
      if (!prefix) return "";
    }
  }
  const lastSlash = prefix.lastIndexOf("/");
  return lastSlash >= 0 ? prefix.slice(0, lastSlash + 1) : "";
}

export function ApplyFixPanel({ files, onPROpened }: ApplyFixPanelProps) {
  const fix = useApplyFix();
  const [dialogOpen, setDialogOpen] = useState(false);

  const pythonFiles = useMemo(
    () => files.filter((f) => (f.language || "").toLowerCase() === "py" || f.source.endsWith(".py")),
    [files],
  );

  const targets: FixTarget[] = useMemo(() => {
    const prefix = commonPrefixOf(pythonFiles.map((f) => f.source));
    return pythonFiles.map((f) => ({
      source: f.source,
      fileName: f.file_name,
      path: relativeRepoPath(f.source, prefix),
    }));
  }, [pythonFiles]);

  const repoSlug = useMemo(() => repoSlugFromUrl(pythonFiles[0]?.repo_url), [pythonFiles]);

  if (pythonFiles.length === 0) {
    return (
      <div className="rounded-xl border border-gray-800 bg-gray-900/60 px-4 py-3 text-xs text-gray-500">
        Automatic fixes are implemented for Python. This selection has none — the review above
        still applies.
      </div>
    );
  }

  const hasRun = fix.scanned > 0 || fix.results.length > 0 || Boolean(fix.error);
  const label = pythonFiles.length === 1 ? "Find safe fixes" : `Find safe fixes (${pythonFiles.length} files)`;

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-900 overflow-hidden">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-800 px-4 py-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-white">
            <Wand2 className="h-4 w-4 text-emerald-400" />
            Verified autofix
          </h3>
          <p className="mt-0.5 text-[11px] text-gray-500">
            Deterministic repairs — no LLM. Every fix is re-parsed and re-analysed, and is discarded
            if it fails to clear the finding or introduces a worse one.
          </p>
        </div>
        <button
          onClick={() => fix.applyFixes(targets, { repoUrl: pythonFiles[0]?.repo_url })}
          disabled={fix.isFixing}
          className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-500"
        >
          {fix.isFixing ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wand2 className="h-3.5 w-3.5" />}
          {fix.isFixing ? "Analysing…" : hasRun ? "Run again" : label}
        </button>
      </div>

      {/* Error */}
      {fix.error && (
        <div className="flex items-start gap-2 border-b border-gray-800 bg-red-500/10 px-4 py-2 text-xs text-red-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {fix.error}
        </div>
      )}

      {/* Results */}
      {hasRun && (
        <div className="space-y-4 p-4">
          {/* Honest empty state */}
          {fix.fixedCount === 0 && fix.results.length > 0 && (
            <div className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-950 px-3 py-2 text-xs text-gray-400">
              <SkipForward className="mt-0.5 h-3.5 w-3.5 shrink-0 text-gray-500" />
              <span>
                No automatic fixes were applicable. Findings that need judgement — how to
                parameterise a query, where a secret should live — are deliberately left to you.
              </span>
            </div>
          )}

          {/* Files the backend could not fix at all (non-Python, not indexed) */}
          {fix.errors.map((entry) => (
            <div
              key={entry.path}
              className="flex items-start gap-2 rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-xs text-amber-200/90"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>
                <code className="font-mono">{entry.path}</code> — {entry.reason}
              </span>
            </div>
          ))}

          {/* Per-file verified fixes */}
          {fix.results.map((result) => (
            <div key={result.path} className="rounded-lg border border-gray-800 bg-gray-950">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-800 px-3 py-2">
                <span className="flex items-center gap-2 font-mono text-xs text-gray-300">
                  <FileCode2 className="h-3.5 w-3.5 text-gray-500" />
                  {result.path}
                </span>
                {result.fixed ? (
                  <span className="flex items-center gap-2 text-[11px]">
                    <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 font-medium text-emerald-300">
                      {result.fixes.length} fix{result.fixes.length === 1 ? "" : "es"}
                    </span>
                    <span className="text-gray-500">
                      score {result.score_before} → <span className="text-emerald-300">{result.score_after}</span>
                    </span>
                    <span className="text-gray-600">
                      findings {result.findings_before} → {result.findings_after}
                    </span>
                  </span>
                ) : (
                  <span className="text-[11px] text-gray-500">nothing safe to fix</span>
                )}
              </div>

              {result.fixes.length > 0 && (
                <ul className="divide-y divide-gray-800/70">
                  {result.fixes.map((applied, index) => (
                    <li key={`${applied.rule_id}-${index}`} className="px-3 py-2">
                      <div className="flex items-center gap-2 text-[11px]">
                        <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                        <span className="text-gray-300">{applied.description}</span>
                        <code className="font-mono text-[10px] text-gray-500">{applied.rule_id}</code>
                        <span className="font-mono text-[10px] text-gray-600">L{applied.line}</span>
                      </div>
                      <div className="mt-1 grid gap-1 font-mono text-[11px] sm:grid-cols-2">
                        <div className="overflow-x-auto rounded bg-red-500/5 px-2 py-1 text-red-300/90">
                          <span className="select-none text-red-500/70">− </span>
                          {applied.before}
                        </div>
                        <div className="overflow-x-auto rounded bg-emerald-500/5 px-2 py-1 text-emerald-300/90">
                          <span className="select-none text-emerald-500/70">+ </span>
                          {applied.after}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}

              {result.skipped.length > 0 && (
                <div className="border-t border-gray-800 px-3 py-2">
                  <p className="mb-1 flex items-center gap-1.5 text-[11px] text-gray-500">
                    <SkipForward className="h-3 w-3" />
                    Skipped — failed verification, fix these by hand:
                  </p>
                  <ul className="space-y-0.5">
                    {result.skipped.map((reason, index) => (
                      <li key={index} className="font-mono text-[11px] text-gray-500">
                        {reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ))}

          {/* The patch */}
          {fix.patch && (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="flex items-center gap-2 text-xs text-gray-300">
                  <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
                  {fix.patch.files_changed} file(s), +{fix.patch.additions}/−{fix.patch.deletions} ·{" "}
                  <span className="font-mono text-[10px] text-gray-500">
                    branch {fix.patch.suggested_branch}
                  </span>
                </p>
                <button
                  onClick={() => setDialogOpen(true)}
                  className="flex items-center gap-1.5 rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-1.5 text-xs font-medium text-violet-200 transition-colors hover:bg-violet-500/20"
                >
                  <GitPullRequest className="h-3.5 w-3.5" />
                  Create PR…
                </button>
              </div>
              <DiffBlock
                diff={fix.patch.diff}
                copyLabel="Copy patch"
                downloadName={`${(fix.patch.suggested_branch ?? "savflux-fixes").replace(/\//g, "-")}.patch`}
              />
              <p className="text-[11px] text-gray-500">
                Nothing has been pushed. Apply locally with{" "}
                <code className="font-mono text-gray-400">git apply savflux.patch</code>, or review the
                diff and open a pull request.
              </p>
            </div>
          )}
        </div>
      )}

      {fix.patch && (
        <CreatePRDialog
          open={dialogOpen}
          onClose={() => {
            setDialogOpen(false);
            if (fix.prResult?.status === "created") onPROpened?.();
          }}
          patch={fix.patch}
          defaultRepo={repoSlug}
          isCreating={fix.isCreatingPR}
          result={fix.prResult}
          error={fix.error}
          onCreate={(payload) => fix.createPR(payload)}
        />
      )}
    </div>
  );
}

export default ApplyFixPanel;
