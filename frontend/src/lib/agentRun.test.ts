/**
 * agentRun.test.ts — the reducer that turns status markers into a run.
 *
 * These fixtures are the payloads `backend/app/api/agent.py` actually emits (same
 * keys, same `step` values). If the two ever disagree, the panel does not throw —
 * it renders a card that never finishes, which is the kind of defect a UI test on a
 * mocked component would happily pass over. So the pairing rules get tested here,
 * against literals copied from the producer.
 */
import { beforeEach, describe, expect, it } from "vitest";
import type { StreamEvent } from "./stream";
import {
  appendReport, applyEvent, applyStreamError, initialRunState, markCancelled,
  reduceAll, resetIds, runSummary, visibleItems,
} from "./agentRun";

const starting = (tools: string[], extra: Record<string, unknown> = {}): StreamEvent => ({
  step: "starting",
  message: "Goal: fix the TLS call",
  run_id: "deadbeefcafe",
  mode: "deterministic",
  goal: "fix the TLS call",
  max_steps: 8,
  plan: tools.join(" → "),
  plan_steps: tools.map((tool, order) => ({ id: `plan:${tool}`, tool, order, state: "queued" })),
  ...extra,
});

const open = (tool: string, n = 1, args: Record<string, string> = {}): StreamEvent => ({
  step: "tool",
  message: `${tool}: running`,
  tool,
  step_id: `${tool}#${n}`,
  plan_id: `plan:${tool}`,
  args,
});

const close = (tool: string, n = 1, extra: Record<string, unknown> = {}): StreamEvent => ({
  step: "tool_done",
  message: `${tool}: done`,
  tool,
  step_id: `${tool}#${n}`,
  plan_id: `plan:${tool}`,
  ok: true,
  elapsed_ms: 12,
  preview: ["pkg/net.py · py"],
  preview_more: 0,
  count: 1,
  ...extra,
});

