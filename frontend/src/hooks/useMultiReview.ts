/**
 * useMultiReview.ts — State and streaming logic for multi-file code review.
 *
 * The multi-review stream adds section delimiters on top of the STATUS protocol:
 *   __SECTION_START__filename__SECTION_END__   → start a new per-file card
 *   __STATUS__...{json}__STATUS_END__          → progress update (same as single review)
 *   everything else                            → review markdown for the current section
 *
 * We maintain a `sections` array so the UI can render each file as a collapsible card.
 */

import { useState, useCallback } from "react";
import { IndexedFile } from "../types";
import { apiFetch } from "../api";

export interface ReviewSection {
  id: string;
  fileName: string;
  content: string;
  status: "pending" | "scanning" | "reviewing" | "writing" | "complete" | "skipped" | "error";
  statusMessage?: string;
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
  });

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
    });

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
      ) => {
        setState((prev) => ({
          ...prev,
          sections: prev.sections.map((s) =>
            s.id === sectionId ? { ...s, status, statusMessage } : s
          ),
        }));
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // ── ERROR terminal marker ─────────────────────────────────────────────
        // Surfaced as an in-section error note so the rest of the multi-review
        // output remains visible (earlier sections are not lost).
        if (buffer.includes("__ERROR__") && buffer.includes("__ERROR_END__")) {
          const eStart = buffer.indexOf("__ERROR__");
          const eEnd   = buffer.indexOf("__ERROR_END__");
          if (eStart !== -1 && eEnd !== -1) {
            const errMsg = buffer.slice(eStart + 9, eEnd).trim();
            buffer = buffer.slice(eEnd + 13 + 1); // consume the marker
            const displayMsg = `\n\n> ⚠️ **Error:** ${errMsg || "Server error — response may be incomplete."}`;
            if (currentSectionId) {
              setState((prev) => ({
                ...prev,
                sections: prev.sections.map((s) =>
                  s.id === currentSectionId
                    ? { ...s, content: s.content + displayMsg }
                    : s
                ),
              }));
              updateSectionStatus(currentSectionId, "error", errMsg);
            }
          }
        }

        // Process all complete markers in the buffer
        let changed = true;
        while (changed) {
          changed = false;

          // ── Section start marker ───────────────────────────────────────────
          if (buffer.includes("__SECTION_START__") && buffer.includes("__SECTION_END__")) {
            const sStart = buffer.indexOf("__SECTION_START__");
            const sEnd = buffer.indexOf("__SECTION_END__");
            if (sStart !== -1 && sEnd !== -1 && sEnd > sStart) {
              // Flush any buffered text to the current section first
              const textBefore = buffer.slice(0, sStart);
              if (textBefore.trim() && currentSectionId) {
                appendToSection(currentSectionId, textBefore);
              }

              const sectionPayload = parseSectionPayload(buffer.slice(sStart + 17, sEnd)); // strip __SECTION_START__
              buffer = buffer.slice(sEnd + 15 + 1); // strip __SECTION_END__\n
              currentSectionId = sectionPayload.id;

              setState((prev) => ({
                ...prev,
                sections: prev.sections.some((section) => section.id === sectionPayload.id)
                  ? prev.sections
                  : [
                    ...prev.sections,
                    { id: sectionPayload.id, fileName: sectionPayload.fileName, content: "", status: "pending" },
                  ],
              }));
              changed = true;
              continue;
            }
          }

          // ── STATUS marker ──────────────────────────────────────────────────
          if (buffer.includes("__STATUS__") && buffer.includes("__STATUS_END__")) {
            const start = buffer.indexOf("__STATUS__");
            const end = buffer.indexOf("__STATUS_END__");
            if (start !== -1 && end !== -1) {
              // Flush text before this marker to current section
              const textBefore = buffer.slice(0, start);
              if (textBefore && currentSectionId) {
                appendToSection(currentSectionId, textBefore);
              }

              const statusText = buffer.slice(start + 10, end);
              buffer = buffer.slice(end + 14 + 1);

              const jsonMatch = statusText.match(/(\{.*\})$/);
              const message = jsonMatch
                ? statusText.slice(0, statusText.lastIndexOf(jsonMatch[0])).trim()
                : statusText.trim();
              let meta: { step?: string; tool?: string; file?: string; id?: string; index?: number; total?: number; mode?: string; llm?: number; static?: number; pct?: number } = {};
              if (jsonMatch) {
                try { meta = JSON.parse(jsonMatch[1]); } catch {}
              }
              const statusId = meta.id ?? currentSectionId;
              if (statusId) {
                updateSectionStatus(statusId, sectionStatusFromMeta(meta, message), message);
              }

              setState((prev) => ({
                ...prev,
                currentStep: message,
                currentFile: meta.file ?? prev.currentFile,
                totalFiles: meta.total ?? prev.totalFiles,
                currentMode: meta.mode ?? prev.currentMode,
                agentSteps: [...prev.agentSteps, { ...meta, message }].slice(-80),
                // Capture server-side coverage counts from the "coverage" step token
                ...(meta.step === "coverage" && {
                  serverLlmCount: meta.llm,
                  serverStaticCount: meta.static,
                  serverCoveragePct: meta.pct,
                }),
              }));
              changed = true;
            }
          }
        }

        // Flush plain text that's not a partial marker
        if (
          !buffer.includes("__SECTION_START__") &&
          !buffer.includes("__STATUS__") &&
          currentSectionId
        ) {
          const partialMatch = buffer.match(/_{1,2}(?:S(?:E(?:C(?:T(?:I(?:O(?:N)?)?)?)?)?|T(?:A(?:T(?:U(?:S)?)?)?)?)?)?$/);
          const splitIdx = partialMatch ? (partialMatch.index ?? buffer.length) : buffer.length;
          const text = buffer.slice(0, splitIdx);
          buffer = buffer.slice(splitIdx);
          if (text) {
            appendToSection(currentSectionId, text);
          }
        }
      }

      setState((prev) => ({
        ...prev,
        isReviewing: false,
        currentStep: null,
        sections: prev.sections.map((section) => {
          if (section.status === "complete" || section.status === "error") return section;
          if (section.content.trim()) return { ...section, status: "complete" };
          return {
            ...section,
            status: "skipped",
            statusMessage: "No review output was received for this file.",
          };
        }),
      }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        isReviewing: false,
        error: err instanceof Error ? err.message : "Review failed.",
      }));
    }
  }, []);

  const reset = useCallback(() => {
    setState({
      isReviewing: false,
      sections: [],
      agentSteps: [],
      currentStep: null,
      currentFile: null,
      totalFiles: 0,
      currentMode: null,
      error: null,
    });
  }, []);

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
  };
}
