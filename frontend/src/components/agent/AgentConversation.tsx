/**
 * AgentConversation.tsx — one surface for "ask" and "do".
 *
 * WHAT CHANGED AND WHY
 * --------------------
 * The old app had two tabs for one job. `Chat` streamed an answer from
 * `/chat/stream`; `Agent` took a goal and streamed a plan from `/agent/run`.
 * Both were "tell the agent something and watch it work", split across a tab
 * boundary, with separate composers, separate histories and no way to continue
 * one from the other. The user's complaint that the product felt like a
 * collection of unrelated tools is, in large part, this.
 *
 * So the conversation is now the product, and a turn has a *mode*:
 *
 *   Ask    → retrieval + the local model. The default, because a question is
 *            what people arrive with.
 *   Agent  → the deterministic tool loop: retrieve, read, graph, blast radius,
 *            autofix, patch, and a pull request behind a digest-bound
 *            confirmation. A plan strip, per-tool timing, and a Stop that
 *            reaches the run rather than the fetch.
 *
 * Both render in the same thread, so "explain this" followed by "fix it" is one
 * conversation instead of two tabs and a lost context.
 *
 * WHAT IS DELIBERATELY NOT HERE
 * -----------------------------
 * The app's honesty rules are load-bearing and they stay: a citation that was
 * never reranked says `unrated` and not `low`; a cancelled run says so instead
 * of rendering as an error; a result served from the review cache says "no model
 * call" rather than "0 ms". A tidier layout is not worth a tidier claim.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  ArrowDown,
  Check,
  Copy,
  CornerDownLeft,
  FileCode,
  Loader2,
  MessageSquare,
  Mic,
  Send,
  Search,
  ShieldAlert,
  ShieldCheck,
  Square,
  Sparkles,
  Upload,
  Trash2,
  Wrench,
  Zap,
} from "lucide-react";
import { apiFetch } from "../../api";
import { useChat } from "../../hooks/useChat";
import { openFileAt } from "../../lib/openFile";
import { usePublicIngest } from "../../hooks/usePublicIngest";
import { repoDisplayName } from "../../lib/github";
import { AGENT_TAGS, decodeStatus, drainMarkers, flushTail } from "../../lib/stream";
import {
  type AgentRunState,
  appendReport,
  applyEvent,
  applyStreamError,
  initialRunState,
  markCancelled,
} from "../../lib/agentRun";
import { ActivityGroup } from "./ActivityGroup";
import { DiffBlock } from "../DiffBlock";
import { CodeHighlight } from "../../lib/highlight";
import { VoiceControls } from "../VoiceControls";
import type { Message, SourceFile, GroundingReport } from "../../types";
import type { ModelStatus } from "../../types/workspace";

export type AgentMode = "ask" | "agent";

interface AgentConversationProps {
  activeRepoUrl: string | null;
  activeRepoUrls?: string[] | null;
  hasIndexedFiles: boolean;
  models: ModelStatus | null;
  onOpenGitHub: () => void;
  /** Re-read the index. The empty state can index a public URL or an upload. */
  onIndexed: () => void;
}

const STARTERS = [
  "Where is the JWT signature verified, and what protects it?",
  "What would break if I changed the embedding model?",
  "Find the hardcoded secrets and patch them",
  "How does the review budget decide which files reach the model?",
];

/* ── Markdown ─────────────────────────────────────────────────────────────── */

