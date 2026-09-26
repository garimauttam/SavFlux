/**
 * agentRun.ts — the state an agent stream describes.
 *
 * `AgentPanel` used to render one `<li>` per `__STATUS__` line, which is why the
 * agent surface read like a build log: three identical "read_file: ✓" rows, with
 * no way to see what each one opened, what it found, or what it cost. The events
 * the backend now emits carry all three (`step_id`, `args`, `elapsed_ms`,
 * `preview`), and this module is where they become a *run*: a plan with per-step
 * state, a chronological transcript, one card per tool call.
 *
 * Why a pure reducer rather than state sprinkled through the component:
 *
 *   * pairing a start marker with its finish, by id, is the one piece of logic
 *     that must be exactly right — and it is close to untestable inside a render;
 *   * a cancelled run, a failed run and a finished run differ only in which
 *     markers never arrived, so "what does the last row look like" is decided here
 *     once instead of in three JSX branches;
 *   * `reduceAll(events)` lets a test — and later a replay from the history tab —
 *     rebuild the same state from a recorded stream, with no network and no clock.
 *
 * No client timestamps, on purpose: the only duration shown is the one the server
 * measured around the real work, and a pure reducer stays reproducible. The
 * ticking stopwatch in the header is separate and labelled as the client's view.
 */

import type { StreamEvent } from "./stream";

export type CardState = "running" | "done" | "failed" | "skipped" | "interrupted";
export type PlanState = "queued" | "running" | "done" | "failed" | "skipped";

export interface ToolCard {
  stepId: string;
  planId: string;
  tool: string;
  state: CardState;
  message: string;
  args: Record<string, string>;
  preview: string[];
  previewMore: number;
  /** Server-reported duration; null until the finish marker arrives. */
  elapsedMs: number | null;
  /** Scalar result fields the tool reported, for the card's key/value grid. */
  fields: Record<string, string | number | boolean | null>;
}

export interface PlanRow {
  id: string;
  tool: string;
  order: number;
  state: PlanState;
  /** Invocations this step spawned — `read_file` runs once per file. */
  cards: number;
  /** The ones that actually ran, for "2 of 3". */
  runs: number;
}

export type TranscriptItem =
  | { kind: "reasoning"; id: string; text: string }
  | { kind: "card"; id: string; stepId: string }
  | { kind: "notice"; id: string; tone: "info" | "warn" | "error"; text: string };

export interface AgentRunState {
  status: "idle" | "running" | "finished" | "cancelled" | "failed";
  runId: string;
  mode: string;
  goal: string;
  plan: string[];
  planRows: PlanRow[];
  cards: Record<string, ToolCard>;
  items: TranscriptItem[];
  maxSteps: number;
  stepsUsed: number;
  truncated: boolean;
  counts: { done: number; failed: number; skipped: number };
  /** From the closing marker — the server's total, not a client estimate. */
  elapsedMs: number;
  report: string;
  error: string | null;
  /** A pull request that was prepared but not opened, with its confirmation token. */
  pendingConfirm: { digest: string; branch: string; title: string } | null;
  prUrl: string | null;
  prNumber: number | null;
  /** Steps that started and never finished — what Stop interrupts. */
  openStepIds: string[];
}

/** Keys that are structure rather than result; they never appear in a card grid. */
const RESERVED = new Set([
  "step", "message", "tool", "step_id", "plan_id", "ok", "elapsed_ms",
  "args", "preview", "preview_more", "run_id", "mode", "goal", "plan",
  "plan_steps", "max_steps", "steps_used", "truncated", "done", "failed",
  "skipped", "kind", "interrupted", "fields_dropped",
]);

export function initialRunState(): AgentRunState {
  return {
    status: "idle",
    runId: "",
    mode: "",
    goal: "",
    plan: [],
    planRows: [],
    cards: {},
    items: [],
    maxSteps: 0,
    stepsUsed: 0,
    truncated: false,
    counts: { done: 0, failed: 0, skipped: 0 },
    elapsedMs: 0,
    report: "",
    error: null,
    pendingConfirm: null,
    prUrl: null,
    prNumber: null,
    openStepIds: [],
  };
}

