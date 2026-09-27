/**
 * useReview.test.ts — the two review hooks, fed a real stream.
 *
 * These are the tests that make the protocol claim true rather than local: the
 * review surfaces decode with the same scanner the agent panel uses, and the timing
 * markers end up in state that the panel can render. A unit test of the decoder
 * cannot prove that routing, and a component test with a hand-written state object
 * cannot either.
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import { useReview } from "./useReview";
import { useMultiReview } from "./useMultiReview";
import type { IndexedFile } from "../types";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = apiFetch as unknown as Mock;

const file = { source: "https://github.com/o/r::pkg/net.py", file_name: "net.py", language: "python" } as IndexedFile;

function respond(chunks: string[]) {
  let index = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length
            ? { done: false, value: new TextEncoder().encode(chunks[index++]) }
            : { done: true, value: undefined },
        cancel: () => Promise.resolve(),
      }),
    },
  };
}

const status = (payload: string) => `__STATUS__${payload}__STATUS_END__\n`;

beforeEach(() => {
  vi.spyOn(window, "setInterval").mockReturnValue(0 as unknown as NodeJS.Timeout);
  vi.spyOn(window, "clearInterval").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  fetchMock.mockReset();
});

describe("useReview — single file", () => {
  it("routes the review to the text and its timings to state", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "starting", "mode": "fast", "message": "Scanning `net.py`…"}'),
      status('{"step": "writing", "mode": "fast", "analysis_ms": 41, "findings": 3, "message": "Writing review… (3 finding(s) from static analysis)"}'),
      "\n## 🐛 Bugs & Risks\nverify=False on line 4.\n",
      // Deliberately split mid-token: a marker that arrives in two pieces must still
      // be a marker on this surface too, not the end of the review text.
      '__STATUS__{"step": "comp',
      'lete", "mode": "fast", "elapsed_ms": 3900, "analysis_ms": 41, "findings": 3}__STATUS_END__\n',
    ]));

    const { result } = renderHook(() => useReview());
    await act(async () => {
      await result.current.reviewFile(file);
    });

    expect(result.current.review).toContain("verify=False on line 4.");
    expect(result.current.isReviewing).toBe(false);
    expect(result.current.timings).toMatchObject({ mode: "fast", totalMs: 3900, analysisMs: 41, findings: 3 });
    expect(result.current.currentStep).toBeNull();
    // The trace still gets the marker as its own row: the breakdown is a summary,
    // not a replacement for the step list.
    expect(result.current.agentSteps.map((step) => step.step)).toEqual(["starting", "writing", "complete"]);
    // And a marker never leaks into the review text.
    expect(result.current.review).not.toContain("__STATUS__");
  });

  it("starts every run with an empty split, not the last one's", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "complete", "mode": "fast", "elapsed_ms": 100, "analysis_ms": 10}'),
    ]));
    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });
    expect(result.current.timings.totalMs).toBe(100);

    fetchMock.mockResolvedValue(respond([status('{"step": "starting", "mode": "fast"}')]));
    await act(async () => { await result.current.reviewFile(file); });
    expect(result.current.timings.totalMs).toBeNull();
  });

  it("keeps a server error visible and stops the clock", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "starting", "mode": "fast", "message": "Scanning `net.py`…"}'),
      "__ERROR__model unavailable__ERROR_END__\n",
    ]));

    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });

    expect(result.current.error).toBe("model unavailable");
    expect(result.current.isReviewing).toBe(false);
  });

  it("reports a non-ok response through the error field, not a crash", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, json: async () => ({ detail: "not indexed" }) });

    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });

    expect(result.current.error).toBe("not indexed");
  });
});

describe("useMultiReview — a repo run", () => {
  it("keeps the plan split and the per-file costs side by side", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "planned", "total": 2, "static_only": 1, "batched_files": 1, "batch_count": 1, ' +
             '"model_calls": 1, "plan_ms": 12, "context_ms": 810, "message": "Planned 2 files"}'),
      '__SECTION_START__{"id": "https://github.com/o/r::pkg/net.py", "file_name": "net.py"}__SECTION_END__\n',
      "review of net.py\n",
      status('{"step": "complete", "id": "https://github.com/o/r::pkg/net.py", "file": "net.py", ' +
             '"index": 1, "total": 2, "tier": "deep", "llm_ms": 2900, "wait_ms": 120, "message": "Review done: `net.py`"}'),
      status('{"step": "timing", "stage": "files", "elapsed_ms": 3050, "model_calls": 2, "cache_hits": 1, ' +
             '"static_only": 1, "slowest_file": "net.py", "llm_ms": 2900, "wait_ms": 120, ' +
             '"message": "2 model call(s), 1 cache hit(s) in 3050 ms"}'),
    ]));

    const { result } = renderHook(() => useMultiReview());
    await act(async () => { await result.current.reviewFiles([file]); });

    await waitFor(() => expect(result.current.sections.length).toBe(1));

    expect(result.current.timings).toMatchObject({
      planMs: 12, contextMs: 810, filesMs: 3050, modelCalls: 2, cacheHits: 1, staticOnly: 1,
    });
    expect(result.current.timings.slowest).toEqual({ file: "net.py", llmMs: 2900, waitMs: 120 });
    expect(result.current.timings.files).toEqual([{ file: "net.py", llmMs: 2900, waitMs: 120, cached: undefined }]);
    expect(result.current.sections[0].content).toContain("review of net.py");
    expect(result.current.sections[0].content).not.toContain("__STATUS__");
  });

  it("does not let a marker's timing numbers become review prose", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "planned", "total": 1, "plan_ms": 3, "context_ms": 4, "message": "Planned 1 file"}'),
      `__SECTION_START__{"id": "${file.source}", "file_name": "net.py"}__SECTION_END__\n`,
      "text before ",
      // A split marker in the middle of a file's prose: the prose must survive the
      // join with no marker debris either side of it.
      "__STATUS__{\"step\": \"complete\", \"file\": \"a.py\", \"el",
      "apsed_ms\": 90, \"llm_ms\": 80, \"wait_ms\": 10, \"message\": \"done\"}__STATUS_END__\n",
      "and after",
    ]));

    const { result } = renderHook(() => useMultiReview());
    await act(async () => { await result.current.reviewFiles([file]); });

    expect(result.current.sections[0].content).toBe("text before and after");
    expect(result.current.timings.files[0]).toMatchObject({ file: "a.py", llmMs: 80, waitMs: 10 });
  });
});

/**
 * A stream that emits what it is given and then stays open until the request is
 * aborted — which is the only honest way to test a Stop, because a Stop is a thing
 * that happens *during* a run. A stream that ends immediately would let an
 * implementation pass by finishing rather than by stopping.
 */