const md: React.ComponentProps<typeof ReactMarkdown>["components"] = {
  h1: ({ children }) => <h1 className="sf-text mt-5 mb-2 text-[16px] font-semibold">{children}</h1>,
  h2: ({ children }) => <h2 className="sf-text mt-4 mb-1.5 text-[14.5px] font-semibold">{children}</h2>,
  h3: ({ children }) => <h3 className="sf-dim mt-3 mb-1 text-[13.5px] font-semibold">{children}</h3>,
  h4: ({ children }) => <h4 className="sf-dim mt-2 mb-1 text-[13px] font-semibold">{children}</h4>,
  p: ({ children }) => <p className="sf-text my-1.5 text-[13.5px] leading-[1.7]">{children}</p>,
  ul: ({ children }) => <ul className="sf-text my-2 list-disc space-y-1 pl-5 text-[13.5px] leading-[1.7]">{children}</ul>,
  ol: ({ children }) => <ol className="sf-text my-2 list-decimal space-y-1 pl-5 text-[13.5px] leading-[1.7]">{children}</ol>,
  li: ({ children }) => <li className="leading-[1.7]">{children}</li>,
  code({ className, children }: any) {
    const match = /language-([\w+-]+)/.exec(className || "");
    const text = String(children ?? "").replace(/\n$/, "");
    if (match) {
      if (match[1] === "diff" || text.startsWith("diff --git")) {
        return <DiffBlock diff={text} downloadName="savflux.patch" copyLabel="Copy patch" />;
      }
      return (
        <div className="my-3 overflow-hidden rounded-lg border sf-line">
          <div className="sf-mono border-b sf-line px-3 py-1 text-[10.5px] sf-mute">{match[1]}</div>
          <CodeHighlight language={match[1]} customStyle={{ margin: 0, borderRadius: 0, fontSize: "12px", background: "transparent" }}>
            {text}
          </CodeHighlight>
        </div>
      );
    }
    return (
      <code className="sf-mono sf-raised rounded border sf-line px-1.5 py-0.5 text-[12px] sf-accent">
        {children}
      </code>
    );
  },
  pre: ({ children }) => <>{children}</>,
  blockquote: ({ children }) => (
    <blockquote className="sf-mute my-2 border-l-2 sf-line pl-3 text-[13px] italic">{children}</blockquote>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto rounded-lg border sf-line">
      <table className="sf-text w-full border-collapse text-[12.5px]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border-b sf-line px-3 py-1.5 text-left sf-dim">{children}</th>,
  td: ({ children }) => <td className="border-b sf-line px-3 py-1.5 sf-text">{children}</td>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="sf-accent underline underline-offset-2">
      {children}
    </a>
  ),
  hr: () => <hr className="my-4 sf-line" />,
};

/* ── Citations ────────────────────────────────────────────────────────────── */

function trustChip(level?: string) {
  switch (level) {
    case "high":
      return { Icon: ShieldCheck, cls: "sf-chip-good", label: "high" };
    case "medium":
      return { Icon: ShieldAlert, cls: "sf-chip-warn", label: "med" };
    case "low":
      return { Icon: ShieldAlert, cls: "sf-chip", label: "low" };
    // "unrated" is not "low". A chunk the reranker never scored was never
    // judged, and rendering it like a weak chunk tells the reader their
    // evidence was assessed and found wanting when it was never assessed.
    default:
      return { Icon: ShieldAlert, cls: "sf-chip", label: "unrated" };
  }
}

function lineSuffix(src: SourceFile): string {
  if (typeof src.start_line !== "number") return "";
  if (typeof src.end_line === "number" && src.end_line !== src.start_line) {
    return `:${src.start_line}-${src.end_line}`;
  }
  return `:${src.start_line}`;
}

function GroundingNotice({ report }: { report: GroundingReport }) {
  /**
   * Sits directly under the answer it is about, not in a log, and not at the
   * top where it would read as a verdict on the whole conversation.
   *
   * It says what was found and stops there. A citation pointing outside the
   * retrieved set is not proof the answer is wrong — the model may be right
   * about a file it was not shown — so the wording never claims it is, and the
   * detail is one click away for anyone who wants to check. Suppressing the
   * answer instead would be the worse trade: a wrong answer you can catch beats
   * a missing one you cannot.
   */
  const declined = report.insufficient_evidence && !report.delivered_anyway;

  return (
    <div
      className="mt-2.5 rounded-lg border sf-line bg-[var(--sf-warn-bg,transparent)] px-3 py-2.5 text-[12.5px]"
      role="note"
    >
      <p className="flex items-center gap-1.5 font-medium">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {declined
          ? "This answer says the code it found was not enough to answer"
          : "Some citations are not in the evidence shown"}
      </p>

      {report.unverified.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {report.unverified.map((c, i) => (
            <li key={`${c.reference}-${i}`} className="sf-dim">
              <code className="sf-mono">{c.reference}</code> — {c.detail}
            </li>
          ))}
        </ul>
      )}

      {declined && (
        <p className="sf-dim mt-1.5">
          Retrieval found {report.citations_claimed === 0 ? "no relevant code" : "code that did not settle it"}.
          Try naming a file with <code className="sf-mono">@filename.py</code>, or a specific symbol.
        </p>
      )}

      <p className="sf-mute mt-1.5 text-[11.5px]">
        Every citation below is one this answer was actually given. The ones above are not.
      </p>
    </div>
  );
}

