/**
 * MessageBubble.tsx — Renders a single chat message.
 *
 * MARKDOWN RENDERING
 * The LLM returns rich markdown: headings, bold/italic, inline code, fenced
 * code blocks with language tags, ordered/unordered lists, tables, blockquotes,
 * and horizontal rules.
 *
 * Each element gets a dedicated className so the bubble looks like a proper
 * document rather than plain unstyled text — headings are larger and bold,
 * lists have bullets/numbers with proper indentation, tables have bordered
 * cells, blockquotes have a left accent bar, code blocks have dark
 * backgrounds and syntax highlighting.
 *
 * STREAMING CURSOR
 * When isStreaming=true, a blinking ▋ is appended to give clear visual
 * feedback that the answer is still being generated.
 *
 * GENERATION STEPS
 * While streaming, the last status step is shown underneath the bubble as a
 * small muted line — "Planning [api] retrieval…", "Reranking evidence…", etc.
 * This disappears once the stream finishes.
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useState } from "react";
import { User, Bot, FileCode, Activity, ShieldCheck, ShieldAlert, Shield, ExternalLink, Share2, Copy, Check } from "lucide-react";
// snippet save event dispatched via savflux:snippet-save
import { apiFetch } from "../api";
import { ExportButton } from "./ExportButton";
import { VoiceButton } from "./VoiceButton";
import { Message, SourceFile } from "../types";
import { TrustLedgerDrawer } from "./TrustLedger";

interface MessageBubbleProps {
  message: Message;
  /** Active repo for ledger verification (passed from ChatWindow) */
  activeRepoUrl?: string | null;
  /** Previous user question — used for share snapshot */
  prevQuestion?: string | null;
  /** Chat history for fuller share context */
  chatHistory?: { role: string; content: string }[] | null;
  /** Optional: navigate to Review tab for a source file */
  onOpenInReview?: (source: string) => void;
}

// ── Markdown component overrides ─────────────────────────────────────────────
// Every element the LLM might produce gets a tailored className.
// We define this object outside the component so it is never re-created on
// each render — important because ReactMarkdown does a shallow-equal check.
const mdComponents: React.ComponentProps<typeof ReactMarkdown>["components"] = {
  // ── Headings ──────────────────────────────────────────────────────────────
  h1: ({ children }) => (
    <h1 className="text-lg font-bold text-white mt-4 mb-2 leading-snug border-b border-gray-600 pb-1">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-base font-bold text-white mt-4 mb-1.5 leading-snug">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-sm font-semibold text-gray-200 mt-3 mb-1 leading-snug">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="text-sm font-semibold text-gray-300 mt-2 mb-1">{children}</h4>
  ),

  // ── Paragraph ─────────────────────────────────────────────────────────────
  p: ({ children }) => (
    <p className="text-sm text-gray-100 leading-relaxed my-1.5">{children}</p>
  ),

  // ── Lists ─────────────────────────────────────────────────────────────────
  ul: ({ children }) => (
    <ul className="list-disc list-outside pl-5 my-2 space-y-1 text-sm text-gray-100">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal list-outside pl-5 my-2 space-y-1 text-sm text-gray-100">
      {children}
    </ol>
  ),
  li: ({ children }) => (
    <li className="leading-relaxed pl-0.5">{children}</li>
  ),

  // ── Code — inline and block ───────────────────────────────────────────────
  code({ className, children }: any) {
    const match = /language-(\w+)/.exec(className || "");
    const isBlock = Boolean(match);
    if (isBlock && match) {
      return (
        <div className="my-3 rounded-lg overflow-hidden border border-gray-700">
          <div className="flex items-center justify-between px-3 py-1 bg-gray-900 border-b border-gray-700">
            <span className="text-xs text-gray-500 font-mono">{match[1]}</span>
          </div>
          <SyntaxHighlighter
            style={vscDarkPlus}
            language={match[1]}
            PreTag="div"
            customStyle={{
              margin: 0,
              borderRadius: 0,
              fontSize: "12px",
              background: "#0d1117",
            }}
          >
            {String(children).replace(/\n$/, "")}
          </SyntaxHighlighter>
        </div>
      );
    }
    return (
      <code className="bg-gray-700/70 text-purple-300 px-1.5 py-0.5 rounded text-xs font-mono">
        {children}
      </code>
    );
  },

  // ── Blockquote ────────────────────────────────────────────────────────────
  blockquote: ({ children }) => (
    <blockquote className="border-l-4 border-purple-500 pl-3 my-2 text-gray-400 italic text-sm">
      {children}
    </blockquote>
  ),

  // ── Horizontal rule ───────────────────────────────────────────────────────
  hr: () => <hr className="border-gray-600 my-4" />,

  // ── Table ─────────────────────────────────────────────────────────────────
  table: ({ children }) => (
    <div className="overflow-x-auto my-3 rounded-lg border border-gray-700">
      <table className="w-full text-sm border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-gray-800 text-gray-300 text-xs uppercase">{children}</thead>
  ),
  tbody: ({ children }) => (
    <tbody className="divide-y divide-gray-700">{children}</tbody>
  ),
  tr: ({ children }) => <tr className="hover:bg-gray-800/50 transition-colors">{children}</tr>,
  th: ({ children }) => (
    <th className="px-3 py-2 text-left font-semibold text-gray-300">{children}</th>
  ),
  td: ({ children }) => (
    <td className="px-3 py-2 text-gray-200">{children}</td>
  ),

  // ── Strong / Em ───────────────────────────────────────────────────────────
  strong: ({ children }) => (
    <strong className="font-semibold text-white">{children}</strong>
  ),
  em: ({ children }) => (
    <em className="italic text-gray-300">{children}</em>
  ),

  // ── Links — open in new tab ───────────────────────────────────────────────
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-blue-400 underline underline-offset-2 hover:text-blue-300 transition-colors"
    >
      {children}
    </a>
  ),
};

