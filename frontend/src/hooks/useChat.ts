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
import { CHAT_TAGS, decodeStatus, drainMarkers, flushTail } from "../lib/stream";

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

      // `lib/stream.ts` is the one scanner, and chat is the consumer that needed
      // it most. Its previous loop kept three separate copies of "hold back a
      // partial marker" and finished with a sweep whose last rule —
      // `_{0,2}(?:STATUS|SOURCES|DIAGNOSTIC)[^\n]*$` — stripped any line containing
      // the word "status", including one in the answer's own prose. Scanning for
      // markers as they complete makes that class of bug unreachable: answer text
      // is never a marker, so it never needs cleaning afterwards.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const scan = drainMarkers(buffer, CHAT_TAGS);
        buffer = scan.rest;

        for (const segment of scan.segments) {
          if (segment.kind === "text") {
            answerBuffer += segment.value;
            continue;
          }
          if (segment.kind === "sources") {
            try {
              const parsed = JSON.parse(segment.payload);
              if (Array.isArray(parsed)) sources = parsed as SourceFile[];
            } catch {
              // A malformed citations block costs the reader their footnote list,
              // not the answer. Deliberately swallowed, loudly commented.
            }
            continue;
          }
          if (segment.kind === "diagnostic") {
            const note = segment.payload.trim();
            if (note) generationSteps = [...generationSteps, `⚠️ ${note}`];
            continue;
          }
          if (segment.kind === "status") {
            const event = decodeStatus(segment.payload);
            const label = typeof event.message === "string" && event.message
              ? event.message
              : segment.payload.trim();
            if (label) generationSteps = [...generationSteps, label];
            continue;
          }
          if (segment.kind === "error") {
            const note = segment.payload.trim();
            // The answer stops here, so say so inside the message rather than
            // leaving a half-sentenced reply and no explanation.
            generationSteps = [...generationSteps, `⚠️ ${note || "The answer was interrupted."}`];
            continue;
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

      // What is still held back is prose — unless the server began a marker and
      // never finished it, which is truncated telemetry and must not be shown.
      const residual = flushTail(buffer, CHAT_TAGS);

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMessageId
            ? { ...m, content: (answerBuffer + residual).trimStart(), isStreaming: false, sources, generationSteps }
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
