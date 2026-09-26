/**
 * timings.ts — what a review measured, and what it is worth showing.
 *
 * The review streams have carried per-stage timings for a while (`plan_ms`,
 * `context_ms`, `llm_ms`, `wait_ms`, `elapsed_ms`, `analysis_ms`, `slowest_file`)
 * and none of it reached the UI, so "the repo review is slow" had no answer. The
 * numbers decide between three different fixes — a slow model, a queue that is too
 * narrow, a pre-pass that is doing more work than the model — and a panel that shows
 * one wall-clock reading cannot tell them apart.
 *
 * Two rules, both deliberate:
 *
 *   * **A stage that was not measured is absent, never zero.** A cached file that
 *     skipped the model has no `llm_ms`; rendering it as `0 ms` would tell a reader
 *     the model answered instantly when it was never asked. (The same truthiness
 *     mistake existed server-side and cost a real measurement.)
 *   * **Nothing is derived that the server did not measure,** except the one
 *     subtraction every profiler does — the part of a run that is not the
 *     deterministic pre-pass is the model — and it is only made when both numbers
 *     exist and the total is not smaller than its own part.
 */

import type { StreamEvent } from "./stream";

/** One file's split: model time, and the time it spent queued for a slot. */
export interface FileTiming {
  file: string;
  llmMs: number | null;
  waitMs: number | null;
  /** True when the answer came from the content-hash cache rather than a model. */
  cached?: boolean;
}

export interface ReviewTimings {
  /** `fast` or `agentic`, from the stream itself. */
  mode: string | null;
  /** The whole single-file review, as the server measured it. */
  totalMs: number | null;
  /** The deterministic parse + rule pass that costs no model call. */
  analysisMs: number | null;
  /** Findings the pre-pass produced, verified and heuristic together. */
  findings: number | null;
  /** Agentic only: time spent investigating before writing. */
  investigateMs: number | null;
  iterations: number | null;
  toolCalls: number | null;
  /** The ReAct loop hit its iteration or time ceiling and was cut off. */
  forced: boolean | null;
  /** Repo review only: the planner's own budget of the run. */
  planMs: number | null;
  /** Cross-file dependency map, built once per run. */
  contextMs: number | null;
  filesMs: number | null;
  /** Model calls this run *made*, not the ones the plan predicted (those are the
   * coverage strip's numbers, and a breakdown that restates them invites a reader
   * to treat the two as the same measurement when they answer different questions:
   * "what did the planner intend" vs "what happened". */
  modelCalls: number | null;
  cacheHits: number | null;
  staticOnly: number | null;
  /** The file that dominated the run, with the phase that made it slow. */
  slowest: FileTiming | null;
  files: FileTiming[];
}