function hangingResponse(chunks: string[], signal?: AbortSignal | null) {
  let index = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: () =>
          new Promise((resolve, reject) => {
            if (index < chunks.length) {
              resolve({ done: false, value: new TextEncoder().encode(chunks[index++]) });
              return;
            }
            if (signal?.aborted) {
              reject(new DOMException("The user aborted a request.", "AbortError"));
              return;
            }
            signal?.addEventListener("abort", () =>
              reject(new DOMException("The user aborted a request.", "AbortError")),
            );
          }),
      }),
    },
  };
}

describe("useReview — stop", () => {
  it("ends a run as a stop, not as a failure", async () => {
    let run: Promise<void> = Promise.resolve();
    fetchMock.mockImplementation(async (_url: string, init: { signal?: AbortSignal } = {}) =>
      hangingResponse([
        status('{"step": "starting", "mode": "fast", "message": "Scanning `net.py`…"}'),
        status('{"step": "writing", "mode": "fast", "message": "Writing review…"}'),
        "partial prose\n",
      ], init.signal),
    );

    const { result } = renderHook(() => useReview());
    act(() => { run = result.current.reviewFile(file); });
    await waitFor(() => expect(result.current.isReviewing).toBe(true));

    act(() => result.current.stop());
    await act(async () => { await run; });

    expect(result.current.isReviewing).toBe(false);
    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.currentStep).toMatch(/Stopped/);
    // The abort is what the server is watching: without it the review would keep
    // spending model time on a page that has stopped showing it.
    expect((fetchMock.mock.calls[0][1] as { signal: AbortSignal }).signal.aborted).toBe(true);
    // What already arrived stays readable — a stopped review is a partial answer.
    expect(result.current.review).toContain("partial prose");
  });

  it("shows the server's own account of where it stopped", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "starting", "mode": "agentic", "message": "Analyzing `net.py`…"}'),
      status('{"step": "cancelled", "mode": "agentic", "elapsed_ms": 2140, "iterations": 2, '
             + '"tools_run": 3, "message": "Review stopped: `net.py`"}'),
    ]));

    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });

    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.currentStep).toBe("Review stopped: `net.py`");
    // A cancelled run contributes nothing to the measured breakdown: no mode, no
    // total. The stages that would have earned those numbers never finished, and the
    // marker's own `elapsed_ms` is the run's wall time, not a review duration.
    expect(result.current.timings.mode).toBeNull();
    expect(result.current.timings.totalMs).toBeNull();
  });

  it("does not call an unterminated stream a finished review", async () => {
    // The socket closing politely is not a verdict. Before this rule a review cut off
    // mid-way showed as done, because the hook treated "no more chunks" as "no more
    // work" — which is exactly what a stopped or dropped run looks like from here.
    fetchMock.mockResolvedValue(respond([
      status('{"step": "starting", "mode": "fast", "message": "Scanning `net.py`…"}'),
      "## Bugs\nhalf a sentence",
    ]));

    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });

    expect(result.current.stopped).toBe(true);
    expect(result.current.currentStep).toMatch(/ended before the review finished/);
    expect(result.current.review).toContain("half a sentence");
  });

  it("leaves a run that reported complete un-stopped", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "complete", "mode": "fast", "elapsed_ms": 100, "analysis_ms": 10}'),
      "the review\n",
    ]));

    const { result } = renderHook(() => useReview());
    await act(async () => { await result.current.reviewFile(file); });

    expect(result.current.stopped).toBe(false);
    expect(result.current.isReviewing).toBe(false);
  });
});

