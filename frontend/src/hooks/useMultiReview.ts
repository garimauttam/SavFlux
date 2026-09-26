/**
 * useMultiReview.ts — State and streaming logic for multi-file code review.
 *
 * The multi-review stream adds section delimiters on top of the shared STATUS
 * protocol (`lib/stream.ts`, which is also what the agent and the single review
 * use):
 *   __SECTION_START__{"id":…,"file_name":…}__SECTION_END__  → start a per-file card
 *   __STATUS__{json}__STATUS_END__                          → progress + timings
 *   everything else                                         → review markdown
 *
 * We maintain a `sections` array so the UI can render each file as a collapsible card.
 */

import { useState, useCallback } from "react";
import { IndexedFile } from "../types";
import { apiFetch } from "../api";
import { isAbortError, useStreamStop } from "../lib/cancel";
import { decodeStatus, drainMarkers, flushTail, REVIEW_TAGS } from "../lib/stream";
import { applyTiming, EMPTY_TIMINGS, type ReviewTimings } from "../lib/timings";

export interface ReviewSection {
  id: string;
  fileName: string;
  content: string;
  status: "pending" | "scanning" | "reviewing" | "writing" | "complete" | "skipped" | "error";
  statusMessage?: string;
  // Server-reported provenance for this file (from the review planner):
  //   tier   — how the file was reviewed: "static" | "fast" | "full"
  //   cached — the review was reused from the content-hash cache, so no model
  //            call was made for it in this run.
  // Surfaced because a review that costs nothing should say why, and a review
  // that never reached a model should not look like one that did.
  tier?: string;
  cached?: boolean;
}

/**
 * A `__STATUS__` payload from the review stream.
 *
 * Every field is optional because the backend emits several kinds of token
 * (plan, per-file progress, coverage, timing) and they carry different payloads.
 * Unknown fields are ignored rather than rejected, so a newer backend can add
 * telemetry without breaking an older client.
 */
export interface StatusMeta {
  step?: string;
  tool?: string;
  file?: string;
  id?: string;
  index?: number;
  total?: number;
  mode?: string;
  // coverage token
  llm?: number;
  static?: number;
  pct?: number;
  cache_hits?: number;
  planned_static?: number;
  fallback_static?: number;
  // per-file token
  cached?: boolean;
  // planned token
  static_only?: number;
  batched_files?: number;
  batch_count?: number;
  model_calls?: number;
  // per-file token
  tier?: string;
  batch?: string;
  [key: string]: unknown;
}

export interface AgentStep {
  tool?: string;
  message: string;
  step?: string;
  file?: string;
  id?: string;
  index?: number;
  total?: number;
  mode?: string;
}

export interface MultiReviewState {
  isReviewing: boolean;
  sections: ReviewSection[];    // one per file + optional summary
  agentSteps: AgentStep[];
  currentStep: string | null;
  currentFile: string | null;
  totalFiles: number;
  currentMode: string | null;
  error: string | null;
  // Server-reported coverage (authoritative — from backend routing, not text scanning)
  serverLlmCount?: number;     // exact LLM-reviewed count as reported by the backend
  serverStaticCount?: number;  // exact static-only count as reported by the backend
  serverCoveragePct?: number;  // exact LLM coverage % as reported by the backend
  // Run plan, reported by the backend before any review happens: how many model
  // calls this review will make, and why the rest of the files did not need one.
  serverPlannedStatic?: number;   // files the parser fully determines
  serverBatchedFiles?: number;    // files sharing a batched model call
  serverBatchCount?: number;      // how many batched calls that is
  serverModelCalls?: number;      // total model calls planned for this run
  serverCacheHits?: number;       // files answered from the content-hash cache
  /**
   * The reader stopped this run. `error` stays null: files that were never reviewed
   * are a fact about a cancelled run, not a failure to retry, and the sections that
   * *were* produced stay readable.
   */
  stopped: boolean;
  /** Per-stage cost, from the `planned` and `timing` markers. */
  timings: ReviewTimings;
  // Derived accuracy fields (not part of useState, computed from sections)
  reviewAccuracy?: number;      // 0–100: % of file sections with real LLM review
  llmReviewedCount?: number;    // absolute count of LLM-reviewed files
  totalFileCount?: number;      // authoritative total (excludes summary section)
}

