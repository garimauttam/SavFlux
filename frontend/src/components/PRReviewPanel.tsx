/**
 * PRReviewPanel.tsx — PR diff review with inline comments (P1 #8).
 *
 * Paste a unified diff, then either:
 *  - "Analyze impact" → POST /review/impact (instant, deterministic, $0):
 *    risk score, changed symbols, security flags, indexed dependents, and
 *    line-level inline comments.
 *  - "Full AI review" → POST /review/pr-webhook (LLM): everything above
 *    plus the agent's written review.
 *
 * Mounted as the "PR Diff" tab inside ReviewPanel.
 */

import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  GitPullRequest, Loader2, Zap, ShieldAlert, FileCode,
  AlertTriangle, CheckCircle2, FlaskConical,
} from "lucide-react";
import { apiFetch } from "../api";

interface InlineComment {
  path: string;
  line: number;
  severity: "high" | "medium" | "low";
  rule: string;
  message: string;
  code: string;
}

interface Impact {
  changed_files: string[];
  changed_symbols: string[];
  impacted_files: string[];
  security_flags: { rule: string; matches: number }[];
  risk_score: number;
  risk_reasons: string[];
  suggested_tests: string[];
  graph_available: boolean;
}

interface PRResult {
  impact: Impact;
  inline_comments: InlineComment[];
  review?: string;
}

function severityStyle(sev: string): string {
  if (sev === "high") return "border-red-500/30 bg-red-500/10 text-red-300";
  if (sev === "medium") return "border-amber-500/30 bg-amber-500/10 text-amber-300";
  return "border-gray-600/50 bg-gray-800/60 text-gray-400";
}

function riskStyle(score: number): string {
  if (score >= 7) return "border-red-500/40 bg-red-500/10 text-red-300";
  if (score >= 4) return "border-amber-500/40 bg-amber-500/10 text-amber-300";
  return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
}

