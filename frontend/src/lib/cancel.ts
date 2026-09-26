/**
 * cancel.ts — the client half of the Stop handshake.
 *
 * The backend refuses to spend a model call once the reader is gone: every stream
 * takes a `should_stop` predicate wired to `Request.is_disconnected`. That guarantee
 * is only true if the request actually ends, and a browser keeps a fetch alive until
 * it is told otherwise — so the Stop button has to abort it. This module owns the
 * three things that are easy to get wrong and easy to copy badly:
 *
 *   * the controller must be per-run, so a Stop pressed after a run finished does not
 *     cancel the next one;
 *   * starting a run while another is in flight must cancel the old request, because
 *     two concurrent reviews on one local model is how you get a product that looks
 *     hung;
 *   * an aborted fetch arrives as an `AbortError`, and a user-initiated stop must not
 *     be rendered as a red failure — a Stop is a state, not an error.
 *
 * Unmounting aborts too. A panel that is closed while a repo review runs is exactly
 * the case the server-side guard was written for.
 */

import { useCallback, useEffect, useRef } from "react";

export interface StreamStop {
  /** Begin a run: aborts anything still in flight and returns its controller. */
  start: () => AbortController;
  /** End the current run on the user's request. Safe to call when nothing runs. */
  stop: () => void;
  /** Mark the run as over, so a later unmount does not abort the next one. */
  finish: () => void;
  /** True when the current run was stopped by the user rather than by a failure. */
  stopped: () => boolean;
}

export function useStreamStop(): StreamStop {
  const controllerRef = useRef<AbortController | null>(null);
  const stoppedRef = useRef(false);

  useEffect(
    () => () => {
      controllerRef.current?.abort();
      controllerRef.current = null;
    },
    [],
  );

  const start = useCallback(() => {
    controllerRef.current?.abort();
    stoppedRef.current = false;
    const controller = new AbortController();
    controllerRef.current = controller;
    return controller;
  }, []);

  const stop = useCallback(() => {
    stoppedRef.current = true;
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  const finish = useCallback(() => {
    controllerRef.current = null;
  }, []);

  const stopped = useCallback(() => stoppedRef.current, []);

  return { start, stop, finish, stopped };
}

/**
 * The rejection `fetch` produces for an aborted request.
 *
 * Matched by name rather than by `instanceof Error`, because the object is a
 * `DOMException` and engines disagree about whether that inherits from `Error` —
 * browsers say yes, jsdom says no. A check written against either one quietly stops
 * recognising a Stop in the other, which is precisely the case where a user's own
 * button gets reported to them as a failure.
 */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object"
    && error !== null
    && (error as { name?: unknown }).name === "AbortError"
  );
}
