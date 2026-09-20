/**
 * AgentPanel.tsx — Deterministic code-task agent UI ($0, no LLM).
 *
 * Sends a goal to POST /agent/run and renders the streamed __STATUS__
 * telemetry as a live step timeline plus the final markdown report.
 * The tool catalogue from GET /agent/tools is shown for transparency.
 */

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot, Loader2, Play, Wrench, CheckCircle2, AlertTriangle, ChevronRight,
  Wand2, GitPullRequest, FileDiff, Search, FileCode, Network, Crosshair,
  ShieldAlert, SkipForward,
} from "lucide-react";
import { apiFetch } from "../api";

interface AgentToolArg {
  name: string;
  type: string;
  required: boolean;
  description: string;
}

interface AgentTool {
  name: string;
  description: string;
  args: string[];
  args_schema?: AgentToolArg[];
  /** True when running this tool changes something outside the process. */
  mutating?: boolean;
}

interface AgentStep {
  step: string;
  message: string;
  tool?: string;
  plan?: string;
  status?: string;
  digest?: string;
  url?: string;
  number?: number;
}

/** Icon + label per tool, so a step reads as "what it did", not "what it is". */
const TOOL_META: Record<string, { label: string; Icon: typeof Bot }> = {
  retrieve_context: { label: "Searched the index", Icon: Search },
  read_file: { label: "Read a file", Icon: FileCode },
  dependency_graph: { label: "Mapped imports", Icon: Network },
  blast_radius: { label: "Traced dependents", Icon: Crosshair },
  autofix: { label: "Applied verified fixes", Icon: Wand2 },
  build_patch: { label: "Built a patch", Icon: FileDiff },
  create_pr: { label: "Pull request", Icon: GitPullRequest },
};

function StepIcon({ step }: { step: AgentStep }) {
  if (step.step === "tool_error") return <AlertTriangle className="w-3.5 h-3.5 shrink-0 text-red-400" />;
  if (step.step === "tool_skipped") return <SkipForward className="w-3.5 h-3.5 shrink-0 text-gray-500" />;
  if (step.step === "complete") return <CheckCircle2 className="w-3.5 h-3.5 shrink-0 text-emerald-400" />;
  if (step.tool && TOOL_META[step.tool]) {
    const { Icon } = TOOL_META[step.tool];
    const tone =
      step.tool === "create_pr"
        ? "text-violet-300"
        : step.tool === "autofix" || step.tool === "build_patch"
          ? "text-emerald-300"
          : "text-pink-400";
    return <Icon className={`w-3.5 h-3.5 shrink-0 ${tone}`} />;
  }
  return <ChevronRight className="w-3.5 h-3.5 shrink-0 text-pink-400" />;
}