export function useMultiReview() {
  const [state, setState] = useState<MultiReviewState>({
    isReviewing: false,
    sections: [],
    agentSteps: [],
    currentStep: null,
    currentFile: null,
    totalFiles: 0,
    currentMode: null,
    error: null,
    stopped: false,
    timings: EMPTY_TIMINGS,
  });
  const stopCtl = useStreamStop();

  /**
   * Everything still unreviewed becomes `skipped`, with the reason the caller gives.
   *
   * A section left at `pending` after a stop reads as "the queue is still working on
   * it" forever. Marking it is the difference between a stopped run whose shape a
   * reader can see and one that looks like it is still coming.
   */
  const markSectionsSkipped = useCallback((message: string) => {
    setState((prev) => ({
      ...prev,
      isReviewing: false,
      stopped: true,
      error: null,
      currentStep: message,
      sections: prev.sections.map((section) =>
        section.status === "complete" || section.status === "error" || section.content.trim()
          ? section
          : { ...section, status: "skipped", statusMessage: message },
      ),
    }));
  }, []);

  const stop = useCallback(() => {
    stopCtl.stop();
    markSectionsSkipped("Stopped — the remaining files were not reviewed.");
  }, [markSectionsSkipped, stopCtl]);

  const reviewFiles = useCallback(async (files: IndexedFile[]) => {
    setState({
      isReviewing: true,
      sections: files.map((file) => ({
        id: file.source,
        fileName: file.file_name,
        content: "",
        status: "pending",
        statusMessage: "Waiting in review queue...",
      })),
      agentSteps: [],
      currentStep: "Starting multi-file review...",
      currentFile: null,
      totalFiles: files.length,
      currentMode: null,
      error: null,
      stopped: false,
      timings: EMPTY_TIMINGS,
    });
    const controller = stopCtl.start();

    try {
      const response = await apiFetch("/api/v1/review/multi", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          files: files.map((f) => ({
            file_path: f.source,
            file_name: f.file_name,
            language: f.language,
          })),
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        const detail = Array.isArray(err.detail)
          ? err.detail.map((item: { msg?: string }) => item.msg ?? "Validation error").join("; ")
          : err.detail;
        throw new Error(detail ?? `Server error ${response.status}`);
      }

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let currentSectionId = "";

      const parseSectionPayload = (raw: string): { id: string; fileName: string } => {
        try {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === "object") {
            const fileName = typeof parsed.file_name === "string" ? parsed.file_name : raw;
            const id = typeof parsed.id === "string" ? parsed.id : fileName;
            return { id, fileName };
          }
        } catch {}
        return { id: raw, fileName: raw };
      };

      const appendToSection = (sectionId: string, text: string) => {
        setState((prev) => ({
          ...prev,
          sections: prev.sections.map((s) =>
            s.id === sectionId ? { ...s, content: s.content + text } : s
          ),
        }));
      };

      const sectionStatusFromMeta = (
        meta: { step?: string; tool?: string; mode?: string },
        message: string,
      ): ReviewSection["status"] => {
        if (meta.step === "complete" || meta.step === "summary_complete") return "complete";
        if (meta.step === "writing") return "writing";
        if (meta.step === "summary") return "writing";
        if (meta.tool || message.toLowerCase().includes("reviewing")) return "reviewing";
        if (message.toLowerCase().includes("scanning")) return "scanning";
        return "reviewing";
      };

      const updateSectionStatus = (
        sectionId: string,
        status: ReviewSection["status"],
        statusMessage?: string,
        provenance?: { tier?: string; cached?: boolean },
      ) => {
        setState((prev) => ({
          ...prev,
          sections: prev.sections.map((s) =>
            s.id === sectionId
              ? {
                  ...s,
                  status,
                  statusMessage,
                  // Provenance only ever accumulates: a later token without a
                  // tier must not erase the tier an earlier token reported.
                  tier: provenance?.tier ?? s.tier,
                  cached: provenance?.cached ?? s.cached,
                }
              : s
          ),
        }));
      };

      // One scanner for the whole protocol: `__SECTION_START__` opens a card,
      // `__STATUS__` moves it, prose appends to it — in stream order, which is what
      // makes prose arriving *before* a section header land on the previous file
      // rather than the next one. The hand-rolled version kept three separate
      // copies of that ordering rule and an index arithmetic (`end + 14 + 1`) for
      // each marker's length, which is the kind of constant that silently misreads
      // the moment a marker is added.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const scan = drainMarkers(buffer, REVIEW_TAGS);
        buffer = scan.rest;

        for (const segment of scan.segments) {
          if (segment.kind === "text") {
            if (currentSectionId) appendToSection(currentSectionId, segment.value);
            continue;
          }

          if (segment.kind === "section") {
            const section = parseSectionPayload(segment.payload);
            currentSectionId = section.id;
            setState((prev) => ({
              ...prev,
              sections: prev.sections.some((item) => item.id === section.id)
                ? prev.sections
                : [...prev.sections, { id: section.id, fileName: section.fileName, content: "", status: "pending" }],
            }));
            continue;
          }

          // Surfaced as an in-section note so the rest of the multi-review output
          // stays visible — one file's failure is not a reason to lose the others.
          if (segment.kind === "error") {
            const errMsg = segment.payload.trim();
            const displayMsg = `\n\n> ⚠️ **Error:** ${errMsg || "Server error — response may be incomplete."}`;
            if (currentSectionId) {
              appendToSection(currentSectionId, displayMsg);
              updateSectionStatus(currentSectionId, "error", errMsg);
            }
            setState((prev) => ({ ...prev, error: errMsg || "Server error — response may be incomplete." }));
            continue;
          }

          if (segment.kind !== "status") continue;

          const meta: StatusMeta = decodeStatus(segment.payload);
          const message = typeof meta.message === "string" ? meta.message : "";
          const statusId = meta.id ?? currentSectionId;
          if (statusId) {
            updateSectionStatus(statusId, sectionStatusFromMeta(meta, message), message, {
              tier: typeof meta.tier === "string" ? meta.tier : undefined,
              cached: meta.cached === true ? true : undefined,
            });
          }

          if (meta.step === "cancelled") {
            // The server's own account of where it stopped, for a departure it
            // noticed before the client said anything.
            markSectionsSkipped(message || "Stopped — the remaining files were not reviewed.");
          }

          setState((prev) => ({
            ...prev,
            currentStep: message,
            currentFile: meta.file ?? prev.currentFile,
            totalFiles: meta.total ?? prev.totalFiles,
            currentMode: meta.mode ?? prev.currentMode,
            agentSteps: [...prev.agentSteps, { ...meta, message }].slice(-80),
            timings: applyTiming(prev.timings, meta),
            // Server-side coverage, from the `coverage` token — a count of what a
            // model actually saw, not what the text looks like it saw…
            ...(meta.step === "coverage" && {
              serverLlmCount: meta.llm,
              serverStaticCount: meta.static,
              serverCoveragePct: meta.pct,
              serverCacheHits: meta.cache_hits,
            }),
            // …and the plan, from the `planned` token, which arrives before any
            // review so the UI can explain the shape of the run up front.
            ...(meta.step === "planned" && {
              serverPlannedStatic: meta.static_only,
              serverBatchedFiles: meta.batched_files,
              serverBatchCount: meta.batch_count,
              serverModelCalls: meta.model_calls,
            }),
            ...(meta.step === "cancelled" && { stopped: true }),
          }));
        }
      }

      const tail = flushTail(buffer, REVIEW_TAGS);
      if (tail && currentSectionId) appendToSection(currentSectionId, tail);

      setState((prev) => ({
        ...prev,
        isReviewing: false,
        // See `useReview`: a stopped run keeps the line that explains where it
        // stopped, instead of being silenced into looking finished.
        currentStep: prev.stopped ? prev.currentStep : null,
        sections: prev.sections.map((section) => {
          // `skipped` is terminal here, and it already carries a reason — the one the
          // stop or the server gave is better than this fallback, which only knows no
          // prose arrived.
          if (
            section.status === "complete"
            || section.status === "error"
            || section.status === "skipped"
          ) return section;
          if (section.content.trim()) return { ...section, status: "complete" };
          return {
            ...section,
            status: "skipped",
            statusMessage: "No review output was received for this file.",
          };
        }),
      }));
      stopCtl.finish();
    } catch (err) {
      // The Stop button aborts the fetch, which surfaces here as an AbortError. It is
      // not a review failure, and the sections have already been marked.
      if (isAbortError(err) || stopCtl.stopped()) return;
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, [stopCtl]);

  const reset = useCallback(() => {
    stopCtl.stop();
    setState({
      isReviewing: false,
      sections: [],
      agentSteps: [],
      currentStep: null,
      currentFile: null,
      totalFiles: 0,
      currentMode: null,
      error: null,
      stopped: false,
      timings: EMPTY_TIMINGS,
    });
  }, [stopCtl]);

  // Derive completedFiles and totalFileCount from sections — always accurate regardless of
  // React batching, and always excludes the __repo_summary__ section.
  // A section is "done" once it has a terminal status (complete, error, or skipped).
  const fileSections = state.sections.filter((s) => s.id !== "__repo_summary__");

  // Use fileSections.length as the authoritative total — avoids relying on state.totalFiles
  // which could lag behind or include the summary section, causing counters like 62/61.
  const totalFileCount = fileSections.length || state.totalFiles;

  const completedFiles = fileSections.filter(
    (s) => s.status === "complete" || s.status === "error" || s.status === "skipped"
  ).length;

  // Review coverage: how many file sections received a real LLM review vs. deterministic-only.
  // Deterministic (static analysis) sections contain the footer marker injected by _static_triage().
  // LLM-reviewed sections contain structured headings like "## Security" or "## Performance" but
  // NOT the deterministic-only footer.
  const isDeterministicOnly = (content: string) =>
    content.includes("deterministic static analysis") ||
    content.includes("Deterministic Score") ||
    // Static triage footer — matches the string injected by _static_triage() in multi_review_agent.py
    content.includes("Static analysis (outside LLM review budget");

  const llmReviewedCount = fileSections.filter(
    (s) =>
      (s.status === "complete" || s.status === "error") &&
      s.content.length > 0 &&
      !isDeterministicOnly(s.content)
  ).length;

  // reviewCoverage: 0–100% of file sections that got a real LLM review.
  const reviewCoverage =
    totalFileCount > 0
      ? Math.round((llmReviewedCount / totalFileCount) * 100)
      : 0;

  // Prefer server-reported counts (from routing, authoritative) over client-derived counts
  // (from content text-scanning, which is less reliable).
  const accuracyPct   = state.serverCoveragePct ?? reviewCoverage;
  const llmCount      = state.serverLlmCount    ?? llmReviewedCount;

  return {
    ...state,
    completedFiles,
    totalFileCount,
    reviewAccuracy: accuracyPct,
    llmReviewedCount: llmCount,
    reviewFiles,
    reset,
    stop,
  };
}
