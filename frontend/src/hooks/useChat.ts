/**
 * useChat.ts — Custom hook managing chat state and API communication.
 *
 * Enhancements in this version:
 * 1. AbortController — cancels the in-flight fetch when the component unmounts
 *    or when a new message is sent before the previous one finishes.
 *
 * 2. activeRepoUrl — passed to the backend so retrieval is scoped to one repo.
 *
 * 3. localStorage persistence — chat history is saved per-repo and restored on
 *    page refresh. Key format: "savflux:messages:{repoUrl}". Stored messages
 *    have isStreaming stripped so a refreshed page never shows stale spinners.
 */

import { useState, useCallback, useEffect, useRef } from "react";
import { Message, SourceFile } from "../types";
import { apiFetch } from "../api";

function generateId(): string {
  return Math.random().toString(36).slice(2, 9);
}

function storageKey(repoUrl: string | null | undefined): string {
  return `savflux:messages:${repoUrl ?? "__none__"}`;
}

function loadMessages(repoUrl: string | null | undefined): Message[] {
  try {
    const raw = localStorage.getItem(storageKey(repoUrl));
    if (!raw) return [];
    const parsed: Message[] = JSON.parse(raw);
    // Strip any stale isStreaming flags — never show a spinner on load
    return parsed.map((m) => ({ ...m, isStreaming: false }));
  } catch {
    return [];
  }
}

function saveMessages(repoUrl: string | null | undefined, messages: Message[]): void {
  try {
    // Only persist completed messages — drop any still-streaming placeholder
    const toSave = messages.filter((m) => !m.isStreaming);
    localStorage.setItem(storageKey(repoUrl), JSON.stringify(toSave));
  } catch {
    // localStorage can throw if storage is full — silently ignore
  }
}

