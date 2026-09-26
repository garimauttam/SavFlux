/**
 * AgentPanel.tsx — the deterministic code-task agent ($0, no LLM).
 *
 * One run of this agent sends a goal, plans from it, calls local tools, and writes
 * a report. What this component does is render that as an *operation* rather than
 * a log: a plan strip with per-step state, a card per tool call showing its
 * arguments, what it found and what it cost, the plan reasoning inline, a live
 * elapsed clock, and a Stop button that ends the run on both sides of the wire.
 *
 * The pieces live apart from here on purpose:
 *
 *   * `lib/stream.ts` — one marker scanner/decoder for every stream in the app;
 *   * `lib/agentRun.ts` — events → run state (pure, replayable, tested directly);
 *   * `components/agent/*` — the transcript and the header.
 *
 * What stays here is the part that cannot be moved: the fetch, its AbortController,
 * and the decision about scrolling.
 *
 * SCROLLING
 * ---------
 * A run writes into the panel continuously. Following it with `scrollIntoView` on
 * every event — which is what this file used to do — pins the reader to the bottom
 * and makes it impossible to read a step that scrolled past. So the transcript
 * follows the run only while the reader is already at the bottom, and a "Jump to
 * latest" control appears the moment they scroll away. jsdom has no layout engine,
 * so both numbers are 0 in tests; the code must therefore treat "everything is
 * zero" as "pinned" rather than as a measurement.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowDownToLine, Play, ShieldAlert, Square, Wrench } from "lucide-react";
import { apiFetch } from "../api";
import { DiffBlock } from "./DiffBlock";
import { AgentRunHeader } from "./agent/AgentRunHeader";
import { AgentTimeline, PlanStrip } from "./agent/AgentTimeline";
import {
  appendReport, applyEvent, applyStreamError, initialRunState, markCancelled,
  type AgentRunState,
} from "../lib/agentRun";
import {
  AGENT_TAGS, decodeStatus, drainMarkers, flushTail,
} from "../lib/stream";

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

/** Goals that work against a freshly indexed repo, offered instead of a blank box. */
const EXAMPLE_GOALS = [
  "fix the unsafe TLS verification in the network layer",
  "map how the review budget decides which files a model sees",
  "find hardcoded secrets and open a PR with the patch",
];

/**
 * The report renderer. A diff is not a language: highlighting it as Python colours
 * the removed lines as if they were still live code, so fenced diffs go to
 * `DiffBlock` (add/remove/hunk by line prefix, with copy + download) and every
 * other block stays a plain monospace box that reads the same in both themes.
 */
const REPORT_COMPONENTS: React.ComponentProps<typeof ReactMarkdown>["components"] = {
  h1: ({ children }) => <h1 className="mb-2 mt-1 border-b border-gray-700 pb-1 text-base font-bold text-white">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-1.5 mt-4 text-sm font-bold text-white">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-3 text-xs font-semibold text-gray-200">{children}</h3>,
  p: ({ children }) => <p className="my-1.5 text-sm leading-relaxed text-gray-100">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5 text-sm text-gray-100">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5 text-sm text-gray-100">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  code({ className, children }: any) {
    const text = String(children ?? "").replace(/\n$/, "");
    const language = /language-(\w+)/.exec(className || "")?.[1] ?? "";
    if (language === "diff" || text.startsWith("diff --git")) {
      return <DiffBlock diff={text} downloadName="agent.patch" copyLabel="Copy patch" maxHeightClass="max-h-[46vh]" />;
    }
    if (language) {
      return (
        <div className="my-3 overflow-hidden rounded-lg border border-gray-700 bg-gray-950">
          <div className="border-b border-gray-700 px-3 py-1 font-mono text-[10px] text-gray-500">{language}</div>
          <pre className="overflow-auto p-3 text-[11px] leading-relaxed text-gray-200">
            <code>{text}</code>
          </pre>
        </div>
      );
    }
    return <code className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[11px] text-purple-300">{text}</code>;
  },
  pre: ({ children }) => <>{children}</>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 rounded-r border-l-2 border-amber-600/60 bg-amber-600/10 py-1 pl-3 pr-2 text-[12px] text-amber-100">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-gray-700 px-2 py-1 text-left font-semibold text-gray-300">{children}</th>
  ),
  td: ({ children }) => <td className="border-b border-gray-800 px-2 py-1 align-top text-gray-200">{children}</td>,
};