let sequence = 0;
function nextId(prefix: string): string {
  sequence += 1;
  return `${prefix}-${sequence}`;
}

/** Back to 1 — tests only, so a recorded stream reproduces ids exactly. */
export function resetIds(): void {
  sequence = 0;
}

/**
 * Longest result string kept for display. `MAX_FIELD_CHARS` in the protocol module
 * caps this server-side; the client repeats the cap because a card is rendered from
 * whatever arrives, and a 40 KB `patch` string in the grid is a frozen panel.
 */
const MAX_FIELD_TEXT = 240;

function scalarFields(event: StreamEvent): Record<string, string | number | boolean | null> {
  const fields: Record<string, string | number | boolean | null> = {};
  for (const [key, value] of Object.entries(event)) {
    if (RESERVED.has(key)) continue;
    if (value === null || typeof value === "number" || typeof value === "boolean") {
      fields[key] = value;
    } else if (typeof value === "string") {
      fields[key] = value.length > MAX_FIELD_TEXT ? `${value.slice(0, MAX_FIELD_TEXT - 1)}\u2026` : value;
    }
  }
  return fields;
}

function derivePlanState(row: PlanRow, cards: Record<string, ToolCard>): PlanState {
  const own = Object.values(cards).filter((card) => card.planId === row.id);
  if (own.some((card) => card.state === "running")) return "running";
  if (own.some((card) => card.state === "failed" || card.state === "interrupted")) return "failed";
  if (own.length === 0) return row.state;
  if (own.every((card) => card.state === "skipped")) return "skipped";
  return "done";
}

function withDerivedPlan(state: AgentRunState): AgentRunState {
  return {
    ...state,
    planRows: state.planRows.map((row) => {
      const own = Object.values(state.cards).filter((card) => card.planId === row.id);
      return {
        ...row,
        cards: own.length,
        runs: own.filter((card) => card.state !== "skipped").length,
        state: derivePlanState(row, state.cards),
      };
    }),
  };
}

function notice(state: AgentRunState, tone: "info" | "warn" | "error", text: string): AgentRunState {
  if (!text) return state;
  return { ...state, items: [...state.items, { kind: "notice", id: nextId("n"), tone, text }] };
}

/**
 * Fold one `__STATUS__` payload into the run.
 *
 * An unknown step becomes a notice rather than being dropped. A newer backend can
 * add a marker an older bundle has never seen, and "the agent did something this
 * UI cannot show" has to be visible — that is the difference between a version
 * skew and a lie by omission.
 */