export function PRReviewPanel() {
  const [repo, setRepo] = useState("");
  const [prNumber, setPrNumber] = useState("");
  const [title, setTitle] = useState("");
  const [diff, setDiff] = useState("");
  const [result, setResult] = useState<PRResult | null>(null);
  const [running, setRunning] = useState<"impact" | "review" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (mode: "impact" | "review") => {
    if (!diff.trim() || running) return;
    setRunning(mode);
    setError(null);
    setResult(null);
    try {
      const endpoint = mode === "impact" ? "/api/v1/review/impact" : "/api/v1/review/pr-webhook";
      const res = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo: repo.trim() || "pasted-diff",
          pr_number: Number(prNumber) || 0,
          title: title.trim() || "Pasted diff review",
          diff,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Server error ${res.status}`);
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "PR review failed");
    } finally {
      setRunning(null);
    }
  };

  const grouped = useMemo(() => {
    const map = new Map<string, InlineComment[]>();
    for (const c of result?.inline_comments ?? []) {
      if (!map.has(c.path)) map.set(c.path, []);
      map.get(c.path)!.push(c);
    }
    return [...map.entries()];
  }, [result]);

  const highs = result?.inline_comments.filter((c) => c.severity === "high").length ?? 0;

  return (
    <div className="flex h-full flex-1 overflow-hidden">
      {/* Input */}
      <div className="flex w-1/2 flex-col border-r border-gray-700 bg-gray-900">
        <div className="grid grid-cols-3 gap-2 border-b border-gray-800 p-3">
          <input
            value={repo} onChange={(e) => setRepo(e.target.value)}
            placeholder="repo (optional)"
            className="rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 font-mono text-xs text-white placeholder-gray-600 focus:border-yellow-500 focus:outline-none"
          />
          <input
            value={prNumber} onChange={(e) => setPrNumber(e.target.value)}
            placeholder="PR # (optional)" inputMode="numeric"
            className="rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 font-mono text-xs text-white placeholder-gray-600 focus:border-yellow-500 focus:outline-none"
          />
          <input
            value={title} onChange={(e) => setTitle(e.target.value)}
            placeholder="title (optional)"
            className="rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 text-xs text-white placeholder-gray-600 focus:border-yellow-500 focus:outline-none"
          />
        </div>
        <textarea
          value={diff} onChange={(e) => setDiff(e.target.value)}
          placeholder={"Paste a unified diff here…\n\ndiff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n@@ -1,2 +1,3 @@\n+API_KEY = \"sk-live-123\""}
          spellCheck={false}
          className="flex-1 resize-none bg-gray-950 p-4 font-mono text-xs leading-relaxed text-gray-200 placeholder-gray-600 focus:outline-none"
        />
        <div className="flex gap-2 border-t border-gray-700 p-3">
          <button
            onClick={() => run("impact")}
            disabled={!diff.trim() || running !== null}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-xl border border-yellow-600/50 bg-yellow-500/10 px-3 py-2 text-sm font-medium text-yellow-300 hover:bg-yellow-500/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running === "impact" ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldAlert className="w-4 h-4" />}
            Analyze impact ($0)
          </button>
          <button
            onClick={() => run("review")}
            disabled={!diff.trim() || running !== null}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-xl bg-yellow-500 px-3 py-2 text-sm font-semibold text-gray-900 hover:bg-yellow-400 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-500"
          >
            {running === "review" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
            Full AI review
          </button>
        </div>
      </div>

      {/* Output */}
      <div className="flex-1 overflow-y-auto bg-gray-950 p-5">
        {!result && !error && !running && (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10">
              <GitPullRequest className="h-7 w-7 text-yellow-400" />
            </div>
            <div>
              <h3 className="font-semibold text-white">PR diff review</h3>
              <p className="mt-1 max-w-sm text-sm text-gray-500">
                Instant deterministic impact + line-level inline comments — or a full
                agent review of the diff.
              </p>
            </div>
          </div>
        )}

        {running && (
          <p className="flex items-center gap-2 text-sm text-gray-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            {running === "impact" ? "Analyzing impact…" : "Agent reviewing diff (may take a minute)…"}
          </p>
        )}

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-red-800 bg-red-900/20 px-4 py-3 text-sm text-red-400">
            <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}

        {result && (
          <div className="space-y-4">
            {/* Risk banner */}
            <div className={`flex items-center gap-3 rounded-xl border px-4 py-3 ${riskStyle(result.impact.risk_score)}`}>
              <span className="font-mono text-2xl font-bold">{result.impact.risk_score}</span>
              <div className="text-xs">
                <p className="font-semibold">/ 10 risk</p>
                {(result.impact.risk_reasons ?? []).map((r, i) => <p key={i} className="opacity-80">· {r}</p>)}
                {(result.impact.risk_reasons ?? []).length === 0 && <p className="opacity-80">No risk signals.</p>}
              </div>
              <div className="ml-auto text-right text-xs opacity-80">
                <p>{result.impact.changed_files.length} files changed</p>
                <p>{result.inline_comments.length} inline comments{highs > 0 && ` (${highs} high)`}</p>
              </div>
            </div>

            {/* Impact grid */}
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-xl border border-gray-800 bg-gray-900 p-3">
                <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-gray-300">
                  <FileCode className="w-3.5 h-3.5 text-blue-400" /> Changed files
                </h4>
                {result.impact.changed_files.length === 0 && <p className="text-xs text-gray-600">None detected.</p>}
                <ul className="space-y-1">
                  {result.impact.changed_files.map((f) => (
                    <li key={f} className="truncate font-mono text-xs text-gray-400" title={f}>{f}</li>
                  ))}
                </ul>
                {result.impact.changed_symbols.length > 0 && (
                  <p className="mt-2 text-[11px] text-gray-500">
                    symbols: <span className="font-mono text-gray-400">{result.impact.changed_symbols.join(", ")}</span>
                  </p>
                )}
              </div>
              <div className="rounded-xl border border-gray-800 bg-gray-900 p-3">
                <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-gray-300">
                  <FlaskConical className="w-3.5 h-3.5 text-purple-400" /> Regression focus
                </h4>
                {(result.impact.suggested_tests ?? []).length === 0 && <p className="text-xs text-gray-600">Nothing flagged.</p>}
                <ul className="space-y-1">
                  {(result.impact.suggested_tests ?? []).slice(0, 6).map((t, i) => (
                    <li key={i} className="text-xs text-gray-400">· {t}</li>
                  ))}
                </ul>
                {!result.impact.graph_available && (
                  <p className="mt-2 text-[11px] text-gray-600">Tip: index the repo to map dependents.</p>
                )}
              </div>
            </div>

            {/* Inline comments */}
            <div>
              <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                Inline comments ({result.inline_comments.length})
              </h4>
              {result.inline_comments.length === 0 && (
                <p className="flex items-center gap-2 rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-300">
                  <CheckCircle2 className="w-4 h-4" /> Clean — no line-level findings.
                </p>
              )}
              <div className="space-y-3">
                {grouped.map(([path, comments]) => (
                  <div key={path} className="overflow-hidden rounded-xl border border-gray-800">
                    <div className="flex items-center gap-2 border-b border-gray-800 bg-gray-900 px-3 py-2">
                      <FileCode className="w-3.5 h-3.5 text-gray-500" />
                      <span className="truncate font-mono text-xs text-gray-300" title={path}>{path}</span>
                      <span className="ml-auto text-[11px] text-gray-600">{comments.length}</span>
                    </div>
                    <div className="divide-y divide-gray-800/70">
                      {comments.map((c, i) => (
                        <div key={i} className="flex gap-3 px-3 py-2.5">
                          <span className="w-12 shrink-0 pt-0.5 text-right font-mono text-[11px] text-gray-500">
                            +{c.line}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase ${severityStyle(c.severity)}`}>
                                {c.severity}
                              </span>
                              <span className="font-mono text-[11px] text-gray-500">{c.rule}</span>
                            </div>
                            <p className="mt-1 text-xs leading-snug text-gray-300">{c.message}</p>
                            <pre className="mt-1.5 overflow-x-auto rounded bg-gray-950 px-2 py-1 font-mono text-[11px] text-gray-400">
                              {c.code}
                            </pre>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Full agent review */}
            {result.review && (
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                  Agent review
                </h4>
                <div className="prose prose-invert prose-sm max-w-none rounded-xl border border-gray-800 bg-gray-900 p-5">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.review}</ReactMarkdown>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default PRReviewPanel;
