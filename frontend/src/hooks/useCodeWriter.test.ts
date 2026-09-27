/**
 * useCodeWriter.test.ts — the writer's stop handshake.
 *
 * The hook is the only place that knows whether a generation ended because the model
 * finished, because it failed, or because the reader pressed Stop. The first two are
 * already distinguished by the error marker; this covers the third, which the panel
 * renders as a partial answer rather than a failure — the code that arrived is real
 * code, and a red banner would send the user back to the model for it.
 */
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { apiFetch } from "../api";
import { useCodeWriter } from "./useCodeWriter";

vi.mock("../api", () => ({ apiFetch: vi.fn() }));
const fetchMock = apiFetch as unknown as Mock;

afterEach(() => {
  fetchMock.mockReset();
});

const status = (payload: string) => `__STATUS__${payload}__STATUS_END__\n`;

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
      }),
    },
  };
}

describe("useCodeWriter — stop", () => {
  it("treats a Stop as a state, not a failure", async () => {
    let run: Promise<void> = Promise.resolve();
    fetchMock.mockImplementation(async (_url: string, init: { signal?: AbortSignal } = {}) => {
      let index = 0;
      const chunks = [status('{"step": "generating", "mode": "generate", "message": "Generating python code…"}'), "def add(a, b):\n"];
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
                init.signal?.addEventListener("abort", () =>
                  reject(new DOMException("The user aborted a request.", "AbortError")),
                );
              }),
          }),
        },
      };
    });

    const { result } = renderHook(() => useCodeWriter());
    act(() => { run = result.current.generate("add two numbers", "python", "calc.py", []); });
    await waitFor(() => expect(result.current.isGenerating).toBe(true));

    act(() => result.current.stop());
    // Drawn immediately, not when the rejection lands: the reader pressed a button and
    // the panel answers, including when there is no in-flight request left to reject.
    expect(result.current.stopped).toBe(true);
    expect(result.current.isGenerating).toBe(false);

    await act(async () => { await run; });

    expect(result.current.isGenerating).toBe(false);
    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    expect((fetchMock.mock.calls[0][1] as { signal: AbortSignal }).signal.aborted).toBe(true);
    expect(result.current.output).toContain("def add");
    expect(result.current.currentStep).toMatch(/what was generated before it stopped/);
  });

  it("shows the server's marker when it noticed the departure first", async () => {
    fetchMock.mockResolvedValue(respond([
      status('{"step": "cancelled", "mode": "generate", "tokens": 41, "incomplete": true, '
             + '"message": "Write stopped after 41 token(s) — the file is incomplete"}'),
    ]));

    const { result } = renderHook(() => useCodeWriter());
    await act(async () => { await result.current.generate("add two numbers", "python", "calc.py", []); });

    expect(result.current.stopped).toBe(true);
    expect(result.current.error).toBeNull();
    expect(result.current.currentStep).toBe("Write stopped after 41 token(s) — the file is incomplete");
    expect(result.current.steps.map((step) => step.step)).toContain("cancelled");
  });
});