function trustChip(trust?: string) {
  if (trust === "high") return { Icon: ShieldCheck, color: "text-emerald-400", bg: "bg-emerald-500/10", border: "border-emerald-500/25", label: "high" };
  if (trust === "medium") return { Icon: ShieldAlert, color: "text-amber-400", bg: "bg-amber-500/10", border: "border-amber-500/25", label: "med" };
  return { Icon: Shield, color: "text-gray-500", bg: "bg-gray-700/40", border: "border-gray-600", label: "low" };
}

export function MessageBubble({ message, activeRepoUrl, prevQuestion, chatHistory, onOpenInReview }: MessageBubbleProps) {
  const isUser = message.role === "user";
  const [selected, setSelected] = useState<SourceFile | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const [copied, setCopied] = useState(false);

  return (
    <div className={`flex gap-3 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      {/* Avatar */}
      <div
        className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 mt-0.5 ${
          isUser ? "bg-blue-600" : "bg-purple-700"
        }`}
      >
        {isUser ? (
          <User className="w-4 h-4 text-white" />
        ) : (
          <Bot className="w-4 h-4 text-white" />
        )}
      </div>

      <div className={`flex flex-col gap-2 max-w-[82%] ${isUser ? "items-end" : "items-start"}`}>
        {/* Message bubble */}
        <div
          className={`rounded-2xl px-4 py-3 text-sm leading-relaxed ${
            isUser
              ? "bg-blue-600 text-white rounded-tr-sm"
              : "bg-gray-800 text-gray-100 rounded-tl-sm"
          }`}
        >
          {isUser ? (
            // User messages: plain text, preserve line breaks
            <p className="whitespace-pre-wrap">{message.content}</p>
          ) : (
            // Assistant messages: full markdown rendering
            <div className="prose-content">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={mdComponents}
              >
                {message.content + (message.isStreaming ? " ▋" : "")}
              </ReactMarkdown>
            </div>
          )}
        </div>

        {/* Generation step — visible while streaming, disappears on finish */}
        {!isUser && message.isStreaming && message.generationSteps && message.generationSteps.length > 0 && (
          <div className="flex items-center gap-1.5 text-xs text-gray-500 px-1">
            <Activity className="w-3 h-3 text-purple-400 animate-pulse shrink-0" />
            <span className="truncate max-w-xs">
              {message.generationSteps[message.generationSteps.length - 1]}
            </span>
          </div>
        )}

        {/* Trust Ledger — clickable __SOURCES__ citations (P0 #4) */}
        {!isUser && message.sources && message.sources.length > 0 && !message.isStreaming && (
          <div className="w-full max-w-full">
            {/* Ledger header */}
            <div className="flex items-center gap-1.5 px-1 mb-1.5">
              <ShieldCheck className="w-3 h-3 text-emerald-400" />
              <span className="text-xs font-semibold text-gray-300">Trust Ledger</span>
              <span className="text-xs text-gray-500">
                · {message.sources.length} source{message.sources.length > 1 ? "s" : ""} · click to verify
              </span>
              <span className="ml-auto flex items-center gap-1 text-xs text-gray-600">
                {(() => {
                  const highs = message.sources!.filter((s) => s.trust_level === "high").length;
                  const meds = message.sources!.filter((s) => s.trust_level === "medium").length;
                  return (
                    <>
                      {highs > 0 && <span className="text-emerald-400">{highs} high</span>}
                      {highs > 0 && meds > 0 && <span>·</span>}
                      {meds > 0 && <span className="text-amber-400">{meds} med</span>}
                      {(highs > 0 || meds > 0) && <span className="text-gray-500">· grounded</span>}
                    </>
                  );
                })()}
              </span>
            </div>

            {/* Clickable citation chips — line-precise (P0 #5.1) */}
            <div className="flex flex-wrap gap-1.5 px-1">
              {message.sources.map((src, i) => {
                const t = trustChip(src.trust_level);
                const TIcon = t.Icon;
                const lineLabel = src.start_line ? `:${src.start_line}${src.end_line && src.end_line !== src.start_line ? `-${src.end_line}` : ""}` : "";
                return (
                  <button
                    key={i}
                    onClick={() => setSelected(src)}
                    title={`${src.source}${lineLabel} — trust: ${src.trust_level ?? "low"} (${src.trust_score ?? "—"}) — click to verify`}
                    className={`group flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border transition-all hover:scale-[1.02] cursor-pointer ${t.bg} ${t.border} ${t.color} hover:border-purple-500/50`}
                  >
                    <TIcon className="w-3 h-3 shrink-0" />
                    <FileCode className="w-3 h-3 shrink-0 opacity-60" />
                    <span className="font-medium max-w-[140px] truncate">
                      {src.file_name}
                      {lineLabel && <span className="font-mono text-[10px] opacity-70">{lineLabel}</span>}
                    </span>
                    {src.trust_score && <span className="text-[10px] opacity-60">·{src.trust_score}</span>}
                    <ExternalLink className="w-3 h-3 opacity-0 group-hover:opacity-60 transition-opacity" />
                  </button>
                );
              })}
            </div>

            {/* Ledger drawer */}
            {selected && (
              <TrustLedgerDrawer
                source={selected}
                onClose={() => setSelected(null)}
                activeRepoUrl={activeRepoUrl}
                onOpenInReview={onOpenInReview}
              />
            )}
          </div>
        )}

        {/* Voice + Share + Export — always for assistant when not streaming (P1 #5.5 + P2 #8 + P2 Voice) */}
        {!isUser && !message.isStreaming && (
          <div className="flex items-center gap-2 px-1 mt-1 flex-wrap">
            <VoiceButton text={message.content} isStreaming={message.isStreaming} />
            <ExportButton question={prevQuestion || "Shared from SavFlux"} answer={message.content} sources={message.sources as any} repoUrl={activeRepoUrl} ledger={message.sources ? { sources: message.sources } : undefined} />
            {!shareUrl ? (
              <button
                onClick={async () => {
                  if (sharing) return;
                  setSharing(true);
                  try {
                    const question = (prevQuestion || "").trim() || "Shared from SavFlux";
                    const res = await apiFetch("/api/v1/chat/share", {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        question,
                        answer: message.content,
                        sources: message.sources || [],
                        repo_url: activeRepoUrl || undefined,
                        ledger: message.sources ? { sources: message.sources } : undefined,
                        chat_history: chatHistory || undefined,
                      }),
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || "Share failed");
                    const url = data.url || `https://savflux.app/s/${data.id}`;
                    setShareUrl(url);
                    try { await navigator.clipboard.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch {}
                  } catch (e) {
                    // silent — user can retry
                  } finally { setSharing(false); }
                }}
                disabled={sharing}
                title="Create shareable link with ledger snapshot — https://savflux.app/s/{id}"
                className="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-gray-700/60 hover:bg-gray-700 border border-gray-600 hover:border-purple-500/40 text-gray-300 hover:text-white transition-colors disabled:opacity-50"
              >
                <Share2 className="w-3 h-3" />
                {sharing ? "Sharing…" : "Share"}
              </button>
            ) : (
              <div className="flex items-center gap-1.5 text-xs bg-emerald-900/30 border border-emerald-700/30 text-emerald-300 px-2.5 py-1 rounded-full">
                <Check className="w-3 h-3" />
                <span className="max-w-[180px] truncate">{shareUrl}</span>
                <button
                  onClick={async () => { try { await navigator.clipboard.writeText(shareUrl); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch {} }}
                  className="ml-1 p-1 hover:bg-emerald-800/40 rounded"
                  title="Copy link"
                >
                  {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                </button>
                <a href={shareUrl} target="_blank" rel="noopener noreferrer" className="p-1 hover:bg-emerald-800/40 rounded" title="Open">
                  <ExternalLink className="w-3 h-3" />
                </a>
              </div>
            )}
            {shareUrl && <span className="text-xs text-gray-500">{copied ? "Copied!" : "Link copied"}</span>}
          </div>
        )}
      </div>
    </div>
  );
}
