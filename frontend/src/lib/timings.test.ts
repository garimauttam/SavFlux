/**
 * timings.test.ts — the rules about what may be shown, and what may never be invented.
 *
 * The dangerous failure here is not a crash, it is a plausible number: a cached file
 * drawn as "0 ms" of model time, a run whose total was never reported drawn as "0 ms"
 * of everything. Every test below is a fence around one of those.
 */
import { describe, expect, it } from "vitest";
import {
  EMPTY_TIMINGS, MAX_FILE_ROWS, applyTiming, stagesFor, timingsSummary,
} from "./timings";

const fold = (...markers: Record<string, unknown>[]) =>
  markers.reduce((state, meta) => applyTiming(state, meta), EMPTY_TIMINGS);

describe("applyTiming", () => {
  it("takes the pre-pass cost off the fast mode's writing marker", () => {
    const timings = fold({ step: "writing", mode: "fast", analysis_ms: 41, findings: 3 });

    expect(timings).toMatchObject({ mode: "fast", analysisMs: 41, findings: 3 });
    // The writing marker's `elapsed_ms` is an investigation number in agentic mode
    // only; reading it as analysis time would double-count the same milliseconds.
    expect(timings.investigateMs).toBeNull();
  });

  it("reads an agentic writing marker's elapsed as the investigation phase", () => {
    const timings = fold({ step: "writing", mode: "agentic", elapsed_ms: 2_300 });
    expect(timings.investigateMs).toBe(2_300);
    expect(timings.analysisMs).toBeNull();
  });

  it("takes the single-file total from the completion marker", () => {
    const timings = fold(
      { step: "writing", mode: "fast", analysis_ms: 41, findings: 3 },
      { step: "complete", mode: "fast", elapsed_ms: 3_900, analysis_ms: 41, findings: 3 },
    );

    expect(timings.totalMs).toBe(3_900);
    expect(timings.findings).toBe(3);
  });

  it("keeps the agentic ceiling as a fact, not as a success", () => {
    const done = fold({ step: "complete", mode: "agentic", elapsed_ms: 90_000, iterations: 8, tool_calls: 14, forced: true });

    expect(done.forced).toBe(true);
    expect(done.iterations).toBe(8);
    expect(done.toolCalls).toBe(14);
  });

  it("ignores a step it does not know, and returns the same object", () => {
    const before = fold({ step: "planned", plan_ms: 10 });
    expect(applyTiming(before, { step: "brand_new_stage", elapsed_ms: 5 })).toBe(before);
  });

  it("refuses numbers that are not numbers", () => {
    const timings = fold({ step: "complete", elapsed_ms: -4, analysis_ms: "12", findings: Number.NaN, mode: "  " });

    expect(timings).toMatchObject({ totalMs: null, analysisMs: null, findings: null, mode: null });
  });

  describe("per-file rows", () => {
    it("keeps one row per file, worst model time first", () => {
      const timings = fold(
        { step: "complete", file: "a.py", llm_ms: 900, wait_ms: 10 },
        { step: "complete", file: "b.py", llm_ms: 4_000, wait_ms: 800 },
        { step: "complete", file: "c.py", llm_ms: 1_200, wait_ms: 5 },
        { step: "complete", file: "b.py", llm_ms: 4_500, wait_ms: 800 }, // retried
      );

      expect(timings.files.map((entry) => entry.file)).toEqual(["b.py", "c.py", "a.py"]);
      expect(timings.files[0].llmMs).toBe(4_500);
    });

    it("keeps a measured zero, because a 0 ms local answer is a measurement", () => {
      const timings = fold({ step: "complete", file: "fast.py", llm_ms: 0, wait_ms: 0 });

      expect(timings.files[0]).toEqual({ file: "fast.py", llmMs: 0, waitMs: 0, cached: undefined });
    });

    it("records a cached answer as no model time rather than zero model time", () => {
      const timings = fold({ step: "complete", file: "cached.py", cached: true, wait_ms: 3 });

      expect(timings.files[0].llmMs).toBeNull();
      expect(timings.files[0].cached).toBe(true);
    });

    it("keeps the expensive end when the list is truncated", () => {
      const many = Array.from({ length: MAX_FILE_ROWS + 3 }, (_, i) => ({
        step: "complete", file: `f${i}.py`, llm_ms: i * 100, wait_ms: 1,
      }));
      const timings = fold(...many);

      expect(timings.files).toHaveLength(MAX_FILE_ROWS);
      expect(timings.files[0].llmMs).toBe((MAX_FILE_ROWS + 2) * 100);
      expect(timings.files.map((entry) => entry.llmMs)).toEqual([700, 600, 500, 400, 300]);
    });
  });

  it("splits the plan markers from the actual ones", () => {
    const timings = fold(
      { step: "planned", total: 5, static_only: 1, batched_files: 4, batch_count: 1,
        model_calls: 1, plan_ms: 12, context_ms: 810 },
      { step: "timing", stage: "files", elapsed_ms: 3_050, model_calls: 2, cache_hits: 1,
        static_only: 1, slowest_file: "net.py", llm_ms: 2_900, wait_ms: 120 },
    );

    // `model_calls` appears in both markers: the plan's intention is the coverage
    // strip's business, so what lands here is the count of calls actually made.
    expect(timings).toMatchObject({
      planMs: 12, contextMs: 810, filesMs: 3_050, modelCalls: 2, cacheHits: 1, staticOnly: 1,
    });
    expect(timings.slowest).toEqual({ file: "net.py", llmMs: 2_900, waitMs: 120 });
  });

  it("does not treat a stage summary for something else as this run's total", () => {
    const timings = fold({ step: "timing", stage: "summary", elapsed_ms: 500 });
    expect(timings.filesMs).toBeNull();
  });
});