export const EMPTY_TIMINGS: ReviewTimings = {
  mode: null, totalMs: null, analysisMs: null, findings: null, investigateMs: null,
  iterations: null, toolCalls: null, forced: null,
  planMs: null, contextMs: null, filesMs: null,
  modelCalls: null, cacheHits: null, staticOnly: null,
  slowest: null, files: [],
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** How many per-file rows to keep: enough to act on, short enough to scan. */
export const MAX_FILE_ROWS = 5;

/**
 * Fold one status marker into the timings.
 *
 * One function for both surfaces because both streams speak the same vocabulary
 * now, and a reviewer switching between a file review and a repo review should see
 * the same shape of answer. Unknown steps are returned untouched — a newer backend
 * that adds telemetry must not break an older client.
 */
export function applyTiming(timings: ReviewTimings, meta: StreamEvent): ReviewTimings {
  switch (meta.step) {
    case "writing": {
      // Fast mode reports the pre-pass; agentic reports how long it investigated.
      // Same marker step, two different measurements, distinguished by which field
      // is present rather than by guessing from the mode string.
      const analysis = num(meta.analysis_ms);
      const spent = num(meta.elapsed_ms);
      return {
        ...timings,
        mode: str(meta.mode) ?? timings.mode,
        findings: num(meta.findings) ?? timings.findings,
        analysisMs: analysis ?? timings.analysisMs,
        investigateMs: analysis === null && spent !== null ? spent : timings.investigateMs,
      };
    }

    case "complete": {
      const file = str(meta.file);
      if (file) {
        // A per-file completion from a repo run. Keep one row per file (a retried
        // file reports twice) and hold the list worst-first so a cut still shows the
        // expensive end.
        const row: FileTiming = {
          file,
          llmMs: num(meta.llm_ms),
          waitMs: num(meta.wait_ms),
          cached: bool(meta.cached) ?? undefined,
        };
        const files = [...timings.files.filter((entry) => entry.file !== file), row];
        return { ...timings, files: sortFiles(files).slice(0, MAX_FILE_ROWS) };
      }
      return {
        ...timings,
        mode: str(meta.mode) ?? timings.mode,
        totalMs: num(meta.elapsed_ms),
        analysisMs: num(meta.analysis_ms) ?? timings.analysisMs,
        findings: num(meta.findings) ?? timings.findings,
        iterations: num(meta.iterations) ?? timings.iterations,
        toolCalls: num(meta.tool_calls) ?? timings.toolCalls,
        forced: bool(meta.forced) ?? timings.forced,
      };
    }

    case "planned":
      // The `planned` marker carries the plan's own timings. Its counts stay out of
      // here on purpose — the coverage strip already owns those.
      return {
        ...timings,
        planMs: num(meta.plan_ms) ?? timings.planMs,
        contextMs: num(meta.context_ms) ?? timings.contextMs,
        totalMs: num(meta.total_ms) ?? timings.totalMs,
      };

    case "timing": {
      if (meta.stage && meta.stage !== "files") return timings;
      const slowestFile = str(meta.slowest_file);
      return {
        ...timings,
        filesMs: num(meta.elapsed_ms) ?? timings.filesMs,
        modelCalls: num(meta.model_calls) ?? timings.modelCalls,
        cacheHits: num(meta.cache_hits) ?? timings.cacheHits,
        staticOnly: num(meta.static_only) ?? timings.staticOnly,
        slowest: slowestFile
          ? { file: slowestFile, llmMs: num(meta.llm_ms), waitMs: num(meta.wait_ms) }
          : timings.slowest,
      };
    }

    default:
      return timings;
  }
}

function sortFiles(files: FileTiming[]): FileTiming[] {
  return [...files].sort((a, b) => (b.llmMs ?? -1) - (a.llmMs ?? -1));
}

/** One measured part of the run, ready to render. */
export interface Stage {
  key: string;
  label: string;
  ms: number;
  /** What the number covers, in the same words the backend used for it. */
  hint?: string;
}

/**
 * The stages of this run, largest first — or an empty list when nothing was measured.
 *
 * Deliberately absent: a bar for "queued". `wait_ms` is reported per file, so a
 * total would have to be summed over a list the backend already truncated to the
 * slowest few, and a partial sum presented as a whole number is worse than no number.
 */
export function stagesFor(timings: ReviewTimings): Stage[] {
  const stages: Stage[] = [];

  if (timings.planMs !== null) {
    stages.push({
      key: "plan",
      label: "Plan & triage",
      ms: timings.planMs,
    });
  }
  if (timings.contextMs !== null) {
    stages.push({ key: "context", label: "Cross-file context", ms: timings.contextMs });
  }
  if (timings.filesMs !== null) {
    stages.push({
      key: "files",
      label: "Reviewing files",
      ms: timings.filesMs,
      hint: [
        timings.modelCalls !== null ? `${timings.modelCalls} model call(s)` : null,
        timings.cacheHits ? `${timings.cacheHits} cache hit(s)` : null,
        timings.staticOnly ? `${timings.staticOnly} static-only file(s)` : null,
      ].filter(Boolean).join(" · ") || undefined,
    });
  }

  if (timings.totalMs !== null) {
    // A single-file review: the pre-pass, and everything else, which is the model.
    if (timings.analysisMs !== null) {
      stages.push({
        key: "analysis",
        label: "Static analysis",
        ms: timings.analysisMs,
        hint: timings.findings !== null ? `${timings.findings} finding(s)` : undefined,
      });
    }
    const after = timings.analysisMs !== null ? timings.totalMs - timings.analysisMs : timings.totalMs;
    if (after >= 0) {
      stages.push({
        key: "model",
        label: timings.analysisMs !== null ? "Model" : "Review",
        ms: after,
        hint: timings.mode === "agentic"
          ? [
              timings.iterations !== null ? `${timings.iterations} iteration(s)` : null,
              timings.toolCalls !== null ? `${timings.toolCalls} tool call(s)` : null,
              timings.forced ? "cut off by the limit" : null,
            ].filter(Boolean).join(" · ")
          : "1 streamed call",
      });
    }
  } else if (timings.investigateMs !== null) {
    stages.push({
      key: "investigate",
      label: "Tool investigation",
      ms: timings.investigateMs,
      hint: timings.toolCalls !== null ? `${timings.toolCalls} tool call(s)` : undefined,
    });
  }

  return stages.sort((a, b) => b.ms - a.ms);
}

/** Where the run's time went, as one sentence — used for titles and live regions. */
export function timingsSummary(timings: ReviewTimings): string {
  const stages = stagesFor(timings);
  if (!stages.length) return "";
  const top = stages[0];
  return `${top.label} is ${Math.round((top.ms / stages.reduce((sum, s) => sum + s.ms, 0)) * 100)}% of the measured time`;
}