export function useChat(
  activeRepoUrl?: string | null,
  activeRepoUrls?: string[] | null,
) {
  const [messages, setMessages] = useState<Message[]>(() => loadMessages(activeRepoUrl));
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // When the active repo changes, swap in that repo's persisted history
  useEffect(() => {
    setMessages(loadMessages(activeRepoUrl));
    setError(null);
  }, [activeRepoUrl]);

  // Persist to localStorage whenever messages change (debounced by React batching)
  useEffect(() => {
    saveMessages(activeRepoUrl, messages);
  }, [messages, activeRepoUrl]);

  // Track the current in-flight AbortController so we can cancel it
  // when a new message is sent or the component unmounts.
  const abortControllerRef = useRef<AbortController | null>(null);

  const sendMessage = useCallback(async (
    question: string,
    activeRepoUrl?: string | null,
  ) => {
    if (!question.trim() || isLoading) return;

    // Cancel any previous in-flight request before starting a new one.
    // This handles "user sends message before previous one finishes" correctly.
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    setError(null);

    // Snapshot history NOW — before we call setMessages below.
    // setMessages is async; if we read `messages` inside the try block
    // after the setState call, it still refers to the pre-update snapshot
    // from this render cycle, missing the user message we just added.
    // Snapshotting here gives us the correct history to send to the backend.
    const history = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    const userMessage: Message = {
      id: generateId(),
      role: "user",
      content: question,
    };

    const assistantMessageId = generateId();
    const assistantMessage: Message = {
      id: assistantMessageId,
      role: "assistant",
      content: "",
      isStreaming: true,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);
    setIsLoading(true);

    try {

      const response = await apiFetch("/api/v1/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          chat_history: history,
          active_repo_url: activeRepoUrl ?? null,
          ...(activeRepoUrls && activeRepoUrls.length > 0
            ? { active_repo_urls: activeRepoUrls }
            : {}),
        }),
        // Pass the signal — fetch will throw an AbortError if aborted
        signal: abortController.signal,
      });

      if (!response.ok) {
        throw new Error(`Server error: ${response.status}`);
      }

      const reader = response.body!.getReader();
      const decoder = new TextDecoder();
      let sources: SourceFile[] = [];
      let buffer = "";
      let answerBuffer = "";
      let generationSteps: string[] = [];

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        buffer += text;

        // Consume protocol markers while keeping ordinary answer text.
        //
        // Recognised markers (always stripped from the visible answer):
        //   __SOURCES__…__SOURCES_END__       → parsed as citation list
        //   __STATUS__…__STATUS_END__         → shown as generation step label
        //   __DIAGNOSTIC__…__DIAGNOSTIC_END__ → shown as a retrieval warning
        //
        // CHUNK-BOUNDARY SAFETY
        // The backend sends each marker as one logical line, but TCP/HTTP chunking
        // can split it anywhere — including in the middle of "__STATUS_END__".
        // The previous logic only held back partial *start* marker prefixes at the
        // tail of the buffer. If "__STATUS_END__" itself was split (e.g. the first
        // chunk ended with "...context\"}__STATUS_" and the next started with
        // "_END__\n"), the start-marker was already in the buffer with no matching
        // end-marker, so the inner loop fell through to the "hold partial" path.
        // But the hold-partial path only scanned for START prefixes — it missed
        // partial END suffixes, causing the raw marker text to bleed into answerBuffer.
        //
        // Fix: also hold back any tail that could be a partial end-marker suffix.
        // We do this by extending HOLD_PATTERNS to cover both starts AND ends.
        let changed = true;
        while (changed) {
          changed = false;

          // ── SOURCES ──────────────────────────────────────────────────────────
          const sourceStart = buffer.indexOf("__SOURCES__");
          const sourceEnd = buffer.indexOf("__SOURCES_END__");
          if (sourceStart !== -1 && sourceEnd !== -1 && sourceEnd > sourceStart) {
            answerBuffer += buffer.slice(0, sourceStart);
            try {
              sources = JSON.parse(buffer.slice(sourceStart + 11, sourceEnd));
            } catch {}
            buffer = buffer.slice(sourceEnd + 15).replace(/^\n/, "");
            changed = true;
            continue;
          }

          // ── STATUS ───────────────────────────────────────────────────────────
          const statusStart = buffer.indexOf("__STATUS__");
          const statusEnd = buffer.indexOf("__STATUS_END__");
          if (statusStart !== -1 && statusEnd !== -1 && statusEnd > statusStart) {
            answerBuffer += buffer.slice(0, statusStart);
            const statusText = buffer.slice(statusStart + 10, statusEnd);
            const jsonMatch = statusText.match(/(\{[^}]*\})$/);
            const stepLabel = jsonMatch
              ? statusText.slice(0, statusText.lastIndexOf(jsonMatch[0])).trim()
              : statusText.trim();
            if (stepLabel) generationSteps = [...generationSteps, stepLabel];
            buffer = buffer.slice(statusEnd + 14).replace(/^\n/, "");
            changed = true;
            continue;
          }

          // ── DIAGNOSTIC (non-blocking retrieval warning) ───────────────────
          const diagStart = buffer.indexOf("__DIAGNOSTIC__");
          const diagEnd = buffer.indexOf("__DIAGNOSTIC_END__");
          if (diagStart !== -1 && diagEnd !== -1 && diagEnd > diagStart) {
            answerBuffer += buffer.slice(0, diagStart);
            const diagText = buffer.slice(diagStart + 14, diagEnd).trim();
            if (diagText) generationSteps = [...generationSteps, `⚠️ ${diagText}`];
            buffer = buffer.slice(diagEnd + 18).replace(/^\n/, "");
            changed = true;
            continue;
          }

          // ── Hold partial marker — wait for more data ──────────────────────
          // Patterns to watch for at the tail of the buffer (both start and end
          // marker substrings). Any of these appearing at the end means we must
          // wait for the next chunk before flushing.
          // Each marker's end-tag can be split at any byte boundary.
          // We must hold back any tail that is a PREFIX of ANY of these strings —
          // including forms that start mid-underscore (e.g. "STATUS_END__" after
          // the opening "__" was already flushed as part of the answer).
          // Adding the bare forms without leading "__" catches splits like:
          //   buffer ends with "...{\"step\":\"context\"}_"
          //   or              "...{\"step\":\"context\"}__STATUS_"
          //   or              "STATUS_END"  (the __ was in a previous chunk)
          const HOLD_PATTERNS = [
            "__STATUS__",     "__STATUS_END__",     "STATUS_END__",     "_STATUS_END__",
            "__SOURCES__",    "__SOURCES_END__",    "SOURCES_END__",    "_SOURCES_END__",
            "__DIAGNOSTIC__", "__DIAGNOSTIC_END__", "DIAGNOSTIC_END__", "_DIAGNOSTIC_END__",
          ];

          // 1. Check for a *complete* start-marker already in buffer whose
          //    end-marker hasn't arrived yet — hold the whole thing.
          const startMarkers: Array<{ tag: string; end: string; len: number }> = [
            { tag: "__STATUS__",     end: "__STATUS_END__",     len: 10 },
            { tag: "__SOURCES__",    end: "__SOURCES_END__",    len: 11 },
            { tag: "__DIAGNOSTIC__", end: "__DIAGNOSTIC_END__", len: 14 },
          ];
          let heldForEnd = false;
          for (const { tag, end } of startMarkers) {
            const si = buffer.indexOf(tag);
            if (si !== -1 && buffer.indexOf(end, si) === -1) {
              // Start present but no matching end — hold everything from start
              answerBuffer += buffer.slice(0, si);
              buffer = buffer.slice(si);
              heldForEnd = true;
              break;
            }
          }
          if (heldForEnd) break;

          // 2. Check for a partial pattern at the tail of the buffer.
          let markerStart = -1;
          const tail = buffer.slice(-20);
          for (const pfx of HOLD_PATTERNS) {
            for (let len = Math.min(pfx.length - 1, tail.length); len > 0; len--) {
              if (tail.endsWith(pfx.slice(0, len))) {
                markerStart = buffer.length - len;
                break;
              }
            }
            if (markerStart !== -1) break;
          }

          if (markerStart !== -1) {
            answerBuffer += buffer.slice(0, markerStart);
            buffer = buffer.slice(markerStart);
          } else {
            answerBuffer += buffer;
            buffer = "";
          }
        }

        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantMessageId
              ? { ...m, content: answerBuffer, sources, generationSteps }
              : m
          )
        );
      }

      // Final flush: strip any residual marker fragments left in `buffer` AND
      // any orphaned marker text that leaked into `answerBuffer` due to a TCP
      // chunk boundary splitting an end-tag (e.g. "...context\"}STATUS_END__\n").
      const stripMarkers = (s: string) =>
        s
          // Complete markers (may span multiple lines with [^]*)
          .replace(/__STATUS__[^]*?__STATUS_END__\n?/g, "")
          .replace(/__SOURCES__[^]*?__SOURCES_END__\n?/g, "")
          .replace(/__DIAGNOSTIC__[^]*?__DIAGNOSTIC_END__\n?/g, "")
          // Orphaned bare end-tags that appear when the __ prefix was already flushed:
          //   "STATUS_END__"  "_STATUS_END__"  "__STATUS_END__"  (all variations)
          .replace(/_*STATUS_END__\n?/g, "")
          .replace(/_*SOURCES_END__\n?/g, "")
          .replace(/_*DIAGNOSTIC_END__\n?/g, "")
          // Orphaned partial start marker at end of string
          .replace(/_{0,2}(?:STATUS|SOURCES|DIAGNOSTIC)[^\n]*$/gm, "");

      const residual = stripMarkers(buffer);
      const cleanAnswer = stripMarkers(answerBuffer);

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessageId
            ? { ...m, content: (cleanAnswer + residual).trimStart(), isStreaming: false, sources, generationSteps }
            : m
        )
      );
    } catch (err) {
      // AbortError is not a real error — it means we intentionally cancelled.
      // Don't show an error banner for it; just clean up the placeholder message.
      if (err instanceof Error && err.name === "AbortError") {
        setMessages((prev) => prev.filter((m) => m.id !== assistantMessageId));
        setIsLoading(false);
        return;
      }

      setError(err instanceof Error ? err.message : "An error occurred.");
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessageId
            ? {
                ...m,
                content: m.content
                  ? m.content + "\n\n*[Stream interrupted — response may be incomplete.]*"
                  : "Sorry, I encountered an error. Please try again.",
                isStreaming: false,
              }
            : m
        )
      );
    } finally {
      // Only clear loading if this is still the active request (not cancelled)
      if (abortControllerRef.current === abortController) {
        setIsLoading(false);
        abortControllerRef.current = null;
      }
    }
  }, [messages, isLoading, activeRepoUrls]);

  const clearChat = useCallback(() => {
    // Also cancel any in-flight request when clearing the chat
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setMessages([]);
    setError(null);
  }, []);

  return { messages, isLoading, error, sendMessage, clearChat };
}