function Citations({ sources }: { sources: SourceFile[] }) {
  return (
    <div className="mt-3 border-t sf-line pt-2.5">
      <p className="sf-mute mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.08em]">
        {sources.length} {sources.length === 1 ? "source" : "sources"} · click to open
      </p>
      <div className="flex flex-wrap gap-1.5">
        {sources.map((src, i) => {
          const t = trustChip(src.trust_level);
          const Icon = t.Icon;
          const span = lineSuffix(src);
          return (
            <button
              key={i}
              type="button"
              onClick={() =>
                openFileAt({
                  source: src.source,
                  startLine: src.start_line,
                  endLine: src.end_line,
                  lineRanges: src.line_ranges,
                })
              }
              title={`${src.file_name}${span} — trust: ${t.label}${
                typeof src.trust_score === "number" ? ` (${src.trust_score})` : " (not scored)"
              }`}
              className={`sf-chip ${t.cls} transition-transform hover:scale-[1.02]`}
            >
              <Icon className="h-3 w-3 shrink-0" />
              <FileCode className="h-3 w-3 shrink-0 opacity-60" />
              <span className="max-w-[150px] truncate">{src.file_name}</span>
              {span && <span className="sf-mono text-[10px] opacity-70">{span}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ── Turns ────────────────────────────────────────────────────────────────── */

function UserTurn({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-indigo-600 px-4 py-2.5 text-[13.5px] leading-[1.65] text-white">
        {text}
      </div>
    </div>
  );
}

function AssistantTurn({ message }: { message: Message }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="group">
      {message.isStreaming && !message.content && message.generationSteps?.length ? (
        <p className="sf-mute flex items-center gap-2 py-1 text-[12.5px]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {message.generationSteps[message.generationSteps.length - 1]}
        </p>
      ) : (
        <div className="prose-sf">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={md}>
            {message.content + (message.isStreaming ? " ▋" : "")}
          </ReactMarkdown>
        </div>
      )}

      {!message.isStreaming && message.content && (
        <div className="mt-2 flex items-center gap-1.5 opacity-0 transition-opacity group-hover:opacity-100">
          <button
            type="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(message.content);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1600);
              } catch {
                /* clipboard blocked — the text is still selectable */
              }
            }}
            className="sf-btn sf-btn-ghost px-2 py-1 text-[11.5px]"
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      )}

      {!message.isStreaming && message.grounding && !message.grounding.is_clean && (
        <GroundingNotice report={message.grounding} />
      )}

      {message.sources && message.sources.length > 0 && !message.isStreaming && (
        <Citations sources={message.sources} />
      )}
    </div>
  );
}

function AgentTurn({ run }: { run: AgentRunState }) {
  return (
    <div>
      {/* The goal, in the reader's own words. An agent run streams a plan strip,
          a timeline and a report, and none of those say what it was asked to do —
          so the one line that makes the rest interpretable was the one line
          missing, and scrolling down lost the question entirely. Same bubble as
          Ask, because it is the same act: someone asked a question. */}
      {run.goal.trim() && <UserTurn text={run.goal} />}
      <ActivityGroup run={run} running={run.status === "running"} />
      {run.pendingConfirm && (
        <div className="mt-2 flex flex-wrap items-center gap-3 rounded-xl border sf-line sf-raised px-3.5 py-3">
          <ShieldCheck className="h-4 w-4 shrink-0 sf-accent" />
          <p className="sf-dim min-w-0 flex-1 text-[12.5px]">
            A pull request is prepared and has <strong>not</strong> been opened. It will only go out
            when you confirm the exact patch.
          </p>
        </div>
      )}
      {run.prUrl && (
        <a
          href={run.prUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="sf-btn sf-btn-primary mt-2"
        >
          <Sparkles className="h-3.5 w-3.5" /> View pull request #{run.prNumber}
        </a>
      )}
      {run.report.trim() && (
        <div className="prose-sf mt-2">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={md}>
            {run.report}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}

/* ── Empty state ──────────────────────────────────────────────────────────── */

function EmptyState({
  hasIndex,
  onOpenGitHub,
  onIndexed,
  modelBlocked,
  modelHint,
}: {
  hasIndex: boolean;
  onOpenGitHub: () => void;
  /** Called after a public URL or an upload finishes indexing. */
  onIndexed: () => void;
  modelBlocked: boolean;
  modelHint: string | null;
}) {
  const [url, setUrl] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const { ingesting, progress, error, ingestUrl, upload } = usePublicIngest(onIndexed);

  if (!hasIndex) {
    return (
      <div className="mx-auto flex min-h-[62vh] max-w-lg flex-col items-center justify-center gap-5 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-indigo-500/12 text-indigo-300">
          <Sparkles className="h-6 w-6" />
        </div>
        <div>
          <h1 className="sf-text text-[19px] font-semibold tracking-tight">
            Give SavFlux a repository
          </h1>
          <p className="sf-mute mx-auto mt-2 max-w-md text-[13px] leading-relaxed">
            It indexes the code, then answers questions about it, reviews it, and opens pull
            requests — all on models that run on your machine.
          </p>
        </div>
        <button type="button" onClick={onOpenGitHub} className="sf-btn sf-btn-primary">
          <Zap className="h-3.5 w-3.5" /> Connect GitHub
        </button>

        {/* The sentence under the button used to be a promise with nothing
            behind it. The two operations it described lived in the
            Repositories panel, one destination away, so a reader who believed
            it and clicked nothing was stuck on the product's first screen. The
            controls are here now, and the copy describes them rather than
            promising them. */}
        <div className="w-full max-w-md space-y-2.5">
          <p className="sf-mute text-[11.5px]">
            Or paste a public URL, or upload a folder — no account needed.
          </p>
          <div className="flex gap-2">
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !ingesting) void ingestUrl(url);
              }}
              placeholder="https://github.com/owner/repo"
              aria-label="Public repository URL"
              className="sf-input min-w-0 flex-1"
              disabled={!!ingesting}
            />
            <button
              type="button"
              onClick={() => void ingestUrl(url)}
              disabled={!!ingesting || !url.trim()}
              className="sf-btn sf-btn-primary shrink-0"
            >
              {ingesting === url.trim() ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Search className="h-3.5 w-3.5" />
              )}
              Index
            </button>
          </div>
          <label className="sf-btn sf-btn-ghost w-full cursor-pointer justify-center">
            <Upload className="h-3.5 w-3.5" />
            {ingesting ? "Indexing…" : "Upload files from this computer"}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="sr-only"
              onChange={(e) => void upload(e.target.files)}
            />
          </label>
          {progress && (
            <p className="sf-mute flex items-center gap-1.5 text-[11.5px]">
              <Loader2 className="h-3 w-3 animate-spin" />
              {progress.message}
            </p>
          )}
          {error && (
            <p
              role="alert"
              className="rounded-lg px-3 py-2 text-[11.5px]"
              style={{ background: "rgba(248,113,113,0.1)", color: "#fca5a5" }}
            >
              {error}
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex min-h-[62vh] max-w-2xl flex-col justify-center">
      <h1 className="sf-text text-[17px] font-semibold tracking-tight">
        What should the agent do?
      </h1>
      <p className="sf-mute mt-1 text-[13px]">
        Ask a question, or switch to <strong className="sf-dim">Agent</strong> and give it a task.
        Either way you can see exactly which lines it used.
      </p>
      {modelBlocked && modelHint && (
        <div
          className="mt-4 flex items-start gap-2.5 rounded-xl border px-3.5 py-3 text-[12.5px]"
          style={{ borderColor: "rgba(251,191,36,0.28)", background: "rgba(251,191,36,0.07)", color: "#fcd34d" }}
        >
          <ShieldAlert className="mt-px h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">The local model is not answering yet</p>
            <p className="mt-0.5 opacity-90">{modelHint}</p>
            <p className="mt-1.5 opacity-75">
              Everything that does not need a model — the dependency graph, static analysis,
              autofix, patch building, citations — already works.
            </p>
          </div>
        </div>
      )}
      <div className="mt-5 grid gap-2 sm:grid-cols-2">
        {STARTERS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent("savflux:send-prompt", { detail: s }))}
            className="sf-surface rounded-xl border sf-line px-3.5 py-2.5 text-left text-[12.5px] sf-dim transition-colors hover:border-[var(--sf-accent-line)] hover:text-[var(--sf-text)]"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Composer ─────────────────────────────────────────────────────────────── */

function Composer({
  mode,
  onMode,
  value,
  onChange,
  onSubmit,
  onStop,
  busy,
  disabled,
  repoLabel,
  models,
}: {
  mode: AgentMode;
  onMode: (m: AgentMode) => void;
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  busy: boolean;
  disabled: boolean;
  repoLabel: string | null;
  models: ModelStatus | null;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // Grow with the content up to a ceiling. A composer that is one line tall
  // hides the second line of the question the user just typed.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  return (
    <div className="sf-surface border-t sf-line px-4 py-3">
      <div className="mx-auto max-w-3xl">
        <div className="sf-raised rounded-2xl border sf-line transition-colors focus-within:border-[var(--sf-accent-line)]">
          <textarea
            ref={ref}
            value={value}
            rows={1}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                onSubmit();
              }
            }}
            placeholder={
              disabled
                ? "Index a repository to start"
                : mode === "ask"
                  ? "Ask anything about this codebase…"
                  : "Give the agent a goal — e.g. find the hardcoded secrets and patch them"
            }
            aria-label={mode === "ask" ? "Ask a question" : "Agent goal"}
            className="sf-text max-h-[200px] w-full resize-none bg-transparent px-4 pb-1 pt-3 text-[13.5px] leading-[1.6] outline-none placeholder:text-[var(--sf-text-mute)] disabled:opacity-50"
          />
          <div className="flex items-center gap-1.5 px-2.5 pb-2.5">
            {/* Mode switch — the decision that used to be a tab. */}
            <div
              role="radiogroup"
              aria-label="Agent mode"
              className="flex items-center gap-0.5 rounded-lg bg-[var(--sf-canvas)] p-0.5"
            >
              {(
                [
                  { id: "ask", label: "Ask", Icon: MessageSquare },
                  { id: "agent", label: "Agent", Icon: Wrench },
                ] as const
              ).map((m) => {
                const Icon = m.Icon;
                const active = mode === m.id;
                return (
                  <button
                    key={m.id}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => onMode(m.id)}
                    className={[
                      "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors",
                      active ? "sf-raised sf-text" : "sf-mute hover:text-[var(--sf-text)]",
                    ].join(" ")}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {m.label}
                  </button>
                );
              })}
            </div>

            <div className="flex-1" />

            {models && (
              <span
                className="sf-mute hidden truncate text-[11px] sm:block"
                title={models.available ? models.chat_model : models.hint ?? ""}
              >
                {models.chat_model}
              </span>
            )}

            <VoiceControls onTranscript={(t) => onChange(value.trim() ? `${value.trim()} ${t}` : t)} disabled={disabled} />

            {busy ? (
              <button type="button" onClick={onStop} className="sf-btn sf-btn-danger" aria-label="Stop">
                <Square className="h-3 w-3 fill-current" /> Stop
              </button>
            ) : (
              <button
                type="button"
                onClick={onSubmit}
                disabled={disabled || !value.trim()}
                className="sf-btn sf-btn-primary"
                aria-label={mode === "ask" ? "Send" : "Run"}
              >
                {mode === "ask" ? <Send className="h-3.5 w-3.5" /> : <Wrench className="h-3.5 w-3.5" />}
                {mode === "ask" ? "Send" : "Run"}
                <CornerDownLeft className="h-3 w-3 opacity-60" />
              </button>
            )}
          </div>
        </div>
        <p className="sf-mute mt-2 text-center text-[11px]">
          {repoLabel
            ? `Scoped to ${repoLabel} · every answer carries the lines it used`
            : "Every answer carries the lines it used · Shift+Enter for a new line"}
        </p>
      </div>
    </div>
  );
}

/* ── The conversation ─────────────────────────────────────────────────────── */

export function AgentConversation({
  activeRepoUrl,
  activeRepoUrls,
  hasIndexedFiles,
  models,
  onOpenGitHub,
  onIndexed,
}: AgentConversationProps) {
  const { messages, isLoading, error, sendMessage, clearChat } = useChat(activeRepoUrl, activeRepoUrls);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<AgentMode>("ask");
  const [run, setRun] = useState<AgentRunState>(initialRunState);
  const [agentRunning, setAgentRunning] = useState(false);
  const [pinned, setPinned] = useState(true);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  const agentTurnRef = useRef<HTMLDivElement>(null);

  // A run must not outlive the surface that started it.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Follow the stream only while the reader is already at the bottom. Pinning
  // unconditionally makes it impossible to read a step that scrolled past,
  // which is the complaint the old transcript's "Jump to latest" button was
  // added to paper over.
  useEffect(() => {
    const el = scrollerRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, run]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    // jsdom has no layout, so every measurement is 0 — an all-zero reading is
    // "cannot tell", and the safe default is to keep following.
    const measurable = el.scrollHeight > el.clientHeight && el.clientHeight > 0;
    const atBottom = !measurable || el.scrollHeight - el.scrollTop - el.clientHeight < 64;
    if (atBottom !== pinnedRef.current) {
      pinnedRef.current = atBottom;
      setPinned(atBottom);
    }
  };

  // "Ask" in the empty state, and the prompt library / palette, land here.
  useEffect(() => {
    const onPrompt = (e: Event) => {
      const text = (e as CustomEvent).detail as string;
      if (!text) return;
      setInput(text);
      window.setTimeout(() => document.querySelector<HTMLTextAreaElement>("textarea[aria-label]")?.focus(), 0);
    };
    window.addEventListener("savflux:use-prompt" as any, onPrompt);
    window.addEventListener("savflux:send-prompt" as any, onPrompt);
    return () => {
      window.removeEventListener("savflux:use-prompt" as any, onPrompt);
      window.removeEventListener("savflux:send-prompt" as any, onPrompt);
    };
  }, []);

  const runAgent = useCallback(
    async (goal: string, confirmDigest?: string) => {
      if (!goal.trim() || agentRunning) return;
      const controller = new AbortController();
      abortRef.current = controller;
      setAgentRunning(true);
      setRun({ ...initialRunState(), status: "running", goal: goal.trim(), maxSteps: 8 });
      pinnedRef.current = true;
      setPinned(true);

      let buffer = "";
      try {
        const res = await apiFetch("/api/v1/agent/run", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            goal: goal.trim(),
            max_steps: 8,
            repo_url: activeRepoUrl,
            confirm_digest: confirmDigest ?? null,
          }),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) {
          const detail = await res.text().catch(() => "");
          throw new Error(detail ? `${res.status}: ${detail.slice(0, 200)}` : `Server error ${res.status}`);
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const scan = drainMarkers(buffer, AGENT_TAGS);
          buffer = scan.rest;
          for (const seg of scan.segments) {
            if (seg.kind === "text") setRun((s) => appendReport(s, seg.value));
            else if (seg.kind === "status") setRun((s) => applyEvent(s, decodeStatus(seg.payload)));
            else if (seg.kind === "error") setRun((s) => applyStreamError(s, seg.payload.trim()));
          }
        }
        const tail = flushTail(buffer, AGENT_TAGS);
        if (tail) setRun((s) => appendReport(s, tail));
        setRun((s) => (s.status === "running" ? markCancelled(s) : s));
      } catch (e) {
        if (controller.signal.aborted) setRun((s) => markCancelled(s));
        else setRun((s) => applyStreamError(s, e instanceof Error ? e.message : String(e)));
      } finally {
        abortRef.current = null;
        setAgentRunning(false);
      }
    },
    [agentRunning],
  );

  const submit = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    if (mode === "ask") {
      void sendMessage(text, activeRepoUrl);
      setInput("");
    } else {
      void runAgent(text);
      setInput("");
    }
  }, [activeRepoUrl, input, mode, runAgent, sendMessage]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setRun((s) => markCancelled(s));
  }, []);

  // `repoDisplayName` is the one place that knows a repository URL, so the
  // footer and the top bar cannot disagree about what is scoped — which they
  // did, because this one parsed the URL itself and the other stripped it.
  const repoLabel = useMemo(
    () => (activeRepoUrl ? repoDisplayName(activeRepoUrl) : null),
    [activeRepoUrl],
  );

  const busy = mode === "ask" ? isLoading : agentRunning;
  const disabled = !hasIndexedFiles;

  // The agent turn is the only thing that can exist without a chat message, so
  // the transcript is built from both rather than from `messages` alone.
  const showAgentTurn = run.status !== "idle";

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="relative flex-1 overflow-y-auto"
        data-testid="agent-transcript"
      >
        <div className="mx-auto max-w-3xl space-y-7 px-5 py-7">
          {messages.length === 0 && !showAgentTurn ? (
            <EmptyState
              hasIndex={hasIndexedFiles}
              onOpenGitHub={onOpenGitHub}
              onIndexed={onIndexed}
              modelBlocked={models !== null && !models.available}
              modelHint={models?.hint ?? null}
            />
          ) : (
            <>
              {messages.map((m, i) =>
                m.role === "user" ? (
                  <UserTurn key={m.id} text={m.content} />
                ) : (
                  <div key={m.id} ref={i === messages.length - 1 ? agentTurnRef : undefined}>
                    {/* The steps the answer was built from, above the answer. */}
                    {m.generationSteps && m.generationSteps.length > 0 && !m.isStreaming && (
                      <RetrievalSteps steps={m.generationSteps} />
                    )}
                    <AssistantTurn message={m} />
                  </div>
                ),
              )}
              {showAgentTurn && <AgentTurn run={run} />}
            </>
          )}

          {error && (
            <div
              role="alert"
              className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3.5 py-2.5 text-[12.5px] text-rose-300"
            >
              {error}
            </div>
          )}
        </div>

        {!pinned && (
          <button
            type="button"
            onClick={() => {
              pinnedRef.current = true;
              setPinned(true);
              const el = scrollerRef.current;
              if (el) el.scrollTop = el.scrollHeight;
            }}
            className="sf-btn sf-btn-secondary sticky bottom-3 left-1/2 -translate-x-1/2 shadow-lg shadow-black/40"
          >
            <ArrowDown className="h-3.5 w-3.5" /> Jump to latest
          </button>
        )}
      </div>

      {/* Thread controls. Deliberately not a second status bar: the shell
          already has one, and a reader should not have to look at two strips
          that each claim to know what the product is doing. */}
      <div className="flex min-h-[30px] items-center justify-end gap-2 border-t sf-line px-4 py-1">
        {showAgentTurn && run.status !== "running" && (
          <span className="sf-mute text-[11.5px]">
            agent run {run.status}
            {run.stepsUsed > 0 && ` · ${run.stepsUsed} steps`}
          </span>
        )}
        {(messages.length > 0 || showAgentTurn) && (
          <button
            type="button"
            onClick={() => { clearChat(); setRun(initialRunState()); }}
            className="sf-btn sf-btn-ghost px-2 py-1 text-[11.5px]"
          >
            <Trash2 className="h-3 w-3" /> New thread
          </button>
        )}
      </div>

      <Composer
        mode={mode}
        onMode={setMode}
        value={input}
        onChange={setInput}
        onSubmit={submit}
        onStop={stop}
        busy={busy}
        disabled={disabled}
        repoLabel={repoLabel}
        models={models}
      />
    </div>
  );
}

