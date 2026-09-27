/**
 * AgentTimeline.tsx — what an agent run looks like, not what it logs.
 *
 * The panel this replaces rendered one bullet per status line. That is the right
 * shape for a log and the wrong shape for an agent, because the three questions a
 * reader has about a step — what did it ask for, what came back, what did it cost —
 * are exactly the three the wire format now carries (`args`, `preview`,
 * `elapsed_ms`) and a bullet shows none of them.
 *
 * So: a plan strip with per-step state, then the transcript, where a step is a
 * card. Collapsed it answers "did this go well" (icon, one line, duration);
 * expanded it answers "and how do you know" (arguments, what it found, the
 * numbers the tool reported). Reasoning lines sit inline where they happened,
 * because a plan decided before a tool call is not the same fact as one decided
 * after it.
 *
 * Deliberately absent: a fake "thinking" block. This agent is deterministic, so
 * its reasoning is the branch arithmetic the backend reported — labelled
 * "Plan reasoning", not dressed up as a chain of thought.
 */

import { useState } from "react";
import {
  AlertTriangle, Brain, Check, ChevronDown, ChevronRight, Clock, Crosshair,
  FileCode, FileDiff, GitPullRequest, Loader2, Network, Search, SkipForward,
  SquareStack, Wand2, XOctagon,
} from "lucide-react";
import type { CardState, PlanRow, ToolCard, TranscriptItem } from "../../lib/agentRun";
import { formatDuration } from "../../lib/stream";

/** Verb + icon per tool: a step should read as what it did, not what it is. */
export const TOOL_META: Record<string, { label: string; Icon: typeof Search }> = {
  retrieve_context: { label: "Searched the index", Icon: Search },
  read_file: { label: "Read a file", Icon: FileCode },
  dependency_graph: { label: "Mapped imports", Icon: Network },
  blast_radius: { label: "Traced dependents", Icon: Crosshair },
  autofix: { label: "Applied verified fixes", Icon: Wand2 },
  build_patch: { label: "Built a patch", Icon: FileDiff },
  create_pr: { label: "Pull request", Icon: GitPullRequest },
};

export function toolLabel(tool: string): string {
  return TOOL_META[tool]?.label ?? tool.replace(/_/g, " ");
}

const STATE_TONE: Record<CardState, { text: string; ring: string }> = {
  running: { text: "text-pink-300", ring: "border-gray-700" },
  done: { text: "text-emerald-300", ring: "border-gray-800" },
  failed: { text: "text-red-300", ring: "border-red-500/40" },
  skipped: { text: "text-gray-500", ring: "border-gray-800" },
  interrupted: { text: "text-amber-300", ring: "border-amber-600/40" },
};

function StateGlyph({ state }: { state: CardState }) {
  const cls = "w-3.5 h-3.5 shrink-0";
  if (state === "running") return <Loader2 className={`${cls} animate-spin ${STATE_TONE.running.text}`} />;
  if (state === "failed") return <XOctagon className={`${cls} ${STATE_TONE.failed.text}`} />;
  if (state === "skipped") return <SkipForward className={`${cls} ${STATE_TONE.skipped.text}`} />;
  if (state === "interrupted") return <AlertTriangle className={`${cls} ${STATE_TONE.interrupted.text}`} />;
  return <Check className={`${cls} ${STATE_TONE.done.text}`} />;
}

function ToolGlyph({ tool }: { tool: string }) {
  const meta = TOOL_META[tool];
  if (!meta) return <SquareStack className="w-3.5 h-3.5 shrink-0 text-gray-500" />;
  const Icon = meta.Icon;
  return <Icon className="w-3.5 h-3.5 shrink-0 text-gray-500" />;
}

/** Result fields worth printing in the grid; the ids and counters are noise here. */
const HIDDEN_FIELDS = new Set([
  "file", "id", "index", "total", "tier", "cached", "mode", "step", "message",
  // Rendered as its own block below, with the shell quoting intact: a grid cell
  // would show the same command twice.
  "gh_command",
]);

