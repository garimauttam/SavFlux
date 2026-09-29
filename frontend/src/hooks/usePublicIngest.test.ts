/**
 * Tests for usePublicIngest.
 *
 * The case that matters most here is the last one. A failed index used to look
 * exactly like a successful one: the backend's `step: "complete"` event carries
 * `status: "error"`, and the client was reading every SSE line as progress. The
 * panel showed a spinner, the spinner stopped, the page refreshed the index,
 * and the user was left staring at an empty screen with no explanation. An
 * index that did not happen has to say so.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePublicIngest } from "./usePublicIngest";

afterEach(() => vi.restoreAllMocks());

/**
 * An SSE response, one `data: {...}` per line. The whole `Response` has to come
 * back, not just `.body` — the hook checks `res.ok` before it starts reading, so
 * a bare stream is rejected as "not ok" and every failure here looks like a
 * transport error instead of the case under test.
 */
function sse(lines: object[]): Response {
  const body = lines.map((l) => `data: ${JSON.stringify(l)}\n\n`).join("");
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("usePublicIngest", () => {
  it("ignores a blank URL instead of requesting one", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValue(new Error("the hook must not request a blank URL"));
    const { result } = renderHook(() => usePublicIngest(() => {}));

    await act(async () => {
      expect(await result.current.ingestUrl("   ")).toBe(false);
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("posts the URL and reports success once the stream completes", async () => {
    const onIndexed = vi.fn();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([
        { step: "queued", job_id: "j1" },
        { step: "scanning", message: "Scanning files..." },
        { step: "complete", status: "success", message: "Indexed 12 files" },
      ]),
    );
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    await act(async () => {
      expect(await result.current.ingestUrl("https://github.com/pallets/click")).toBe(true);
    });

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/v1/ingest/github");
    expect(JSON.parse(String(init.body)).repo_url).toBe("https://github.com/pallets/click");
    expect(JSON.parse(String(init.body)).branch).toBe("");
    await waitFor(() => expect(onIndexed).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBeNull();
    expect(result.current.ingesting).toBeNull();
  });

  it("trims the URL before sending it", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([{ step: "complete", status: "success" }]),
    );
    const { result } = renderHook(() => usePublicIngest(() => {}));

    await act(async () => {
      await result.current.ingestUrl("  https://github.com/pallets/click  ");
    });

    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body)).repo_url).toBe("https://github.com/pallets/click");
    expect(JSON.parse(String(init.body)).branch).toBe("");
  });

  it("forwards the selected remote branch rather than silently indexing the default", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([{ step: "complete", status: "success" }]),
    );
    const { result } = renderHook(() => usePublicIngest(() => {}));
    await act(async () => {
      await result.current.ingestUrl("https://github.com/owner/repo", " feature/auth ");
    });
    const init = fetchSpy.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      repo_url: "https://github.com/owner/repo", branch: "feature/auth",
    });
  });

  // The silent-failure regression.
  it("surfaces the backend's error status instead of reporting success", async () => {
    const onIndexed = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([
        { step: "cloning", message: "Cloning..." },
        {
          step: "complete",
          status: "error",
          message: "Could not reach github.com. Check your connection and try again.",
        },
      ]),
    );
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    await act(async () => {
      expect(await result.current.ingestUrl("https://github.com/pallets/click")).toBe(false);
    });

    expect(result.current.error).toMatch(/Could not reach github\.com/);
    // Refreshing the index here is what made the failure look like a success.
    expect(onIndexed).not.toHaveBeenCalled();
    expect(result.current.ingesting).toBeNull();
  });

  it("surfaces a progress line without treating it as the outcome", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      sse([
        { step: "scanning", message: "Scanning files..." },
        { step: "embedding", message: "Embedding", files_indexed: 7 },
      ]),
    );
    const onIndexed = vi.fn();
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    await act(async () => {
      // No complete event at all — the stream just ends.
      await result.current.ingestUrl("https://github.com/pallets/click");
    });

    // Do not advertise a selected branch as indexed after an interrupted job.
    expect(onIndexed).not.toHaveBeenCalled();
    expect(result.current.error).toMatch(/did not report success/);
  });

  it("reports an HTTP failure before the stream opens", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ detail: "nope" }, 400));
    const onIndexed = vi.fn();
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    await act(async () => {
      expect(await result.current.ingestUrl("https://github.com/pallets/click")).toBe(false);
    });

    expect(result.current.error).toBeTruthy();
    expect(onIndexed).not.toHaveBeenCalled();
  });

  it("uploads every chosen file and clears the input", async () => {
    const onIndexed = vi.fn();
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ status: "success" }));
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    const files = [new File(["a"], "a.py"), new File(["b"], "b.py")];
    await act(async () => {
      expect(
        await result.current.upload({
          length: 2,
          0: files[0],
          1: files[1],
          item: () => null,
        } as unknown as FileList),
      ).toBe(true);
    });

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toContain("/api/v1/ingest/files");
    expect((init!.body as FormData).getAll("files")).toHaveLength(2);
    expect(onIndexed).toHaveBeenCalledTimes(1);
  });

  it("does nothing for an empty selection", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValue(new Error("the hook must not request an empty selection"));
    const { result } = renderHook(() => usePublicIngest(() => {}));

    await act(async () => {
      expect(await result.current.upload(null)).toBe(false);
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("surfaces an upload failure", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ detail: "too large" }, 413));
    const onIndexed = vi.fn();
    const { result } = renderHook(() => usePublicIngest(onIndexed));

    await act(async () => {
      expect(
        await result.current.upload({
          length: 1,
          0: new File(["a"], "a.py"),
          item: () => null,
        } as unknown as FileList),
      ).toBe(false);
    });

    expect(result.current.error).toBeTruthy();
    expect(onIndexed).not.toHaveBeenCalled();
  });
});