describe("applyEvent", () => {
  beforeEach(() => resetIds());

  it("opens a run from the starting marker", () => {
    const state = applyEvent(initialRunState(), starting(["retrieve_context", "read_file"]));

    expect(state.status).toBe("running");
    expect(state.runId).toBe("deadbeefcafe");
    expect(state.plan).toEqual(["retrieve_context", "read_file"]);
    expect(state.maxSteps).toBe(8);
    expect(state.planRows.map((row) => row.state)).toEqual(["queued", "queued"]);
  });

  it("falls back to the plan string when a server sends no plan_steps", () => {
    const event = starting(["retrieve_context", "read_file"]);
    delete event.plan_steps;
    const state = applyEvent(initialRunState(), event);

    expect(state.planRows.map((row) => row.tool)).toEqual(["retrieve_context", "read_file"]);
    expect(state.planRows[0].id).toBe("plan:retrieve_context");
  });

  it("starts clean: the previous run's cards must not be attributed to this one", () => {
    let state = reduceAll([starting(["read_file"]), open("read_file"), close("read_file")]);
    state = applyEvent(state, starting(["read_file"]));

    expect(Object.keys(state.cards)).toEqual([]);
    expect(state.items).toEqual([]);
    expect(state.report).toBe("");
    expect(state.counts).toEqual({ done: 0, failed: 0, skipped: 0 });
  });

  it("binds a finish to its start by step id, not by tool name", () => {
    const state = reduceAll([
      starting(["read_file"]),
      open("read_file", 1, { source: "pkg/a.py" }),
      open("read_file", 2, { source: "pkg/b.py" }),
      close("read_file", 2, { elapsed_ms: 40, preview: ["pkg/b.py · 91 chars"] }),
    ]);

    expect(state.cards["read_file#1"].state).toBe("running");
    expect(state.cards["read_file#2"].state).toBe("done");
    expect(state.cards["read_file#2"].elapsedMs).toBe(40);
    expect(state.cards["read_file#2"].args).toEqual({ source: "pkg/b.py" });
    expect(state.cards["read_file#2"].preview).toEqual(["pkg/b.py · 91 chars"]);
    expect(state.openStepIds).toEqual(["read_file#1"]);
    expect(state.stepsUsed).toBe(2);
  });

  it("keeps the card's message from whichever marker is more specific", () => {
    const state = reduceAll([starting(["read_file"]), open("read_file"), close("read_file")]);
    expect(state.cards["read_file#1"].message).toBe("read_file: done");

    const silent = reduceAll([
      starting(["read_file"]),
      open("read_file", 1, { source: "a.py" }),
      { ...close("read_file", 1), message: "" },
    ]);
    expect(silent.cards["read_file#1"].message).toBe("read_file: running");
  });

  it("separates a failure from a skip, because they want different follow-ups", () => {
    const failed = reduceAll([
      starting(["autofix"]),
      open("autofix"),
      { ...close("autofix"), step: "tool_error", ok: false, message: "autofix failed: chroma down" },
    ]);
    expect(failed.cards["autofix#1"].state).toBe("failed");
    expect(failed.counts).toEqual({ done: 0, failed: 1, skipped: 0 });

    const skipped = reduceAll([
      starting(["autofix"]),
      { step: "tool_skipped", message: "no auto-fixable findings", tool: "autofix", plan_id: "plan:autofix" },
    ]);
    expect(skipped.cards["plan:autofix#skipped"].state).toBe("skipped");
    expect(skipped.counts.skipped).toBe(1);
    expect(skipped.planRows[0].state).toBe("skipped");
  });

  it("rolls a plan row up to running → done → failed", () => {
    let state = applyEvent(initialRunState(), starting(["read_file", "autofix"]));
    state = applyEvent(state, open("read_file", 1));
    expect(state.planRows[0].state).toBe("running");

    state = applyEvent(state, close("read_file", 1));
    expect(state.planRows[0].state).toBe("done");
    expect(state.planRows[1].state).toBe("queued");

    state = applyEvent(state, open("read_file", 2));
    state = applyEvent(state, close("read_file", 2));
    expect(state.planRows[0].cards).toBe(2);
    expect(state.planRows[0].runs).toBe(2);

    state = applyEvent(state, { ...open("autofix"), step: "tool" });
    state = applyEvent(state, { ...close("autofix"), step: "tool_error", ok: false });
    expect(state.planRows[1].state).toBe("failed");
    // A failure anywhere keeps the row red even after a later attempt succeeds.
    state = applyEvent(state, open("autofix", 2));
    state = applyEvent(state, close("autofix", 2));
    expect(state.planRows[1].state).toBe("failed");
  });

  it("treats a finish with no start as a finished card rather than dropping it", () => {
    const state = reduceAll([starting(["read_file"]), close("read_file", 7, { elapsed_ms: 3 })]);

    const card = state.cards["read_file#7"];
    expect(card).toBeDefined();
    expect(card.state).toBe("done");
    expect(card.elapsedMs).toBe(3);
  });

  it("collects the scalar result fields, without the plumbing", () => {
    const state = reduceAll([
      starting(["create_pr"]),
      open("create_pr"),
      close("create_pr", 1, {
        status: "manual", digest: "abc123", head: "savflux/fix", count: 1,
        patch: "diff --git a/x\n".repeat(60),
      }),
    ]);
    const fields = state.cards["create_pr#1"].fields;

    expect(fields).toMatchObject({ status: "manual", digest: "abc123", head: "savflux/fix", count: 1 });
    expect(fields.step_id).toBeUndefined();
    expect(fields.ok).toBeUndefined();
    expect(fields.elapsed_ms).toBeUndefined();
    expect(fields.preview).toBeUndefined(); // a list is a payload, shown as `preview`
    // A producer that puts a whole diff in a field must not get a 40 KB card.
    expect(String(fields.patch).length).toBe(240);
    expect(String(fields.patch).endsWith("\u2026")).toBe(true);
  });

  it("lifts the pull-request outcome out for its button", () => {
    const manual = reduceAll([
      starting(["create_pr"]),
      open("create_pr"),
      close("create_pr", 1, { status: "manual", digest: "abc123", head: "savflux/fix", title: "Fix TLS" }),
    ]);
    expect(manual.pendingConfirm).toEqual({ digest: "abc123", branch: "savflux/fix", title: "Fix TLS" });
    expect(manual.prUrl).toBeNull();

    const opened = reduceAll([
      starting(["create_pr"]),
      open("create_pr"),
      close("create_pr", 1, { status: "created", url: "https://x/pull/12", number: 12, digest: "abc123" }),
    ]);
    expect(opened.prUrl).toBe("https://x/pull/12");
    expect(opened.prNumber).toBe(12);
    expect(opened.pendingConfirm).toBeNull();
  });

  it("keeps plan reasoning in the transcript, where it happened", () => {
    const state = reduceAll([
      starting(["read_file"]),
      { step: "reasoning", kind: "plan", message: "No change was asked for, so the plan stays read-only." },
      open("read_file"),
      { step: "reasoning", kind: "plan", message: "Retrieval found one candidate." },
    ]);

    expect(state.items.map((item) => item.kind)).toEqual(["reasoning", "card", "reasoning"]);
  });

  it("surfaces an unrecognised step instead of swallowing it", () => {
    const state = reduceAll([starting(["read_file"]), { step: "quantum_check", message: "Entangling…" }]);
    const notices = visibleItems(state).filter((item) => item.kind === "notice");

    expect(notices).toHaveLength(1);
    expect(notices[0]).toMatchObject({ text: "Entangling…", tone: "info" });
  });

  it("finishes with the server's own totals", () => {
    const state = reduceAll([
      starting(["read_file", "autofix"]),
      open("read_file"),
      close("read_file"),
      { step: "complete", message: "Agent run finished", run_id: "x", steps_used: 2, max_steps: 8,
        elapsed_ms: 431, truncated: false, done: 1, failed: 0, skipped: 1, ok: true },
    ]);

    expect(state.status).toBe("finished");
    expect(state.elapsedMs).toBe(431);
    expect(state.stepsUsed).toBe(2);
    expect(state.counts).toEqual({ done: 1, failed: 0, skipped: 1 });
  });

  it("marks an open step interrupted when the run is cancelled server-side", () => {
    const state = reduceAll([
      starting(["read_file", "autofix"]),
      open("read_file"),
      { step: "cancelled", message: "Run stopped before the report", elapsed_ms: 90,
        interrupted: [{ step_id: "read_file#1", plan_id: "plan:read_file", tool: "read_file", elapsed_ms: 12 }] },
    ]);

    expect(state.status).toBe("cancelled");
    expect(state.cards["read_file#1"].state).toBe("interrupted");
    expect(state.planRows[0].state).toBe("failed");
  });

  it("says when the budget rather than the plan ended the run", () => {
    const state = reduceAll([
      starting(["a", "b", "c"], { max_steps: 1 }),
      { step: "complete", message: "done", steps_used: 1, max_steps: 1, truncated: true,
        elapsed_ms: 5, done: 1, failed: 0, skipped: 0 },
    ]);

    expect(state.truncated).toBe(true);
    expect(runSummary(state)).toContain("Step budget reached");
  });
});