function FieldGrid({ fields }: { fields: Record<string, string | number | boolean | null> }) {
  const entries = Object.entries(fields).filter(([key]) => !HIDDEN_FIELDS.has(key));
  if (!entries.length) return null;
  return (
    <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      {entries.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="font-mono text-[10px] uppercase tracking-wide text-gray-600">{key}</dt>
          <dd className="font-mono text-[11px] text-gray-300 break-words">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ArgChips({ args }: { args: Record<string, string> }) {
  const entries = Object.entries(args);
  if (!entries.length) return <span className="text-[10px] text-gray-600">no arguments</span>;
  return (
    <>
      {entries.map(([key, value]) => (
        <span
          key={key}
          title={`${key} = ${value}`}
          className="max-w-[22ch] truncate rounded bg-gray-800/70 px-1.5 py-0.5 font-mono text-[10px] text-gray-400"
        >
          <span className="text-gray-600">{key}=</span>
          {value}
        </span>
      ))}
    </>
  );
}

export function ToolCardRow({ card, index }: { card: ToolCard; index: number }) {
  const [open, setOpen] = useState(false);
  const tone = STATE_TONE[card.state];
  const bodyId = `agent-step-${card.stepId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;

  return (
    <li className={`rounded-lg border bg-gray-900/60 ${tone.ring}`}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={bodyId}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left transition-colors hover:bg-gray-800/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/60"
      >
        <span className="w-4 shrink-0 text-right font-mono text-[10px] text-gray-600">{index}</span>
        <StateGlyph state={card.state} />
        <ToolGlyph tool={card.tool} />
        <span className={`min-w-0 truncate text-xs ${card.state === "skipped" ? "text-gray-500" : "text-gray-200"}`}>
          {toolLabel(card.tool)}
          <span className="mx-1.5 text-gray-700">·</span>
          <span className="text-gray-400">{card.message}</span>
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {card.elapsedMs !== null && (
            <span className="flex items-center gap-1 font-mono text-[10px] text-gray-500">
              <Clock className="w-3 h-3" />
              {formatDuration(card.elapsedMs)}
            </span>
          )}
          {card.state === "running" && (
            <span className="animate-pulse font-mono text-[10px] text-pink-300">running</span>
          )}
          {open ? <ChevronDown className="w-3.5 h-3.5 text-gray-600" /> : <ChevronRight className="w-3.5 h-3.5 text-gray-600" />}
        </span>
      </button>

      {open && (
        <div id={bodyId} className="border-t border-gray-800 px-3 pb-3 pt-2">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-600">Arguments</p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <ArgChips args={card.args} />
          </div>

          {(card.preview.length > 0 || card.previewMore > 0) && (
            <>
              <p className="mt-3 text-[10px] font-semibold uppercase tracking-wide text-gray-600">
                Found
              </p>
              <ul className="mt-1 space-y-0.5">
                {card.preview.map((line) => (
                  <li key={line} className="truncate font-mono text-[11px] text-gray-300" title={line}>
                    {line}
                  </li>
                ))}
                {card.previewMore > 0 && (
                  <li className="font-mono text-[10px] text-gray-600">+{card.previewMore} more</li>
                )}
              </ul>
            </>
          )}

          <FieldGrid fields={card.fields} />

          {card.tool === "create_pr" && typeof card.fields.gh_command === "string" && (
            <p className="mt-2 break-all rounded bg-gray-950 px-2 py-1.5 font-mono text-[10px] text-gray-400">
              {card.fields.gh_command}
            </p>
          )}
          {card.fields.url && (
            <a
              href={String(card.fields.url)}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-flex items-center gap-1 text-[11px] text-violet-300 underline hover:text-violet-200"
            >
              <GitPullRequest className="h-3 w-3" /> open pull request
            </a>
          )}
        </div>
      )}
    </li>
  );
}

export function PlanStrip({ rows }: { rows: PlanRow[] }) {
  if (!rows.length) return null;
  return (
    <ol
      aria-label="Agent plan"
      className="flex flex-wrap items-center gap-x-1.5 gap-y-1 rounded-lg border border-gray-800 bg-gray-900/60 px-3 py-2"
    >
      <li className="mr-1 text-[10px] font-semibold uppercase tracking-wide text-gray-600">Plan</li>
      {rows.map((row) => (
        <li key={row.id} className="flex items-center gap-1.5">
          <PlanDot state={row.state} />
          <span
            className={`font-mono text-[10.5px] ${
              row.state === "queued" ? "text-gray-600" : row.state === "failed" ? "text-red-300" : "text-gray-300"
            }`}
            title={`${row.tool} — ${row.state}`}
          >
            {toolLabel(row.tool)}
            {row.cards > 1 && <span className="text-gray-600"> ×{row.runs}</span>}
          </span>
          {row.order < rows.length - 1 && <span className="text-gray-700">→</span>}
        </li>
      ))}
    </ol>
  );
}

function PlanDot({ state }: { state: PlanRow["state"] }) {
  const cls = "h-1.5 w-1.5 shrink-0 rounded-full";
  if (state === "running") return <span className={`${cls} animate-pulse bg-pink-500`} />;
  if (state === "done") return <span className={`${cls} bg-emerald-500`} />;
  if (state === "failed") return <span className={`${cls} bg-red-500`} />;
  if (state === "skipped") return <span className={`${cls} bg-gray-600`} />;
  return <span className={`${cls} bg-gray-700`} />;
}

function ReasoningRow({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const long = text.length > 132;
  return (
    <li className="border-l-2 border-gray-700/70 pl-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        disabled={!long}
        className="flex items-start gap-2 py-0.5 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-pink-500/60 disabled:cursor-default"
      >
        <Brain className="mt-0.5 h-3 w-3 shrink-0 text-gray-600" />
        <span className={`text-[11px] leading-relaxed text-gray-500 ${open ? "" : "line-clamp-2"}`}>
          {text}
        </span>
        {long && <span className="mt-0.5 shrink-0 text-[10px] text-gray-600">{open ? "less" : "more"}</span>}
      </button>
    </li>
  );
}

export function AgentTimeline({ items, cards }: { items: TranscriptItem[]; cards: Record<string, ToolCard> }) {
  if (!items.length) return null;
  let cardIndex = 0;

  return (
    <ol aria-label="Agent transcript" className="space-y-1.5">
      {items.map((item) => {
        if (item.kind === "reasoning") return <ReasoningRow key={item.id} text={item.text} />;
        if (item.kind === "notice") {
          const tone =
            item.tone === "error" ? "text-red-300" : item.tone === "warn" ? "text-amber-300" : "text-gray-500";
          return (
            <li key={item.id} className={`flex items-center gap-2 px-1 text-[11px] ${tone}`}>
              {item.tone === "error" && <AlertTriangle className="h-3 w-3 shrink-0" />}
              {item.text}
            </li>
          );
        }
        const card = cards[item.stepId];
        if (!card) return null;
        cardIndex += 1;
        return <ToolCardRow key={item.id} card={card} index={cardIndex} />;
      })}
    </ol>
  );
}

export default AgentTimeline;
