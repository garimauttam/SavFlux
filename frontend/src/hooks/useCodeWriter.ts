/**
 * useCodeWriter.ts — State and streaming logic for the code writing agent.
 *
 * Same protocol and same parser as the review and agent streams
 * (`lib/stream.ts`):
 *   __STATUS__{json}__STATUS_END__   → progress trace
 *   __ERROR__text__ERROR_END__       → a failure the panel must show
 *   everything else                   → generated code output
 */

import { useCallback, useState } from "react";
import { apiFetch } from "../api";
import { isAbortError, useStreamStop } from "../lib/cancel";
import { AGENT_TAGS, decodeStatus, drainMarkers, flushTail } from "../lib/stream";

export interface WriterStep {
  step: string;
  message: string;
}

export interface CodeWriterState {
  isGenerating: boolean;
  output: string;           // streamed code + explanation
  steps: WriterStep[];      // progress trace
  currentStep: string | null;
  error: string | null;
  /**
   * The reader stopped the generation. Kept apart from `error` because the code that
   * did arrive is still real code — a stopped write is a partial answer, not a
   * failure, and an error banner would invite a re-run that costs the model again.
   */
  stopped: boolean;
}

export function useCodeWriter() {
  const [state, setState] = useState<CodeWriterState>({
    isGenerating: false,
    output: "",
    steps: [],
    currentStep: null,
    error: null,
    stopped: false,
  });
  const stopCtl = useStreamStop();

  const generate = useCallback(async (
    prompt: string,
    language: string,
    fileName: string,
    contextSources: string[],
    mode: "generate" | "edit" | "tests" = "generate",
  ) => {
    setState({ isGenerating: true, output: "", steps: [], currentStep: "Starting...", error: null, stopped: false });
    const controller = stopCtl.start();

    try {
      const response = await apiFetch("/api/v1/write/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          language,
          file_name: fileName,
          context_sources: contextSources,
          mode,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({ detail: `Server error ${response.status}` }));
        throw new Error(err.detail ?? `Server error ${response.status}`);
      }

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // `lib/stream.ts` — the same scanner the agent panel and the review hooks
      // use, because the writer's markers are the same markers. The hand-rolled
      // version this replaces held back partial tags with a 20-character window
      // and a list of four spellings of "__STATUS_END__"; the scanner holds back
      // any tail that could still grow into any protocol tag, which is the rule
      // those four strings were approximating.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const scan = drainMarkers(buffer, AGENT_TAGS);
        buffer = scan.rest;

        for (const segment of scan.segments) {
          if (segment.kind === "text") {
            setState((prev) => ({ ...prev, output: prev.output + segment.value }));
            continue;
          }
          if (segment.kind === "error") {
            setState((prev) => ({
              ...prev,
              isGenerating: false,
              currentStep: null,
              error: segment.payload.trim() || "Generation was interrupted by a server error.",
            }));
            return;
          }
          if (segment.kind !== "status") continue;

          const meta = decodeStatus(segment.payload);
          const message = typeof meta.message === "string" ? meta.message : "";
          // The server noticed the reader leaving and said where it stopped. Its
          // wording is better than the client's guess, so it is what gets shown.
          if (meta.step === "cancelled") {
            setState((prev) => ({
              ...prev,
              isGenerating: false,
              stopped: true,
              error: null,
              currentStep: message || "Stopped.",
            }));
          }
          setState((prev) => ({
            ...prev,
            currentStep: message,
            steps: meta.step ? [...prev.steps, { step: String(meta.step), message }] : prev.steps,
          }));
        }
      }

      const tail = flushTail(buffer, AGENT_TAGS);
      if (tail) setState((prev) => ({ ...prev, output: prev.output + tail }));
      // As in the review hooks: a run the reader stopped keeps the line that says so,
      // instead of being silenced into looking finished.
      setState((prev) => ({
        ...prev,
        isGenerating: false,
        currentStep: prev.stopped ? prev.currentStep : null,
      }));
      stopCtl.finish();
    } catch (err) {
      // An aborted fetch is the Stop button, and Stop has already drawn its state.
      if (isAbortError(err) || stopCtl.stopped()) {
        setState((prev) => ({ ...prev, isGenerating: false, stopped: true, error: null }));
        return;
      }
      setState((prev) => ({
        ...prev,
        isGenerating: false,
        error: err instanceof Error ? err.message : "Generation failed.",
      }));
    }
  }, [stopCtl]);

  const stop = useCallback(() => {
    stopCtl.stop();
    setState((prev) => ({
      ...prev,
      isGenerating: false,
      stopped: true,
      error: null,
      currentStep: "Stopped — the code above is what was generated before it stopped.",
    }));
  }, [stopCtl]);

  const reset = useCallback(() => {
    stopCtl.stop();
    setState({ isGenerating: false, output: "", steps: [], currentStep: null, error: null, stopped: false });
  }, [stopCtl]);

  return { ...state, generate, reset, stop };
}