/**
 * The steps a RAG answer was built from.
 *
 * These used to be a single grey line under the bubble that disappeared when
 * streaming ended — so the work was visible while it was slow and gone once it
 * was useful. Here it collapses to one line and stays available, which is the
 * difference between a tool that shows its work and one that asks to be
 * believed.
 */
function RetrievalSteps({ steps }: { steps: string[] }) {
  const [open, setOpen] = useState(false);
  const unique = Array.from(new Set(steps.filter(Boolean)));
  if (unique.length === 0) return null;
  return (
    <div className="mb-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="sf-mute inline-flex items-center gap-1.5 text-[11.5px] transition-colors hover:text-[var(--sf-text)]"
      >
        <Wrench className="h-3 w-3" />
        {unique.length} {unique.length === 1 ? "step" : "steps"}
        <ChevronHint open={open} />
      </button>
      {open && (
        <ol className="sf-mute mt-1.5 space-y-0.5 border-l sf-line pl-3 text-[11.5px]">
          {unique.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ChevronHint({ open }: { open: boolean }) {
  return (
    <svg
      className={`h-3 w-3 transition-transform duration-150 ${open ? "rotate-90" : ""}`}
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      aria-hidden
    >
      <path d="M4.5 2.5 8 6l-3.5 3.5" />
    </svg>
  );
}

export { Mic };