describe("useMultiReview — stop", () => {
  it("marks the files a stopped run never reached", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "planned", "total": 2, "model_calls": 2, "message": "Planned 2 files"}'),
      `__SECTION_START__{"id": "${file.source}", "file_name": "net.py"}__SECTION_END__\n`,
      "the one review that finished\n",
      status(`{"step": "complete", "id": "${file.source}", "file": "net.py", "tier": "full", "message": "Review done: \`net.py\`"}`),
      `__SECTION_START__{"id": "https://github.com/o/r::pkg/db.py", "file_name": "db.py"}__SECTION_END__\n`,
      status('{"step": "cancelled", "elapsed_ms": 4400, "published": 1, "not_reviewed": 1, '
             + '"files_skipped": ["db.py"], "message": "Stopped — 1 of 2 file(s) were not reviewed"}'),
    ]));

    const { result } = renderHook(() => useMultiReview());
    await act(async () => { await result.current.reviewFiles([file]); });

    await waitFor(() => expect(result.current.sections.length).toBe(2));
    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.isReviewing).toBe(false);
    expect(result.current.currentStep).toBe("Stopped — 1 of 2 file(s) were not reviewed");

    // The finished file keeps its review and its status; the other is named as
    // skipped rather than left looking like it is still queued.
    expect(result.current.sections[0].status).toBe("complete");
    expect(result.current.sections[0].content).toContain("the one review that finished");
    expect(result.current.sections[1].status).toBe("skipped");
    expect(result.current.sections[1].statusMessage).toBe("Stopped — 1 of 2 file(s) were not reviewed");
    expect(result.current.agentSteps.map((step) => step.step)).toContain("cancelled");
  });

  it("aborting mid-run skips the whole pending queue and tells the server", async () => {
    let run: Promise<void> = Promise.resolve();
    fetchMock.mockImplementation(async (_url: string, init: { signal?: AbortSignal } = {}) =>
      hangingResponse([
        status('{"step": "planned", "total": 1, "model_calls": 1, "message": "Planned 1 file"}'),
        `__SECTION_START__{"id": "${file.source}", "file_name": "net.py"}__SECTION_END__\n`,
      ], init.signal),
    );

    const { result } = renderHook(() => useMultiReview());
    act(() => { run = result.current.reviewFiles([file]); });
    await waitFor(() => expect(result.current.isReviewing).toBe(true));

    act(() => result.current.stop());
    await act(async () => { await run; });

    expect((fetchMock.mock.calls[0][1] as { signal: AbortSignal }).signal.aborted).toBe(true);
    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    // The section that got no prose is skipped, and "skipped" is the status the panel
    // already renders distinctly from "error" and from an in-flight review.
    expect(result.current.sections[0].status).toBe("skipped");
  });
});
