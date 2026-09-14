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
import { User, Bot, FileCode, Activity } from "lucide-react";
import { Message } from "../types";

interface MessageBubbleProps {
  message: Message;
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

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

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

        {/* Sources panel — shown after streaming completes */}
        {!isUser && message.sources && message.sources.length > 0 && !message.isStreaming && (
          <div className="flex flex-wrap gap-1.5 px-1">
            {message.sources.map((src, i) => (
              <span
                key={i}
                title={src.source}
                className="flex items-center gap-1 bg-gray-900 border border-gray-700 text-gray-400 text-xs px-2 py-0.5 rounded-full hover:border-purple-600 transition-colors cursor-default"
              >
                <FileCode className="w-3 h-3 text-purple-400 shrink-0" />
                {src.file_name}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
