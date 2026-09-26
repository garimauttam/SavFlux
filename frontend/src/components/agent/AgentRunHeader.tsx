/**
 * AgentRunHeader.tsx — the run's controls and its live numbers.
 *
 * Lives at the top of the panel, above the transcript, because the run's identity
 * and its numbers are what a reader glances at while scrolling — whereas Stop
 * belongs under the cursor, next to the composer that started the run. That split
 * is the only difference in layout from the panel this replaced, and it is the
 * reason `AgentComposer` sits at the bottom of `AgentPanel`.
 *
 * Two jobs:
 *
 *   * State. "running · Step 3 of 7" vs "stopped after 4 of 7" — the difference
 *     between a run in flight and a run that ended early, which the transcript
 *     alone cannot tell you if the last step never sent a marker.
 *   * The budget and the clock. "4/8 steps · 684 ms · $0" is what makes an agent
 *     legible: how far it got, what it cost, and — via `truncated` — whether the
 *     silence at the end is "finished" or "out of budget". Those are different
 *     sentences and a reader must not have to guess which one they are being told.
 *
 * The elapsed number is a client measurement while a run is in flight and the
 * server's own total once the closing marker lands, which is why the running value
 * carries a "+" prefix. Showing a local clock as if it were measured would be a
 * small lie with a big audience.
 */

import { useEffect, useState } from "react";
import { Bot } from "lucide-react";
import type { AgentRunState } from "../../lib/agentRun";
import { runSummary } from "../../lib/agentRun";
import { formatDuration } from "../../lib/stream";

/** Ticks only while a run is live, so a finished panel costs no timers. */
export function useLiveElapsed(running: boolean): number {
  const [ms, setMs] = useState(0);

  useEffect(() => {
    if (!running) {
      setMs(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(() => setMs(Date.now() - started), 200);
    return () => window.clearInterval(id);
  }, [running]);

  return ms;
}

const STATUS_TONE: Record<AgentRunState["status"], string> = {
  idle: "text-gray-500",
  running: "text-pink-300",
  finished: "text-emerald-300",
  cancelled: "text-amber-300",
  failed: "text-red-300",
};

const STATUS_WORD: Record<AgentRunState["status"], string> = {
  idle: "ready",
  running: "running",
  finished: "complete",
  cancelled: "stopped",
  failed: "failed",
};

export interface AgentRunHeaderProps {
  state: AgentRunState;
  running: boolean;

}

export function AgentRunHeader({ state, running }: AgentRunHeaderProps) {
  const live = useLiveElapsed(running);
  const ms = running ? live : state.elapsedMs;
  const total = state.planRows.length;
  const finished = state.counts.done + state.counts.failed + state.counts.skipped;
  const progress = total > 0 ? Math.min(100, Math.round((finished / total) * 100)) : running ? 8 : 0;

  return (
    <div className="border-b border-gray-800 px-4 pb-3 pt-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex items-center gap-2">
          <Bot className="h-5 w-5 text-pink-400" />
          <h2 className="text-sm font-semibold text-white">Code Agent</h2>
        </div>
        <span className="rounded-full bg-gray-800 px-2 py-0.5 font-mono text-[10px] text-gray-400">
          $0 · deterministic · {state.mode || "no LLM"}
        </span>
        {(state.status !== "idle") && (
          <span className={`flex items-center gap-1.5 text-[11px] font-medium ${STATUS_TONE[state.status]}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${running ? "animate-pulse bg-pink-500" : "bg-current"}`} />
            {STATUS_WORD[state.status]}
            {runSummary(state) && <span className="text-gray-500">· {runSummary(state)}</span>}
          </span>
        )}

        <div className="ml-auto flex items-center gap-3 font-mono text-[10.5px] text-gray-500">
          <span title="Steps used of the budget you gave the run">
            {state.stepsUsed}/{state.maxSteps || "–"} steps
          </span>
          {ms > 0 && (
            <span title={running ? "Wall time since you pressed Run" : "Time the run reported for itself"}>
              {running ? "+" : ""}{formatDuration(ms)}
            </span>
          )}
        </div>
      </div>

      {state.truncated && !running && (
        <p className="mt-2 rounded-lg border border-amber-600/40 bg-amber-600/10 px-3 py-1.5 text-[11px] text-amber-200">
          The step budget ran out before the plan did — {finished} of {total} steps ran. Raise
          <span className="mx-1 font-mono text-amber-100">max_steps</span>
          to see the rest.
        </p>
      )}

      {total > 0 && (
        <div
          className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-gray-800"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
          aria-label="Agent run progress"
        >
          <div
            className={`h-full rounded-full transition-all duration-500 ${state.counts.failed ? "bg-red-500" : "bg-pink-500"}`}
            style={{ width: `${progress}%` }}
          />
        </div>
      )}
    </div>
  );
}

export default AgentRunHeader;
