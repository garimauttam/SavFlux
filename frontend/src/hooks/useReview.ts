/**
 * useReview.ts — Manages the code review agent's streaming state.
 *
 * This is structurally similar to useChat but with one key difference:
 * the stream contains TWO types of data interleaved:
 *   1. __STATUS__{json}__STATUS_END__             → agent status updates
 *   2. regular text tokens                       → the actual review markdown
 *
 * We parse them apart as the stream arrives and route them to different state fields.
 * The UI shows status badges ("Running: search_pattern...") while the review types out.
 *
 * WHY NOT USE A WEBSOCKET?
 * Same reason as before — one-directional stream, HTTP is simpler.
 * The status updates are part of the same stream as the text, just tagged differently.
 */

import { useState, useCallback } from "react";
import { IndexedFile } from "../types";
import { apiFetch } from "../api";
import { isAbortError, useStreamStop } from "../lib/cancel";
import { decodeStatus, drainMarkers, flushTail, REVIEW_TAGS } from "../lib/stream";
import { applyTiming, EMPTY_TIMINGS, type ReviewTimings } from "../lib/timings";

export interface AgentStep {
  tool?: string;
  message: string;
  step?: string;
  file?: string;
  mode?: string;
}

export interface ReviewState {
  isReviewing: boolean;
  review: string;               // the streamed markdown review text
  agentSteps: AgentStep[];      // tool calls the agent made (shown as a trace)
  currentStep: string | null;   // what the agent is doing right now
  currentMode: string | null;
  error: string | null;
  /**
   * The run ended because the reader stopped it, not because anything failed.
   *
   * Kept separate from `error` because they want opposite wording in the UI: a
   * stopped review still has the sections it produced, and telling someone their
   * review failed makes them re-run a run that did exactly what they asked.
   */
  stopped: boolean;
  /**
   * What the server said this review cost, per stage. Empty until a marker that
   * carries timing arrives — the panel renders nothing rather than a zero.
   */
  timings: ReviewTimings;
}

const STOPPED_STEP = "Stopped — the review was not finished.";

export function useReview() {
  const [state, setState] = useState<ReviewState>({
    isReviewing: false,
    review: "",
    agentSteps: [],
    currentStep: null,
    currentMode: null,
    error: null,
    stopped: false,
    timings: EMPTY_TIMINGS,
  });
  const stopCtl = useStreamStop();

  /**
   * End the run and say so locally, without waiting for the server.
   *
   * The abort kills the reader, so nothing further arrives to explain the gap —
   * the UI has to draw the stopped state itself rather than wait for a marker it
   * will never read.
   */
  const markStopped = useCallback((message: string) => {
    setState((prev) => ({
      ...prev,
      isReviewing: false,
      currentStep: message,
      stopped: true,
      // A Stop is not a failure, and the previous run's error must not survive it.
      error: null,
    }));
  }, []);

  const stop = useCallback(() => {
    stopCtl.stop();
    markStopped(STOPPED_STEP);
  }, [markStopped, stopCtl]);

  const reviewFile = useCallback(async (file: IndexedFile) => {
    setState({ isReviewing: true, review: "", agentSteps: [], currentStep: "Starting review...", currentMode: null, error: null, stopped: false, timings: EMPTY_TIMINGS });
    const controller = stopCtl.start();

    try {
      const response = await apiFetch("/api/v1/review/file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_path: file.source,
          file_name: file.file_name,
          language: file.language,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail ?? `Server error ${response.status}`);
      }

      await _consumeStream(response);
      stopCtl.finish();
    } catch (err) {
      // An abort is the Stop button, and Stop already drew its own state; re-entering
      // it as an error is how a deliberate stop turns into a red banner telling the
      // user something broke.
      if (isAbortError(err) || stopCtl.stopped()) return;
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, [stopCtl]);

  const reviewPaste = useCallback(async (code: string, language: string, fileName: string) => {
    setState({ isReviewing: true, review: "", agentSteps: [], currentStep: "Starting review...", currentMode: null, error: null, stopped: false, timings: EMPTY_TIMINGS });
    const controller = stopCtl.start();

    try {
      const response = await apiFetch("/api/v1/review/paste", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, language, file_name: fileName }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail ?? `Server error ${response.status}`);
      }

      await _consumeStream(response);
      stopCtl.finish();
    } catch (err) {
      // An abort is the Stop button, and Stop already drew its own state; re-entering
      // it as an error is how a deliberate stop turns into a red banner telling the
      // user something broke.
      if (isAbortError(err) || stopCtl.stopped()) return;
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, [stopCtl]);

  /**
   * Shared stream consumer — splits STATUS/ERROR markers out of the stream and
   * routes them to agentSteps; the prose between them is the review.
   *
   * The parsing is `lib/stream.ts`, the same module the agent panel uses. That is
   * the point: this stream and the agent stream are one protocol, so they must not
   * be decoded twice in two directions. It used to be, and the two copies
   * disagreed about a marker whose payload contained a nested object.
   */
  const _consumeStream = useCallback(async (response: Response) => {
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    // Whether the stream said how it ended. A review whose last marker was `writing`
    // did not finish, so the closing state below is decided by this flag rather than
    // by the socket closing politely.
    let terminal = false;

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const scan = drainMarkers(buffer, REVIEW_TAGS);
      buffer = scan.rest;

      for (const segment of scan.segments) {
        if (segment.kind === "text") {
          setState((prev) => ({ ...prev, review: prev.review + segment.value }));
          continue;
        }
        if (segment.kind === "error") {
          terminal = true;
          setState((prev) => ({
            ...prev,
            isReviewing: false,
            currentStep: null,
            error: segment.payload.trim() || "The review was interrupted by a server error.",
          }));
          return;
        }
        if (segment.kind !== "status") continue;

        const meta = decodeStatus(segment.payload);
        const message = typeof meta.message === "string" ? meta.message : "";
        if (meta.step === "complete") terminal = true;
        // The server can notice a departure the client never announced — a proxy
        // timing out, a tab closing — and it says so on the stream. Reaching that
        // marker is a stopped run, not a finished one.
        if (meta.step === "cancelled") {
          terminal = true;
          markStopped(message || STOPPED_STEP);
        }
        setState((prev) => ({
          ...prev,
          currentStep: message || null,
          currentMode: typeof meta.mode === "string" ? meta.mode : prev.currentMode,
          agentSteps: [...prev.agentSteps, { ...meta, message }].slice(-40),
          timings: applyTiming(prev.timings, meta),
        }));
      }
    }

    const tail = flushTail(buffer, REVIEW_TAGS);
    if (tail) setState((prev) => ({ ...prev, review: prev.review + tail }));
    if (!terminal) markStopped("The stream ended before the review finished.");
    // A finished run has nothing left to say, so its status line clears. A stopped one
    // keeps the sentence that says where it stopped.
    else setState((prev) => ({
      ...prev,
      isReviewing: false,
      currentStep: prev.stopped ? prev.currentStep : null,
    }));
  }, [markStopped]);

  const reset = useCallback(() => {
    stopCtl.stop();
    setState({ isReviewing: false, review: "", agentSteps: [], currentStep: null, currentMode: null, error: null, stopped: false, timings: EMPTY_TIMINGS });
  }, [stopCtl]);

  return { ...state, reviewFile, reviewPaste, reset, stop };
}
