/**
 * useCodeWriter.ts — State and streaming logic for the code writing agent.
 *
 * Same protocol and same parser as the review and agent streams
 * (`lib/stream.ts`):
 *   __STATUS__{json}__STATUS_END__   → progress trace
 *   __ERROR__text__ERROR_END__       → a failure the panel must show
 *   everything else                   → generated code output
 */

import { useState, useCallback } from "react";
import { apiFetch } from "../api";
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
}

export function useCodeWriter() {
  const [state, setState] = useState<CodeWriterState>({
    isGenerating: false,
    output: "",
    steps: [],
    currentStep: null,
    error: null,
  });

  const generate = useCallback(async (
    prompt: string,
    language: string,
    fileName: string,
    contextSources: string[],
    mode: "generate" | "edit" | "tests" = "generate",
  ) => {
    setState({ isGenerating: true, output: "", steps: [], currentStep: "Starting...", error: null });

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
          setState((prev) => ({
            ...prev,
            currentStep: message,
            steps: meta.step ? [...prev.steps, { step: String(meta.step), message }] : prev.steps,
          }));
        }
      }

      const tail = flushTail(buffer, AGENT_TAGS);
      if (tail) setState((prev) => ({ ...prev, output: prev.output + tail }));
      setState((prev) => ({ ...prev, isGenerating: false, currentStep: null }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isGenerating: false,
        error: err instanceof Error ? err.message : "Generation failed.",
      }));
    }
  }, []);

  const reset = useCallback(() => {
    setState({ isGenerating: false, output: "", steps: [], currentStep: null, error: null });
  }, []);

  return { ...state, generate, reset };
}