export function AgentPanel() {
  const [goal, setGoal] = useState("");
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [report, setReport] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    apiFetch("/api/v1/agent/tools")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setTools(data.tools ?? []);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [steps, report]);

  // The plan is decided server-side from the goal and echoed on the first step,
  // so the UI shows what the run will do instead of guessing from keywords.
  const plan = steps.find((s) => s.step === "starting")?.plan ?? null;

  // A create_pr step that produced a plan rather than a pull request. Its digest
  // is the token that turns the next run into an actual push — offered only once
  // the diff has been rendered below.
  const pendingPR = steps.find(
    (s) => s.tool === "create_pr" && s.step === "tool_done" && s.status === "manual" && s.digest,
  );

  /**
   * Run the agent.
   *
   * `confirmDigest` is only ever set by the "Open pull request" button below,
   * after the user has seen the diff in the report. The backend compares it with
   * the digest of the patch this run builds, so the confirmation cannot be
   * replayed against a different change.
   */
  const run = async (confirmDigest?: string) => {
    if (!goal.trim() || running) return;
    setRunning(true);
    setError(null);
    setSteps([]);
    setReport("");
    try {
      const res = await apiFetch("/api/v1/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: goal.trim(),
          max_steps: 8,
          confirm_digest: confirmDigest ?? null,
        }),
      });
      if (!res.ok || !res.body) throw new Error(`Server error ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Extract complete __STATUS__ markers; rest is report text
        let idx: number;
        while ((idx = buffer.indexOf("__STATUS__")) !== -1) {
          const end = buffer.indexOf("__STATUS_END__", idx);
          if (end === -1) break; // partial marker — wait for more
          const before = buffer.slice(0, idx);
          if (before) setReport((r) => r + before);
          try {
            const payload = JSON.parse(buffer.slice(idx + 10, end));
            setSteps((s) => [...s, payload]);
          } catch {}
          buffer = buffer.slice(end + 14);
        }
        // Flush plain text up to the last safe boundary (keep partial markers)
        const partial = buffer.search(/__STATUS__?$/);
        if (partial !== -1) {
          setReport((r) => r + buffer.slice(0, partial));
          buffer = buffer.slice(partial);
        } else if (!buffer.includes("__STATUS")) {
          setReport((r) => r + buffer);
          buffer = "";
        }
      }
      if (buffer && !buffer.includes("__STATUS")) setReport((r) => r + buffer);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Agent run failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex h-full overflow-hidden bg-gray-950">
      {/* Tool catalogue */}
      <div className="hidden w-64 shrink-0 flex-col gap-2 overflow-y-auto border-r border-gray-800 p-4 md:flex">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          <Wrench className="w-3.5 h-3.5" /> Agent tools
        </div>
        {tools.map((t) => (
          <div key={t.name} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="font-mono text-xs text-purple-300">{t.name}</p>
              {t.mutating && (
                <span
                  className="flex items-center gap-1 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-medium text-violet-300"
                  title="This tool changes something outside SavFlux and is never run without confirmation"
                >
                  <ShieldAlert className="w-2.5 h-2.5" />
                  confirmed
                </span>
              )}
            </div>
            <p className="mt-1 text-[11px] leading-snug text-gray-500">{t.description}</p>
            {(t.args_schema ?? []).length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {(t.args_schema ?? []).map((arg) => (
                  <li key={arg.name} className="font-mono text-[10px] text-gray-500" title={arg.description}>
                    <span className={arg.required ? "text-gray-300" : "text-gray-600"}>{arg.name}</span>
                    <span className="text-gray-700">: {arg.type}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        {tools.length === 0 && (
          <p className="text-xs text-gray-600">Tool list unavailable.</p>
        )}
      </div>

      {/* Run panel */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="border-b border-gray-800 p-4">
          <div className="flex items-center gap-2">
            <Bot className="w-5 h-5 text-pink-400" />
            <h2 className="text-sm font-semibold text-white">Deterministic Agent</h2>
            <span className="text-[11px] text-gray-500">$0 · no LLM · reproducible</span>
          </div>
          <p className="mt-1 text-[11px] text-gray-500">
            Asking for a fix adds <span className="font-mono text-emerald-300">autofix</span> and{" "}
            <span className="font-mono text-emerald-300">build_patch</span> to the plan. Asking for a
            pull request adds <span className="font-mono text-violet-300">create_pr</span>, which
            prepares the PR and never pushes without a confirmed diff.
          </p>
          <div className="mt-3 flex gap-2">
            <input
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") run(); }}
              placeholder="Goal, e.g. fix the TLS bug in net.py and open a PR…"
              className="flex-1 rounded-xl border border-gray-700 bg-gray-800 px-4 py-2.5 text-sm text-white placeholder-gray-500 focus:border-pink-500 focus:outline-none"
            />
            <button
              onClick={() => run()}
              disabled={running || !goal.trim()}
              className="flex items-center gap-1.5 rounded-xl bg-pink-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-pink-500 disabled:bg-gray-700 disabled:cursor-not-allowed"
            >
              {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Run
            </button>
          </div>
          {plan && (
            <p className="mt-2 flex flex-wrap items-center gap-1 text-[11px] text-gray-500">
              <span className="text-gray-600">Plan:</span>
              {plan.split(" → ").map((name, i) => (
                <span key={name}>
                  {i > 0 && <span className="mx-1 text-gray-700">→</span>}
                  <span
                    className={
                      name === "create_pr"
                        ? "font-mono text-violet-300"
                        : name === "autofix" || name === "build_patch"
                          ? "font-mono text-emerald-300"
                          : "font-mono text-gray-400"
                    }
                  >
                    {name}
                  </span>
                </span>
              ))}
            </p>
          )}
          {error && (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertTriangle className="w-3.5 h-3.5" /> {error}
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {steps.length === 0 && !report && !running && (
            <p className="text-sm text-gray-600">
              Describe a code-investigation goal. The agent searches the index, reads the top
              files, maps dependents, and writes a findings report — all deterministic.
            </p>
          )}

          {/* Step timeline */}
          {steps.length > 0 && (
            <ol className="mb-4 space-y-1">
              {steps.map((s, i) => (
                <li key={i} className="flex items-start gap-2 text-xs text-gray-400">
                  <StepIcon step={s} />
                  <span className="min-w-0">
                    {s.message}
                    {/* A created PR is the one step whose result is worth a link. */}
                    {s.url && (
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="ml-2 text-violet-300 underline hover:text-violet-200"
                      >
                        open #{s.number}
                      </a>
                    )}
                  </span>
                </li>
              ))}
            </ol>
          )}

          {/* Confirmation for a prepared-but-unopened pull request */}
          {!running && pendingPR && report.includes("```diff") && (
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-violet-500/30 bg-violet-500/10 px-4 py-3">
              <div className="text-xs text-violet-200">
                <p className="font-medium">A pull request is ready and has not been opened.</p>
                <p className="mt-0.5 text-violet-200/70">
                  Read the diff above, then confirm — the run repeats with digest{" "}
                  <span className="font-mono">{pendingPR.digest}</span>, which only matches this exact patch.
                </p>
              </div>
              <button
                onClick={() => run(pendingPR.digest)}
                className="flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-violet-500"
              >
                <GitPullRequest className="h-3.5 w-3.5" />
                I have reviewed the diff — open the PR
              </button>
            </div>
          )}

          {/* Report */}
          {report && (
            <div className="prose prose-invert prose-sm max-w-none rounded-xl border border-gray-800 bg-gray-900 p-5">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}

export default AgentPanel;
