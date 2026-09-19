/**
 * ChatWindow.tsx — Main chat interface.
 *
 * Enhancements:
 * 1. Accepts `activeRepoUrl` prop — passed to sendMessage so retrieval is scoped.
 * 2. Accepts `hasIndexedFiles` prop — hides suggestion chips until a repo is indexed,
 *    preventing the confusing "no results" response when nothing is indexed.
 * 3. Shows an active-repo banner in the header so the user always knows
 *    which repo the chat is scoped to.
 */

import React, { useEffect, useRef, useState } from "react";
import { apiFetch } from "../api";
import { Send, Trash2, Loader2, Database } from "lucide-react";
import { MessageBubble } from "./MessageBubble";
import { ArchitectureDiagram } from "./ArchitectureDiagram";
import { VoiceControls } from "./VoiceControls";
import { useChat } from "../hooks/useChat";

interface ChatWindowProps {
  activeRepoUrl: string | null;
  hasIndexedFiles: boolean;
  activeRepoUrls?: string[] | null;  // multi-repo cross-search
}

export function ChatWindow({ activeRepoUrl, hasIndexedFiles, activeRepoUrls }: ChatWindowProps) {
  const { messages, isLoading, error, sendMessage, clearChat } = useChat(activeRepoUrl, activeRepoUrls);
  const [input, setInput] = useState("");
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashCmds, setSlashCmds] = useState<{command:string; description:string}[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // P2 Slash Commands — autocomplete when typing / at start
  useEffect(() => {
    const slash = input.trim().startsWith("/") ? input.trim().split(/\s+/)[0].slice(1) : "";
    const shouldOpen = input.trim().startsWith("/");
    if (!shouldOpen) { setSlashOpen(false); return; }
    let cancelled = false;
    (async () => {
      try {
        const params = new URLSearchParams();
        if (slash) params.set("q", slash);
        params.set("limit", "8");
        const res = await apiFetch(`/api/v1/slash/commands?${params.toString()}`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const cmds = (data.commands || []).map((c:any)=>({command:c.command, description:c.description}));
        setSlashCmds(cmds);
        setSlashOpen(cmds.length>0);
      } catch { if (!cancelled) setSlashOpen(false); }
    })();
    return () => { cancelled = true; };
  }, [input]);

  // P2 Prompt Library — fill input when prompt used from library/history/palette
  useEffect(() => {
    const handler = (e: Event) => {
      const text = (e as CustomEvent).detail as string;
      if (text) setInput(text);
    };
    window.addEventListener("savflux:use-prompt" as any, handler);
    return () => window.removeEventListener("savflux:use-prompt" as any, handler);
  }, []);

  const handleSend = () => {
    if (!input.trim()) return;
    sendMessage(input, activeRepoUrl);
    setInput("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // ↑ to load last history when input empty (bonus UX, still $0)
  const handleKeyDownWithHistory = (e: React.KeyboardEvent) => {
    handleKeyDown(e);
    if (e.key === "ArrowUp" && !input.trim()) {
      e.preventDefault();
      (async () => {
        try {
          const { apiFetch } = await import("../api");
          const r = await apiFetch("/api/v1/prompts/history?limit=1");
          if (r.ok) {
            const j = await r.json();
            const last = j.history?.[0]?.text;
            if (last) setInput(last);
          }
        } catch {}
      })();
    }
  };

  // Suggestions only shown when a repo is actually indexed — prevents
  // users clicking a suggestion and getting "no results found"
  const suggestions = hasIndexedFiles
    ? [
        "What is the overall architecture of this codebase?",
        "Are there any security vulnerabilities in the authentication code?",
        "Explain how the database connection is managed.",
        "What does the main entry point do?",
      ]
    : [];

  // Derive a short name for the active repo banner
  const activeRepoName = activeRepoUrl
    ? (() => { try { return new URL(activeRepoUrl).pathname.replace(/^\//, ""); } catch { return activeRepoUrl; } })()
    : null;

  return (
    <div className="flex flex-col flex-1 h-full">
      {/* Top bar */}
      <div className="flex items-center justify-between px-6 py-3 border-b border-gray-700 bg-gray-900">
        <div>
          <h2 className="text-sm font-semibold text-white">Ask your codebase</h2>
          <p className="text-xs text-gray-500">
            {messages.length === 0
              ? hasIndexedFiles
                ? "Ask anything about the indexed codebase"
                : "Index a repo first, then ask any question"
              : `${messages.filter((m) => m.role === "user").length} question(s) asked`}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {/* Active repo badge — always visible so user knows what's scoped */}
          {activeRepoName && (
            <span className="flex items-center gap-1 text-xs text-purple-400 bg-purple-900/30 border border-purple-800/40 px-2 py-1 rounded-full max-w-[160px]">
              <Database className="w-3 h-3 shrink-0" />
              <span className="truncate">{activeRepoName}</span>
            </span>
          )}
          {messages.length > 0 && (
            <button
              onClick={clearChat}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-red-400 transition-colors"
            >
              <Trash2 className="w-3.5 h-3.5" />
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4 bg-gray-950">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center gap-6 text-center py-6">
            <div>
              <h3 className="text-lg font-semibold text-white mb-1">
                {hasIndexedFiles ? "What would you like to know?" : "No codebase indexed yet"}
              </h3>
              <p className="text-sm text-gray-500">
                {hasIndexedFiles
                  ? "Ask anything about the indexed codebase"
                  : "Paste a GitHub URL in the sidebar to get started"}
              </p>
            </div>

            {/* Auto Architecture Diagram on Connect — calls get_indexed_files + ask_codebase → Mermaid */}
            {hasIndexedFiles && (
              <ArchitectureDiagram activeRepoUrl={activeRepoUrl} hasIndexedFiles={hasIndexedFiles} />
            )}

            {/* Suggestions — only render when a repo is indexed */}
            {suggestions.length > 0 && (
              <div className="grid grid-cols-1 gap-2 w-full max-w-lg">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    onClick={() => sendMessage(s, activeRepoUrl)}
                    className="text-left text-sm text-gray-400 bg-gray-800 hover:bg-gray-700 border border-gray-700 hover:border-purple-600 px-4 py-2.5 rounded-xl transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          messages.map((msg, idx) => {
            // For assistant bubbles, find the preceding user question for share snapshot
            let prevQuestion: string | null = null;
            if (msg.role === "assistant") {
              for (let j = idx - 1; j >= 0; j--) {
                if (messages[j].role === "user") { prevQuestion = messages[j].content; break; }
              }
            }
            return (
              <MessageBubble
                key={msg.id}
                message={msg}
                activeRepoUrl={activeRepoUrl}
                prevQuestion={prevQuestion}
                chatHistory={messages.slice(Math.max(0, idx - 4), idx).map(m => ({ role: m.role, content: m.content }))}
              />
            );
          })
        )}

        {error && (
          <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-2">
            {error}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input area */}
      <div className="px-6 py-4 border-t border-gray-700 bg-gray-900 relative">
        <div className="flex gap-3 items-end">
          {slashOpen && slashCmds.length > 0 && (
            <div className="absolute bottom-14 left-6 right-20 bg-gray-800 border border-gray-700 rounded-lg shadow-xl overflow-hidden z-10">
              {slashCmds.map((c)=> (
                <button
                  key={c.command}
                  onClick={async ()=>{
                    const args = input.trim().slice(c.command.length).trim();
                    try {
                      const res = await apiFetch("/api/v1/slash/expand", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({command: c.command, args})});
                      if (res.ok) { const data = await res.json(); setInput(data.prompt); }
                      else setInput(c.command + " " + args);
                    } catch { setInput(c.command + " " + args); }
                    setSlashOpen(false);
                  }}
                  className="w-full text-left px-3 py-1.5 text-xs hover:bg-gray-700 flex justify-between"
                >
                  <span className="font-mono text-emerald-400">{c.command}</span><span className="text-gray-500 truncate ml-2">{c.description}</span>
                </button>
              ))}
            </div>
          )}
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDownWithHistory}
            placeholder={
              hasIndexedFiles
                ? "Ask a question about the codebase... (Enter to send)"
                : "Index a repo first to start asking questions"
            }
            disabled={!hasIndexedFiles}
            rows={1}
            className="flex-1 bg-gray-800 text-white text-sm rounded-xl px-4 py-3 border border-gray-600 focus:outline-none focus:border-purple-500 placeholder-gray-500 resize-none overflow-hidden disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ minHeight: "48px", maxHeight: "160px" }}
            onInput={(e) => {
              const el = e.currentTarget;
              el.style.height = "auto";
              el.style.height = `${el.scrollHeight}px`;
            }}
          />
          <VoiceControls
            onTranscript={(t) => setInput((prev) => (prev.trim() ? prev.trim() + " " + t : t))}
            disabled={!hasIndexedFiles}
          />
          <button
            onClick={handleSend}
            disabled={isLoading || !input.trim() || !hasIndexedFiles}
            className="shrink-0 w-11 h-11 bg-purple-600 hover:bg-purple-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-xl flex items-center justify-center transition-colors"
          >
            {isLoading ? (
              <Loader2 className="w-4 h-4 text-white animate-spin" />
            ) : (
              <Send className="w-4 h-4 text-white" />
            )}
          </button>
        </div>
        <p className="text-xs text-gray-600 mt-2 text-center">
          {activeRepoName
            ? `Scoped to ${activeRepoName} — sources shown below each response`
            : "Answers are grounded in indexed code — sources shown below each response"}
        </p>
      </div>
    </div>
  );
}
