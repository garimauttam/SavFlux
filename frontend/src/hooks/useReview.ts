/**
 * useReview.ts — Manages the code review agent's streaming state.
 *
 * This is structurally similar to useChat but with one key difference:
 * the stream contains TWO types of data interleaved:
 *   1. __STATUS__...text...{json}__STATUS_END__  → agent status updates
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
  const _consumeStream = useCallback(async (response: Response) => {
    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // ── ERROR terminal marker ───────────────────────────────────────────────
      // Emitted by the backend when an exception occurs mid-stream (e.g. rate limit).
      // We surface it as a user-visible error instead of silent truncation.
      if (buffer.includes("__ERROR__") && buffer.includes("__ERROR_END__")) {
        const eStart = buffer.indexOf("__ERROR__");
        const eEnd   = buffer.indexOf("__ERROR_END__");
        if (eStart !== -1 && eEnd !== -1) {
          const errMsg = buffer.slice(eStart + 9, eEnd).trim();
          setState((prev) => ({
            ...prev,
            isReviewing: false,
            currentStep: null,
            error: errMsg || "The review was interrupted by a server error.",
          }));
          return;
        }
      }

      // Process all complete STATUS markers in the buffer
      // A STATUS marker looks like: __STATUS__message{json}__STATUS_END__\n
      while (buffer.includes("__STATUS_END__")) {
        const start = buffer.indexOf("__STATUS__");
        const end = buffer.indexOf("__STATUS_END__");

        if (start === -1 || end === -1) break;

        const statusText = buffer.slice(start + 10, end); // strip __STATUS__
        buffer = buffer.slice(end + 14 + 1);               // strip __STATUS_END__\n

        // Extract the message (before the JSON) and the JSON metadata
        const jsonMatch = statusText.match(/(\{.*\})$/);
        const message = jsonMatch ? statusText.slice(0, statusText.lastIndexOf(jsonMatch[0])).trim() : statusText.trim();
        let meta: { step?: string; tool?: string; file?: string; mode?: string } = {};
        if (jsonMatch) {
          try { meta = JSON.parse(jsonMatch[1]); } catch {}
        }

        setState((prev) => ({
          ...prev,
          currentStep: message,
          currentMode: meta.mode ?? prev.currentMode,
          agentSteps: [...prev.agentSteps, { ...meta, message }].slice(-40),
        }));
      }

      // Whatever remains in the buffer that isn't a STATUS marker or partial marker prefix is review text
      if (!buffer.includes("__STATUS__")) {
        // Protect against partial marker splitting (e.g. buffer ends with "_" or "__STAT")
        const partialPrefixMatch = buffer.match(/_{1,2}(?:S(?:T(?:A(?:T(?:U(?:S)?)?)?)?)?)?$/);
        if (partialPrefixMatch) {
          const splitIndex = partialPrefixMatch.index ?? buffer.length;
          const text = buffer.slice(0, splitIndex);
          buffer = buffer.slice(splitIndex);
          if (text) {
            setState((prev) => ({
              ...prev,
              review: prev.review + text,
            }));
          }
        } else {
          const text = buffer;
          buffer = "";
          if (text) {
            setState((prev) => ({
              ...prev,
              review: prev.review + text,
            }));
          }
        }
      }
    }

    // Mark done
    setState((prev) => ({ ...prev, isReviewing: false, currentStep: null }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // setState is stable — no deps needed

  const reset = useCallback(() => {
    setState({ isReviewing: false, review: "", agentSteps: [], currentStep: null, currentMode: null, error: null });
  }, []);

  return { ...state, reviewFile, reviewPaste, reset };
}