describe("client-side stop and failure", () => {
  beforeEach(() => resetIds());

  it("interrupts what is still running the moment the reader stops it", () => {
    const running = reduceAll([starting(["read_file"]), open("read_file"), open("read_file", 2)]);
    const stopped = markCancelled(running);

    expect(stopped.status).toBe("cancelled");
    expect(stopped.cards["read_file#1"].state).toBe("interrupted");
    expect(stopped.cards["read_file#2"].state).toBe("interrupted");
    expect(stopped.openStepIds).toEqual([]);
    expect(runSummary(stopped)).toContain("Stopped");
  });

  it("does not rewrite a run that already finished", () => {
    const done = reduceAll([starting(["read_file"]), open("read_file"), close("read_file"),
      { step: "complete", message: "done", elapsed_ms: 12 }]);

    expect(markCancelled(done).status).toBe("finished");
  });

  it("records a stream error as a failure, with the text the server sent", () => {
    const state = applyStreamError(reduceAll([starting(["read_file"]), open("read_file")]), "429 rate limited");

    expect(state.status).toBe("failed");
    expect(state.error).toBe("429 rate limited");
    expect(state.cards["read_file#1"].state).toBe("interrupted");
  });

  it("appends report prose without touching the transcript", () => {
    const state = appendReport(appendReport(reduceAll([starting(["read_file"])]), "# Report\n"), "\nbody");
    expect(state.report).toBe("# Report\n\nbody");
    expect(appendReport(state, "").report).toBe(state.report);
  });
});
