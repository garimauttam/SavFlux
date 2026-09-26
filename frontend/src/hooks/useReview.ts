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
import { decodeStatus, drainMarkers, flushTail, REVIEW_TAGS } from "../lib/stream";

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
}

export function useReview() {
  const [state, setState] = useState<ReviewState>({
    isReviewing: false,
    review: "",
    agentSteps: [],
    currentStep: null,
    currentMode: null,
    error: null,
  });

  const reviewFile = useCallback(async (file: IndexedFile) => {
    setState({ isReviewing: true, review: "", agentSteps: [], currentStep: "Starting review...", currentMode: null, error: null });

    try {
      const response = await apiFetch("/api/v1/review/file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          file_path: file.source,
          file_name: file.file_name,
          language: file.language,
        }),
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail ?? `Server error ${response.status}`);
      }

      await _consumeStream(response);
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, []);

  const reviewPaste = useCallback(async (code: string, language: string, fileName: string) => {
    setState({ isReviewing: true, review: "", agentSteps: [], currentStep: "Starting review...", currentMode: null, error: null });

    try {
      const response = await apiFetch("/api/v1/review/paste", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, language, file_name: fileName }),
      });

      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail ?? `Server error ${response.status}`);
      }

      await _consumeStream(response);
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, []);

  /**
   * Shared stream consumer — parses STATUS markers out of the stream
   * and routes them to agentSteps; everything else goes to `review`.
   *
   * Wrapped in useCallback so it has a stable reference and can safely
   * be listed as a dependency if reviewFile/reviewPaste ever need it.
   */
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
        setState((prev) => ({
          ...prev,
          currentStep: message || null,
          currentMode: typeof meta.mode === "string" ? meta.mode : prev.currentMode,
          agentSteps: [...prev.agentSteps, { ...meta, message }].slice(-40),
        }));
      }
    }

    const tail = flushTail(buffer, REVIEW_TAGS);
    if (tail) setState((prev) => ({ ...prev, review: prev.review + tail }));
    setState((prev) => ({ ...prev, isReviewing: false, currentStep: null }));
  }, []);

  const reset = useCallback(() => {
    setState({ isReviewing: false, review: "", agentSteps: [], currentStep: null, currentMode: null, error: null });
  }, []);

  return { ...state, reviewFile, reviewPaste, reset };
}