export function AgentPanel() {
  const [goal, setGoal] = useState("");
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [run, setRun] = useState<AgentRunState>(initialRunState);
  const [running, setRunning] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const [pinned, setPinned] = useState(true);

  useEffect(() => {
    apiFetch("/api/v1/agent/tools")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setTools(data.tools ?? []);
      })
      .catch(() => {});
  }, []);

  // A panel that unmounts mid-run must not leave a request running for a reader
  // who has navigated to another tab.
  useEffect(() => () => abortRef.current?.abort(), []);

  const follow = useCallback(() => {
    const el = scrollerRef.current;
    if (!el || !pinnedRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, []);

  useEffect(() => { follow(); }, [run, follow]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    // No layout in jsdom (clientHeight is 0 for every element), so an all-zero
    // measurement is "we cannot tell", and the safe default is to keep following.
    const measurable = el.scrollHeight > el.clientHeight && el.clientHeight > 0;
    const atBottom = !measurable || el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    if (atBottom !== pinnedRef.current) {
      pinnedRef.current = atBottom;
      setPinned(atBottom);
    }
  };

  /**
   * Run the agent.
   *
   * `confirmDigest` is only ever set by the confirm button below, after the diff has
   * been rendered. The backend compares it with the digest of the patch that *this*
   * run builds, so a confirmation cannot be replayed against a different change.
   */
  const run_ = useCallback(async (confirmDigest?: string) => {
    if (!goal.trim() || running) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    // Fresh state with the goal echoed in: the transcript must not show the
    // previous run's cards while the new one is only just starting.
    setRun({ ...initialRunState(), status: "running", goal: goal.trim(), maxSteps: 8 });
    pinnedRef.current = true;
    setPinned(true);

    let buffer = "";
    try {
      const response = await apiFetch("/api/v1/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          goal: goal.trim(),
          max_steps: 8,
          confirm_digest: confirmDigest ?? null,
        }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) {
        const detail = await response.text().catch(() => "");
        throw new Error(readableStatus(response.status, detail));
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const scan = drainMarkers(buffer, AGENT_TAGS);
        buffer = scan.rest;
        for (const segment of scan.segments) {
          if (segment.kind === "text") {
            setRun((state) => appendReport(state, segment.value));
          } else if (segment.kind === "status") {
            setRun((state) => applyEvent(state, decodeStatus(segment.payload)));
          } else if (segment.kind === "error") {
            setRun((state) => applyStreamError(state, segment.payload.trim()));
          }
        }
      }
      const tail = flushTail(buffer, AGENT_TAGS);
      if (tail) setRun((state) => appendReport(state, tail));
      setRun((state) => (state.status === "running" ? markCancelled(state) : state));
    } catch (error) {
      if (controller.signal.aborted) {
        // The reader stopped it. That is a state, not an error.
        setRun((state) => markCancelled(state));
      } else {
        setRun((state) => applyStreamError(state, error instanceof Error ? error.message : String(error)));
      }
    } finally {
      abortRef.current = null;
      setRunning(false);
    }
  }, [goal, running]);

  const stop = useCallback(() => {
    // Abort first: the reader's `read()` rejection is what unblocks the loop, and
    // cancelling the state here means the panel is honest even if the promise
    // settles after the component is gone.
    abortRef.current?.abort();
    setRun((state) => markCancelled(state));
  }, []);

  const confirm = useMemo(() => run.pendingConfirm, [run.pendingConfirm]);
  const hasDiff = run.report.includes("diff --git") || run.report.includes("```diff");

  return (
    <div className="flex h-full overflow-hidden bg-gray-950">
      {/* Tool catalogue */}
      <div className="hidden w-64 shrink-0 flex-col gap-2 overflow-y-auto border-r border-gray-800 p-4 md:flex">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          <Wrench className="w-3.5 h-3.5" /> Agent tools
        </div>
        {tools.map((tool) => (
          <div key={tool.name} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
            <div className="flex items-center justify-between gap-2">
              <p className="font-mono text-xs text-purple-300">{tool.name}</p>
              {tool.mutating && (
                <span
                  className="flex items-center gap-1 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-medium text-violet-300"
                  title="This tool changes something outside SavFlux and is never run without confirmation"
                >
                  <ShieldAlert className="w-2.5 h-2.5" />
                  confirmed
                </span>
              )}
            </div>
            <p className="mt-1 text-[11px] leading-snug text-gray-500">{tool.description}</p>
            {(tool.args_schema ?? []).length > 0 && (
              <ul className="mt-2 space-y-0.5">
                {(tool.args_schema ?? []).map((arg) => (
                  <li key={arg.name} className="font-mono text-[10px] text-gray-500" title={arg.description}>
                    <span className={arg.required ? "text-gray-300" : "text-gray-600"}>{arg.name}</span>
                    <span className="text-gray-700">: {arg.type}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        {tools.length === 0 && <p className="text-xs text-gray-600">Tool list unavailable.</p>}
      </div>

      {/* Run panel — transcript above, composer below, like every agent surface
          a user already knows. The old layout put the form on top, which made the
          run read like a settings page and left the reader scrolling away from the
          control they were about to press. */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <AgentRunHeader state={run} running={running} />

        <div ref={scrollerRef} onScroll={onScroll} className="relative flex-1 overflow-y-auto p-4">
          {run.error && (
            <p className="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              {run.error}
            </p>
          )}

          {run.status === "idle" && (
            <div className="flex h-full flex-col justify-center gap-3 pb-24 text-sm text-gray-500">
              <p className="max-w-lg">
                Give the agent a goal. It searches the index, reads the top files, maps their dependents and
                writes a findings report — deterministically, with no model call, so the same goal against the
                same index always produces the same bytes.
              </p>
              <ul className="max-w-lg space-y-1 text-[12px] text-gray-600">
                <li>· A goal that says <span className="text-gray-400">fix</span>,{" "}
                  <span className="text-gray-400">patch</span> or{" "}
                  <span className="text-gray-400">harden</span> may edit files: it adds a verified autofix
                  pass and one reviewable patch.</li>
                <li>· A goal that says <span className="text-gray-400">open a PR</span> prepares a pull
                  request and pushes nothing until you confirm that exact diff.</li>
              </ul>
            </div>
          )}

          {(run.planRows.length > 0 || run.items.length > 0) && (
            <div className="space-y-3">
              <PlanStrip rows={run.planRows} />
              <AgentTimeline items={run.items} cards={run.cards} />
            </div>
          )}

          {confirm && !running && hasDiff && (
            <div className="mb-4 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-violet-500/30 bg-violet-500/10 px-4 py-3">
              <div className="text-xs text-violet-200">
                <p className="font-medium">A pull request is ready and has not been opened.</p>
                <p className="mt-0.5 text-violet-200/70">
                  Read the patch below, then confirm — the run repeats with digest{" "}
                  <span className="font-mono">{confirm.digest}</span>, which only matches this exact patch.
                </p>
              </div>
              <button
                type="button"
                onClick={() => void run_(confirm.digest)}
                className="rounded-lg bg-violet-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-violet-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-500/60"
              >
                I have reviewed the diff — open the PR
              </button>
            </div>
          )}

          {run.report.trim() && (
            <div className="prose prose-invert prose-sm mt-4 max-w-none rounded-xl border border-gray-800 bg-gray-900 p-5">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={REPORT_COMPONENTS}>
                {run.report}
              </ReactMarkdown>
            </div>
          )}

          {!pinned && (
            <button
              type="button"
              onClick={() => {
                pinnedRef.current = true;
                setPinned(true);
                follow();
              }}
              className="sticky bottom-2 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] text-gray-300 shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/60"
            >
              <ArrowDownToLine className="h-3 w-3" /> Jump to latest
            </button>
          )}
        </div>

        <div className="border-t border-gray-800 bg-gray-950 px-4 py-3">
          <div className="flex items-end gap-2">
            <textarea
              rows={1}
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void run_();
                }
              }}
              placeholder="Goal — e.g. fix the TLS bug in net.py and open a PR  ·  Enter to run, Shift+Enter for a new line"
              aria-label="Agent goal"
              disabled={running}
              className="max-h-32 min-h-[42px] flex-1 resize-none rounded-xl border border-gray-700 bg-gray-800 px-4 py-2.5 text-sm text-white placeholder-gray-500 focus:border-pink-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/40 disabled:opacity-60"
            />
            {running ? (
              <button
                type="button"
                onClick={stop}
                className="flex items-center gap-1.5 rounded-xl bg-red-500 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-red-500/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/60"
              >
                <Square className="h-3.5 w-3.5 fill-current" />
                Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void run_()}
                disabled={!goal.trim()}
                className="flex items-center gap-1.5 rounded-xl bg-pink-600 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-pink-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/60 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-500"
              >
                <Play className="h-4 w-4" />
                Run
              </button>
            )}
          </div>
          {!running && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] uppercase tracking-wide text-gray-600">Try</span>
              {EXAMPLE_GOALS.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => setGoal(example)}
                  className="max-w-full truncate rounded-full border border-gray-800 bg-gray-900 px-2.5 py-1 text-[10.5px] text-gray-400 transition-colors hover:border-gray-700 hover:text-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/60"
                >
                  {example}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** A 401 from a missing key and a 429 from the limiter want different sentences. */
function readableStatus(status: number, detail: string): string {
  const trimmed = detail.trim();
  if (status === 401 || status === 403) {
    return "The API key was rejected. Set VITE_API_KEY (and the server's API_KEY) before running the agent.";
  }
  if (status === 429) return "Rate limited — the agent allows 10 runs a minute.";
  return trimmed ? `${status}: ${trimmed.slice(0, 200)}` : `Server error ${status}`;
}

export default AgentPanel;