export function applyEvent(state: AgentRunState, event: StreamEvent): AgentRunState {
  const step = String(event.step ?? "");
  const message = typeof event.message === "string" ? event.message : "";

  switch (step) {
    case "starting": {
      const planSteps = Array.isArray(event.plan_steps) ? (event.plan_steps as StreamEvent[]) : [];
      const rows: PlanRow[] = planSteps.length
        ? planSteps.map((row, index) => ({
            id: String(row.id ?? `plan:${row.tool}`),
            tool: String(row.tool ?? ""),
            order: typeof row.order === "number" ? row.order : index,
            state: "queued" as PlanState,
            cards: 0,
            runs: 0,
          }))
        : String(event.plan ?? "")
            .split(" → ")
            .filter(Boolean)
            .map((tool, index) => ({
              id: `plan:${tool}`,
              tool,
              order: index,
              state: "queued" as PlanState,
              cards: 0,
              runs: 0,
            }));

      return {
        ...initialRunState(),
        status: "running",
        runId: String(event.run_id ?? ""),
        mode: String(event.mode ?? "deterministic"),
        goal: String(event.goal ?? state.goal),
        plan: rows.map((row) => row.tool),
        planRows: rows,
        maxSteps: typeof event.max_steps === "number" ? event.max_steps : 0,
        report: state.report,
      };
    }

    case "reasoning":
      return { ...state, items: [...state.items, { kind: "reasoning", id: nextId("r"), text: message }] };

    case "tool": {
      const stepId = String(event.step_id ?? nextId("step"));
      const card: ToolCard = {
        stepId,
        planId: String(event.plan_id ?? `plan:${event.tool ?? ""}`),
        tool: String(event.tool ?? ""),
        state: "running",
        message,
        args: (event.args as Record<string, string>) ?? {},
        preview: [],
        previewMore: 0,
        elapsedMs: null,
        fields: {},
      };
      return withDerivedPlan({
        ...state,
        cards: { ...state.cards, [stepId]: card },
        items: [...state.items, { kind: "card", id: nextId("c"), stepId }],
        stepsUsed: state.stepsUsed + 1,
        openStepIds: [...state.openStepIds, stepId],
      });
    }

    case "tool_done":
    case "tool_error": {
      const stepId = event.step_id ? String(event.step_id) : "";
      const previous = stepId ? state.cards[stepId] : undefined;
      if (!previous) {
        // A finish with no start — a replay, or a backend that emits one marker
        // per tool. Show it as a finished card instead of losing the result.
        if (!stepId) return notice(state, "info", message);
        const synthesised = applyEvent(state, { ...event, step: "tool", step_id: stepId });
        return applyEvent(synthesised, event);
      }
      const failed = step === "tool_error" || event.ok === false;
      const card: ToolCard = {
        ...previous,
        state: failed ? "failed" : "done",
        message: message || previous.message,
        elapsedMs: typeof event.elapsed_ms === "number" ? event.elapsed_ms : previous.elapsedMs,
        preview: Array.isArray(event.preview) ? (event.preview as string[]) : previous.preview,
        previewMore: typeof event.preview_more === "number" ? event.preview_more : previous.previewMore,
        fields: { ...previous.fields, ...scalarFields(event) },
      };
      const next: AgentRunState = {
        ...state,
        cards: { ...state.cards, [stepId]: card },
        openStepIds: state.openStepIds.filter((id) => id !== stepId),
        counts: {
          ...state.counts,
          done: state.counts.done + (failed ? 0 : 1),
          failed: state.counts.failed + (failed ? 1 : 0),
        },
      };
      return withConfirmLink(withDerivedPlan(next), card);
    }

    case "tool_skipped": {
      const tool = String(event.tool ?? "");
      const planId = String(event.plan_id ?? `plan:${tool}`);
      const stepId = `${planId}#skipped`;
      const card: ToolCard = {
        stepId,
        planId,
        tool,
        state: "skipped",
        message,
        args: {},
        preview: [],
        previewMore: 0,
        elapsedMs: null,
        fields: scalarFields(event),
      };
      return withDerivedPlan({
        ...state,
        cards: { ...state.cards, [stepId]: card },
        items: [...state.items, { kind: "card", id: nextId("c"), stepId }],
        counts: { ...state.counts, skipped: state.counts.skipped + 1 },
      });
    }

    case "cancelled":
    case "complete":
    case "failed": {
      const interrupted = Array.isArray(event.interrupted) ? (event.interrupted as StreamEvent[]) : [];
      const cards = { ...state.cards };
      for (const entry of interrupted) {
        const id = String(entry.step_id ?? "");
        if (id && cards[id]) cards[id] = { ...cards[id], state: "interrupted" };
      }
      const closed: AgentRunState = {
        ...state,
        cards,
        status: step === "complete" ? "finished" : step === "cancelled" ? "cancelled" : "failed",
        elapsedMs: typeof event.elapsed_ms === "number" ? event.elapsed_ms : state.elapsedMs,
        stepsUsed: typeof event.steps_used === "number" ? event.steps_used : state.stepsUsed,
        truncated: Boolean(event.truncated),
        counts: {
          done: numberOr(event.done, state.counts.done),
          failed: numberOr(event.failed, state.counts.failed),
          skipped: numberOr(event.skipped, state.counts.skipped),
        },
        openStepIds: [],
      };
      return withDerivedPlan(
        notice(
          closed,
          step === "complete" ? "info" : step === "cancelled" ? "warn" : "error",
          message,
        ),
      );
    }

    case "writing":
      return notice(state, "info", message);

    default:
      return notice(state, "info", message);
  }
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}

