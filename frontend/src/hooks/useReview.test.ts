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