describe("stagesFor", () => {
  it("is empty when nothing was measured", () => {
    expect(stagesFor(EMPTY_TIMINGS)).toEqual([]);
  });

  it("names the model what is left after the pre-pass", () => {
    const timings = fold(
      { step: "writing", mode: "fast", analysis_ms: 41, findings: 3 },
      { step: "complete", elapsed_ms: 1_041, analysis_ms: 41 },
    );

    expect(stagesFor(timings)).toEqual([
      { key: "model", label: "Model", ms: 1_000, hint: "1 streamed call" },
      { key: "analysis", label: "Static analysis", ms: 41, hint: "3 finding(s)" },
    ]);
  });

  it("refuses to subtract its way to a negative model time", () => {
    // A cached or partially-reported run can legitimately arrive with
    // `analysis_ms > elapsed_ms` if the two were measured against different clocks
    // across a restart; drawing "-300 ms of model" would be a fabricated number.
    const timings = fold({ step: "complete", elapsed_ms: 10, analysis_ms: 400 });
    const stages = stagesFor(timings);

    expect(stages.map((stage) => stage.key)).toEqual(["analysis"]);
    expect(stages.find((stage) => stage.key === "model")).toBeUndefined();
  });

  it("orders a repo run by cost, with the plan and the context separated", () => {
    const timings = fold(
      { step: "planned", plan_ms: 12, context_ms: 810 },
      { step: "timing", stage: "files", elapsed_ms: 3_050, model_calls: 2, cache_hits: 1 },
    );

    const stages = stagesFor(timings);
    expect(stages.map((stage) => stage.key)).toEqual(["files", "context", "plan"]);
    expect(stages[0].hint).toBe("2 model call(s) · 1 cache hit(s)");
  });

  it("calls the whole thing a review when no split was reported", () => {
    // A run that reports one total and no pre-pass gets one honest bar, not a
    // "Model" stage whose size was guessed by subtracting nothing.
    const timings = fold({ step: "planned", total_ms: 900 });
    expect(stagesFor(timings)).toEqual([
      { key: "model", label: "Review", ms: 900, hint: "1 streamed call" },
    ]);
  });
});

describe("timingsSummary", () => {
  it("points at the dominant stage", () => {
    const timings = fold(
      { step: "planned", plan_ms: 12, context_ms: 810 },
      { step: "timing", stage: "files", elapsed_ms: 3_050 },
    );
    expect(timingsSummary(timings)).toBe("Reviewing files is 79% of the measured time");
  });

  it("says nothing rather than nothing-at-all", () => {
    expect(timingsSummary(EMPTY_TIMINGS)).toBe("");
  });
});