/**
 * The PR card is the one result with a button attached to it, so it is lifted out
 * of the generic grid: either a link to a pull request that exists, or the digest
 * that turns the next run into an actual push.
 */
function withConfirmLink(state: AgentRunState, card: ToolCard): AgentRunState {
  if (card.tool !== "create_pr") return state;
  const url = typeof card.fields.url === "string" ? card.fields.url : null;
  const number = typeof card.fields.number === "number" ? card.fields.number : null;
  const digest = typeof card.fields.digest === "string" ? card.fields.digest : null;
  return {
    ...state,
    prUrl: url ?? state.prUrl,
    prNumber: number ?? state.prNumber,
    pendingConfirm:
      url && number
        ? null
        : card.fields.status === "manual" && digest
          ? { digest, branch: String(card.fields.head ?? ""), title: String(card.fields.title ?? "") }
          : state.pendingConfirm,
  };
}

/** Fold a whole recorded stream — the shape a history replay will need. */
export function reduceAll(events: StreamEvent[]): AgentRunState {
  return events.reduce<AgentRunState>(applyEvent, initialRunState());
}

/** `__ERROR__…__ERROR_END__` is plain text, not an event. */
export function applyStreamError(state: AgentRunState, message: string): AgentRunState {
  return { ...interruptOpen(state), status: "failed", error: message || "The stream ended with an error." };
}

/**
 * The reader pressed Stop (or closed the tab): the stream is gone, so no closing
 * marker will arrive. Anything still running is interrupted by definition, and the
 * run reads as stopped rather than spinning forever.
 */
export function markCancelled(state: AgentRunState): AgentRunState {
  if (state.status !== "running") return state;
  return withDerivedPlan(notice({ ...interruptOpen(state), status: "cancelled" }, "warn",
    "Stopped — the run was interrupted."));
}

function interruptOpen(state: AgentRunState): AgentRunState {
  const cards = { ...state.cards };
  let touched = false;
  for (const id of state.openStepIds) {
    if (cards[id] && cards[id].state === "running") {
      cards[id] = { ...cards[id], state: "interrupted" };
      touched = true;
    }
  }
  if (!touched) return { ...state, openStepIds: [] };
  // Nothing is open any more: a step that was interrupted is not one a later
  // consumer should still treat as in flight.
  return { ...state, cards, openStepIds: [] };
}

export function appendReport(state: AgentRunState, text: string): AgentRunState {
  if (!text) return state;
  return { ...state, report: state.report + text };
}

/** What the header's progress chip says. Derived, never remembered. */
export function runSummary(state: AgentRunState): string {
  const total = state.planRows.length;
  if (state.status === "idle") return "";
  if (state.status === "running") {
    const current = Math.min(state.stepsUsed + 1, Math.max(total, 1));
    return `Step ${current} of ${total}`;
  }
  const finished = state.counts.done + state.counts.failed + state.counts.skipped;
  if (state.status === "cancelled") return `Stopped after ${finished} of ${total} steps`;
  if (state.truncated) return `Step budget reached — ${finished} of ${total} ran`;
  if (state.counts.failed) return `${state.counts.failed} step(s) failed, ${state.counts.done} succeeded`;
  return `${state.counts.done} of ${total} steps done`;
}

/** Every card, in transcript order — what the timeline renders. */
export function visibleItems(state: AgentRunState): Array<TranscriptItem & { card?: ToolCard }> {
  return state.items.map((item) =>
    item.kind === "card" ? { ...item, card: state.cards[item.stepId] } : item,
  );
}
