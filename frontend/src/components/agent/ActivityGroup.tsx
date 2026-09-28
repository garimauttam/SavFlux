/**
 * ActivityGroup.tsx — one agent run, rendered as an activity rather than a log.
 *
 * WHY THIS EXISTS
 * ---------------
 * "Chat" and "Agent" used to be two tabs and two mental models: one asked a
 * question and got prose, the other took a goal and produced a wall of
 * telemetry. A user could not tell from the screen which one they were in, and
 * could not see that asking a question *is* something the agent does — it
 * retrieves, reranks, and only then writes.
 *
 * So both are runs now and both render the same way: a collapsed line that says
 * what the agent did and how long it took, expandable into the real transcript.
 * The reader gets the answer immediately and the evidence on demand, which is
 * the only arrangement where a fast tool is not punished for being fast.
 *
 * It reuses `PlanStrip` and `AgentTimeline` rather than re-rendering the steps,
 * so there is exactly one place that knows what a tool card looks like.
 */

import { useState } from "react";
import { ChevronRight, Clock, Wrench, Zap } from "lucide-react";
import { AgentTimeline, PlanStrip } from "./AgentTimeline";
import { runSummary, type AgentRunState } from "../../lib/agentRun";

interface ActivityGroupProps {
  run: AgentRunState;
  running: boolean;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function ActivityGroup({ run, running }: ActivityGroupProps) {
  const [open, setOpen] = useState(false);
  const summary = runSummary(run);
  const hasSteps = run.planRows.length > 0 || run.items.length > 0;

  return (
    <div className="my-3 overflow-hidden rounded-xl border sf-line sf-surface">
      <button
        type="button"
        onClick={() => hasSteps && setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Show what the agent did"
        className={`flex w-full items-center gap-2.5 px-3 py-2 text-left transition-colors ${
          hasSteps ? "hover:bg-[var(--sf-raised)]" : "cursor-default"
        }`}
      >
        <ChevronRight
          className={`h-3.5 w-3.5 shrink-0 sf-mute transition-transform duration-150 ${
            open ? "rotate-90" : ""
          } ${hasSteps ? "" : "opacity-0"}`}
        />
        <span className={running ? "sf-accent" : "sf-mute"}>
          {running ? <Zap className="h-3.5 w-3.5 animate-pulse" /> : <Wrench className="h-3.5 w-3.5" />}
        </span>
        <span className="sf-dim min-w-0 flex-1 truncate text-[12.5px]">{summary}</span>
        {!running && run.elapsedMs > 0 && (
          <span className="sf-mute inline-flex shrink-0 items-center gap-1 text-[11.5px]">
            <Clock className="h-3 w-3" /> {formatDuration(run.elapsedMs)}
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-3 border-t sf-line px-3 py-2.5">
          {run.planRows.length > 0 && <PlanStrip rows={run.planRows} />}
          {run.items.length > 0 && <AgentTimeline items={run.items} cards={run.cards} />}
          {run.error && (
            <p className="rounded-lg border border-rose-500/25 bg-rose-500/10 px-2.5 py-1.5 text-[11.5px] text-rose-300">
              {run.error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
