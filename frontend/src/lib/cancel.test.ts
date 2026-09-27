/**
 * cancel.test.ts — the Stop controller, on its own.
 *
 * The three stream hooks share this because the failure modes are shared and
 * non-obvious: a controller reused across runs aborts the wrong request, a Stop
 * pressed after a run finished aborts the *next* one, and an abort that is treated as
 * an error tells the user their own button broke. Testing the reducer and the panel
 * cannot catch any of those.
 */
import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { isAbortError, useStreamStop } from "./cancel";

describe("useStreamStop", () => {
  it("aborts the current run and remembers that the user stopped it", () => {
    const { result } = renderHook(() => useStreamStop());

    let controller: AbortController | null = null;
    act(() => { controller = result.current.start(); });
    expect(result.current.stopped()).toBe(false);

    act(() => { result.current.stop(); });

    expect(controller!.signal.aborted).toBe(true);
    expect(result.current.stopped()).toBe(true);
  });

  it("clears the stopped flag when a new run starts", () => {
    // A Stop that stays true across runs would mark the *next* review stopped as soon
    // as it begins, which is the kind of bug that only shows up on the second try.
    const { result } = renderHook(() => useStreamStop());

    act(() => { result.current.start(); });
    act(() => { result.current.stop(); });
    expect(result.current.stopped()).toBe(true);

    act(() => { result.current.start(); });
    expect(result.current.stopped()).toBe(false);
  });

  it("cancels a run still in flight when a new one starts", () => {
    // Two concurrent reviews on one local model is how a product looks hung, so the
    // older request has to go rather than queue behind the new one.
    const { result } = renderHook(() => useStreamStop());

    let first: AbortController | null = null;
    act(() => { first = result.current.start(); });
    act(() => { result.current.start(); });

    expect(first!.signal.aborted).toBe(true);
  });

  it("does not let an unmount abort the next run", () => {
    const { result, unmount } = renderHook(() => useStreamStop());

    let controller: AbortController | null = null;
    act(() => { controller = result.current.start(); });
    act(() => { result.current.finish(); });
    unmount();

    expect(controller!.signal.aborted).toBe(false);
  });

  it("aborts a run that is still open when the panel unmounts", () => {
    // The case the server-side guard exists for: closing the tab mid-review.
    const { result, unmount } = renderHook(() => useStreamStop());

    let controller: AbortController | null = null;
    act(() => { controller = result.current.start(); });
    unmount();

    expect(controller!.signal.aborted).toBe(true);
  });

  it("stops safely when nothing is running", () => {
    const { result } = renderHook(() => useStreamStop());
    expect(() => act(() => { result.current.stop(); })).not.toThrow();
    expect(() => act(() => { result.current.finish(); })).not.toThrow();
  });
});

describe("isAbortError", () => {
  it("recognises the rejection by name, not by constructor", () => {
    expect(isAbortError(new DOMException("Aborted", "AbortError"))).toBe(true);
    expect(isAbortError(Object.assign(new Error("x"), { name: "AbortError" }))).toBe(true);
    expect(isAbortError(new TypeError("failed to fetch"))).toBe(false);
    expect(isAbortError("AbortError")).toBe(false);
    expect(isAbortError(null)).toBe(false);
  });

  it("matches what a browser rejects an aborted fetch with", () => {
    // The shape `fetch` uses, as closely as a unit test can hold it: a DOMException
    // named AbortError. This environment's DOMException does *not* inherit from
    // Error, which is why the check is by name — a version that asked for
    // `instanceof Error` would pass here and fail in a browser, or the other way
    // round, and neither is worth discovering in production.
    const rejection = new DOMException("The user aborted a request.", "AbortError");
    expect(isAbortError(rejection)).toBe(true);
    expect(isAbortError(new DOMException("Other", "NotFoundError"))).toBe(false);
  });
});
