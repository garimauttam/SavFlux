/**
 * ReviewPanel.tsx — Unified code review panel.
 *
 * Input tabs:
 *   "From repo"  — folder-tree checkbox picker; 1 file = single review,
 *                  2+ files = multi-file review with combined summary
 *   "Paste code" — paste raw code, always single review
 *   "PR Diff"    — unified diff → impact + inline comments (+ optional AI review)
 *
 * The Single / Multi distinction is invisible to the user — they just
 * pick files and click Run. The panel routes internally.
 */

import { useState, useMemo, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import {
  Loader2,
  ChevronDown,
  ChevronUp,
  RotateCcw,
  Zap,
  FileCode,
  ClipboardPaste,
  Download,
  Files,
  FolderOpen,
  Folder,
  CheckSquare,
  Square,
  Minus,
  Activity,
  BrainCircuit,
  Search,
  BarChart3,
  ListTree,
  Clock3,
  CheckCircle2,
  GitPullRequest,
} from "lucide-react";
import { IndexedFile } from "../types";
import { PRReviewPanel } from "./PRReviewPanel";
import { useReview } from "../hooks/useReview";
import { useMultiReview, ReviewSection } from "../hooks/useMultiReview";

interface ReviewPanelProps {
  indexedFiles: IndexedFile[];
  /** When set, pre-selects this file source on mount (used for graph→review navigation). */
  initialSelectedSource?: string | null;
  /** Called after the initial selection has been applied, so the parent can clear it. */
  onInitialSourceConsumed?: () => void;
}

// ── Language → icon colour ────────────────────────────────────────────────────
const LANG_COLOR: Record<string, string> = {
  py:   "text-blue-400",
  js:   "text-yellow-300",
  ts:   "text-blue-300",
  jsx:  "text-yellow-300",
  tsx:  "text-blue-300",
  go:   "text-cyan-400",
  java: "text-orange-400",
  rs:   "text-orange-300",
  rb:   "text-red-400",
  md:   "text-gray-400",
  json: "text-green-400",
  yaml: "text-green-300",
  yml:  "text-green-300",
  txt:  "text-gray-500",
};

// Map tool names to friendly labels for the reasoning trace UI
const TOOL_LABELS: Record<string, string> = {
  get_function_list:           "Mapped structure",
  count_complexity_indicators: "Measured complexity",
  search_pattern:              "Searched for pattern",
};

interface ActivityStep {
  tool?: string;
  message: string;
  step?: string;
  file?: string;
  index?: number;
  total?: number;
  mode?: string;
}

function activityIcon(step: ActivityStep) {
  if (step.tool === "get_function_list") return <ListTree className="w-3.5 h-3.5 text-blue-300" />;
  if (step.tool === "count_complexity_indicators") return <BarChart3 className="w-3.5 h-3.5 text-green-300" />;
  if (step.tool === "search_pattern") return <Search className="w-3.5 h-3.5 text-purple-300" />;
  if (step.step === "complete") return <CheckCircle2 className="w-3.5 h-3.5 text-emerald-300" />;
  if (step.step === "writing") return <BrainCircuit className="w-3.5 h-3.5 text-yellow-300" />;
  return <Activity className="w-3.5 h-3.5 text-gray-400" />;
}

function activityLabel(step: ActivityStep) {
  if (step.tool) return TOOL_LABELS[step.tool] ?? step.tool;
  if (step.step === "file") return step.file ? `Queued ${step.file}` : "Queued file";
  if (step.step === "complete") return step.file ? `Finished ${step.file}` : "Finished file";
  if (step.step === "summary_complete") return "Summary ready";
  if (step.step === "summary") return "Synthesizing summary";
  if (step.step === "writing") return step.mode === "fast" ? "Writing fast review" : "Writing review";
  if (step.step === "starting") return step.mode === "fast" ? "Fast scan started" : "Review started";
  return step.message;
}

function useElapsed(active: boolean) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const id = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, [active]);

  const minutes = Math.floor(elapsed / 60);
  const seconds = elapsed % 60;
  return minutes > 0 ? `${minutes}m ${seconds.toString().padStart(2, "0")}s` : `${seconds}s`;
}

function ReviewActivityPanel({
  isActive,
  currentStep,
  steps,
  completed,
  total,
  currentFile,
  mode,
  reviewAccuracy,
  llmReviewedCount,
}: {
  isActive: boolean;
  currentStep: string | null;
  steps: ActivityStep[];
  completed: number;
  total: number;
  currentFile?: string | null;
  mode?: string | null;
  reviewAccuracy?: number;
  llmReviewedCount?: number;
}) {
  const elapsed = useElapsed(isActive);
  const percent = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  const visibleSteps = steps.slice(-8).reverse();
  const finishedWithGaps = !isActive && total > 0 && completed < total;
  const title = isActive ? "Review in progress" : finishedWithGaps ? "Review finished with gaps" : "Review complete";

  return (
    <div className="border border-gray-700 bg-gray-900 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-semibold text-white">
              {isActive ? (
                <Loader2 className="w-4 h-4 animate-spin text-yellow-400" />
              ) : finishedWithGaps ? (
                <Activity className="w-4 h-4 text-yellow-400" />
              ) : (
                <CheckCircle2 className="w-4 h-4 text-emerald-400" />
              )}
              <span>{title}</span>
              {mode && <span className="rounded bg-yellow-500/10 px-2 py-0.5 text-[10px] font-medium text-yellow-300">{mode}</span>}
            </div>
            <p className="mt-1 truncate text-xs text-gray-400">
              {currentStep ?? (isActive ? "Preparing review..." : "Finished")}
            </p>
            {currentFile && (
              <p className="mt-1 truncate text-[11px] text-gray-500">{currentFile}</p>
            )}
          </div>
          <div className="shrink-0 text-right text-xs text-gray-400">
            <div className="flex items-center justify-end gap-1">
              <Clock3 className="w-3.5 h-3.5" />
              {elapsed}
            </div>
            {total > 0 && <div className="mt-1 font-mono">{completed}/{total}</div>}
            {!isActive && total > 1 && (
              <div
                className={`mt-1 text-[11px] font-medium ${
                  (reviewAccuracy ?? 0) >= 70
                    ? "text-emerald-400"
                    : (reviewAccuracy ?? 0) >= 30
                    ? "text-yellow-400"
                    : "text-gray-500"
                }`}
                title={
                  (reviewAccuracy ?? 0) === 0
                    ? `All ${total} files used deterministic static analysis — no LLM provider available. Configure Ollama (ollama serve) for deeper reviews.`
                    : `${llmReviewedCount ?? 0}/${total} files had a full LLM review; ${total - (llmReviewedCount ?? 0)} used static analysis only`
                }
              >
                {(reviewAccuracy ?? 0) === 0
                  ? "⚙️ Static analysis only"
                  : `🤖 ${llmReviewedCount ?? 0}/${total} LLM · ${(reviewAccuracy ?? 0)}%`}
              </div>
            )}
          </div>
        </div>
        {total > 0 && (
          <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-gray-800">
            <div
              className="h-full rounded-full bg-yellow-400 transition-all duration-500"
              style={{ width: `${isActive ? Math.max(percent, 5) : 100}%` }}
            />
          </div>
        )}
      </div>

      <div className="max-h-56 overflow-y-auto px-3 py-2">
        {visibleSteps.length === 0 ? (
          <div className="flex items-center gap-2 px-1 py-2 text-xs text-gray-500">
            <Activity className="w-3.5 h-3.5" />
            Waiting for the first streamed update...
          </div>
        ) : (
          <div className="space-y-1.5">
            {visibleSteps.map((step, i) => (
              <div key={`${step.message}-${i}`} className="flex items-start gap-2 rounded-md px-1.5 py-1.5 text-xs text-gray-400">
                <span className="mt-0.5 shrink-0">{activityIcon(step)}</span>
                <div className="min-w-0">
                  <div className="truncate text-gray-300">{activityLabel(step)}</div>
                  {step.message && step.message !== activityLabel(step) && (
                    <div className="truncate text-[11px] text-gray-500">{step.message}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function sectionStatusStyle(status: ReviewSection["status"]) {
  if (status === "complete") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  if (status === "error") return "border-red-500/30 bg-red-500/10 text-red-300";
  if (status === "skipped") return "border-gray-500/30 bg-gray-500/10 text-gray-300";
  if (status === "writing") return "border-yellow-500/30 bg-yellow-500/10 text-yellow-300";
  if (status === "reviewing" || status === "scanning") return "border-blue-500/30 bg-blue-500/10 text-blue-300";
  return "border-gray-600 bg-gray-800 text-gray-400";
}

function SectionStatusBadge({ section }: { section: ReviewSection }) {
  const label =
    section.status === "complete" ? "Complete" :
    section.status === "error" ? "Error" :
    section.status === "skipped" ? "No output" :
    section.status === "writing" ? "Writing" :
    section.status === "scanning" ? "Scanning" :
    section.status === "reviewing" ? "Reviewing" :
    "Pending";

  return (
    <span className={`inline-flex items-center gap-1 rounded border px-2 py-0.5 text-[10px] font-medium ${sectionStatusStyle(section.status)}`}>
      {section.status === "complete" ? (
        <CheckCircle2 className="w-3 h-3" />
      ) : section.status === "error" ? (
        <Activity className="w-3 h-3" />
      ) : section.status === "skipped" ? (
        <Activity className="w-3 h-3" />
      ) : section.status === "pending" ? (
        <Clock3 className="w-3 h-3" />
      ) : (
        <Loader2 className="w-3 h-3 animate-spin" />
      )}
      {label}
    </span>
  );
}

// ── Folder-tree builder ───────────────────────────────────────────────────────
interface TreeFile {
  file: IndexedFile;
  relPath: string;   // e.g. "src/utils/auth.py"
  dirParts: string[]; // e.g. ["src", "utils"]
  name: string;      // e.g. "auth.py"
}

interface FolderNode {
  path: string;          // full relative folder path, e.g. "src/utils"
  name: string;          // last segment, e.g. "utils"
  children: FolderNode[];
  files: TreeFile[];
}

/** Extract a relative path from the absolute source by stripping the common prefix. */
function buildRelPath(source: string, commonPrefix: string): string {
  let rel = source.startsWith(commonPrefix) ? source.slice(commonPrefix.length) : source;
  rel = rel.replace(/^\/+/, "");
  return rel;
}

function computeCommonPrefix(sources: string[]): string {
  if (sources.length === 0) return "";
  let prefix = sources[0];
  for (const s of sources.slice(1)) {
    while (!s.startsWith(prefix)) {
      prefix = prefix.slice(0, prefix.lastIndexOf("/"));
      if (!prefix) return "";
    }
  }
  // Trim to the last slash so we don't cut a directory name in half
  const lastSlash = prefix.lastIndexOf("/");
  return lastSlash >= 0 ? prefix.slice(0, lastSlash + 1) : prefix + "/";
}

/** Derive a human-readable repo label from a GitHub URL or file path.
 *  "https://github.com/owner/My-Repo" → "My-Repo"
 *  "/var/folders/.../tmpXXX"          → "" (falls through to caller default)
 */
function repoLabel(repoUrl: string): string {
  if (!repoUrl) return "";
  // Strip trailing slash, then take the last path segment
  const trimmed = repoUrl.replace(/\/$/, "");
  const last = trimmed.split("/").pop() ?? "";
  // Filter out temp-dir-like names: pure hex/alphanumeric 8+ chars with no dots
  if (/^tmp[a-z0-9]{6,}$/i.test(last)) return "";
  return last;
}

function buildTree(files: IndexedFile[]): FolderNode {
  // Derive a display name for the root from the repo URL stored in metadata.
  // Falls back to the last non-temp segment of the common path prefix.
  const firstRepoUrl = files[0]?.repo_url ?? "";
  const label = repoLabel(firstRepoUrl);

  const root: FolderNode = { path: "", name: label, children: [], files: [] };
  let commonPrefix = computeCommonPrefix(files.map((f) => f.source));

  // After stripping the common prefix, if every remaining path starts with a
  // temp-dir segment (e.g. "tmpfk4eq1jx/..."), extend the prefix to swallow it.
  // This happens when repos are indexed across multiple clone sessions so the
  // common prefix only reaches "/tmp/" — leaving the tmpXXX dir visible in the tree.
  const relPaths = files.map((f) => buildRelPath(f.source, commonPrefix));
  const firstSegments = relPaths.map((r) => r.split("/")[0]);
  const allSameTmpSeg =
    firstSegments.length > 0 &&
    firstSegments.every((s) => /^tmp[a-z0-9]{6,}$/i.test(s)) &&
    new Set(firstSegments).size === 1;
  if (allSameTmpSeg) {
    commonPrefix = commonPrefix + firstSegments[0] + "/";
  }

  const treeFiles: TreeFile[] = files.map((f) => {
    const relPath = buildRelPath(f.source, commonPrefix);
    const parts = relPath.split("/");
    return {
      file: f,
      relPath,
      dirParts: parts.slice(0, -1),
      name: parts[parts.length - 1] || f.file_name,
    };
  });

  // Insert each file into the tree
  for (const tf of treeFiles) {
    let node = root;
    let accPath = "";
    for (const part of tf.dirParts) {
      accPath = accPath ? `${accPath}/${part}` : part;
      let child = node.children.find((c) => c.path === accPath);
      if (!child) {
        child = { path: accPath, name: part, children: [], files: [] };
        node.children.push(child);
      }
      node = child;
    }
    node.files.push(tf);
  }

  return root;
}

// ── Section card (multi-review output) ───────────────────────────────────────
// ── Rich markdown components for review cards ────────────────────────────────
// Defined outside SectionCard so they're never re-created on each render.
const reviewMdComponents: React.ComponentProps<typeof ReactMarkdown>["components"] = {
  h1: ({ children }) => (
    <h1 className="text-base font-bold text-white mt-4 mb-2 pb-1 border-b border-gray-600 leading-snug">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="text-sm font-bold text-white mt-4 mb-1.5 leading-snug">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="text-sm font-semibold text-gray-200 mt-3 mb-1 leading-snug">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="text-xs font-semibold text-gray-300 mt-2 mb-0.5">{children}</h4>
  ),
  p: ({ children }) => (
    <p className="text-sm text-gray-200 leading-relaxed my-1.5">{children}</p>
  ),
  ul: ({ children }) => (
    <ul className="list-disc list-outside pl-5 my-2 space-y-1 text-sm text-gray-200">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="list-decimal list-outside pl-5 my-2 space-y-1 text-sm text-gray-200">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  code({ className, children }: any) {
    const match = /language-(\w+)/.exec(className || "");
    if (match) {
      return (
        <div className="my-2 rounded-lg overflow-hidden border border-gray-700">
          <div className="px-3 py-1 bg-gray-900 border-b border-gray-700 text-xs text-gray-500 font-mono">{match[1]}</div>
          <SyntaxHighlighter
            style={vscDarkPlus}
            language={match[1]}
            PreTag="div"
            customStyle={{ margin: 0, borderRadius: 0, fontSize: "12px", background: "#0d1117" }}
          >
            {String(children).replace(/\n$/, "")}
          </SyntaxHighlighter>
        </div>
      );
    }
    return (
      <code className="bg-gray-700/70 text-purple-300 px-1.5 py-0.5 rounded text-xs font-mono">{children}</code>
    );
  },
  blockquote: ({ children }) => (
    <blockquote className="border-l-4 border-purple-500 pl-3 my-2 text-gray-400 italic text-sm">{children}</blockquote>
  ),
  hr: () => <hr className="border-gray-700 my-3" />,
  table: ({ children }) => (
    <div className="overflow-x-auto my-2 rounded-lg border border-gray-700">
      <table className="w-full text-sm border-collapse">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-gray-800 text-gray-300 text-xs uppercase">{children}</thead>,
  tbody: ({ children }) => <tbody className="divide-y divide-gray-700">{children}</tbody>,
  tr: ({ children }) => <tr className="hover:bg-gray-800/40">{children}</tr>,
  th: ({ children }) => <th className="px-3 py-2 text-left font-semibold text-gray-300">{children}</th>,
  td: ({ children }) => <td className="px-3 py-2 text-gray-200">{children}</td>,
  strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
  em: ({ children }) => <em className="italic text-gray-300">{children}</em>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer"
       className="text-blue-400 underline underline-offset-2 hover:text-blue-300 transition-colors">
      {children}
    </a>
  ),
};

function SectionCard({ section, defaultOpen }: { section: ReviewSection; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const isSummary = section.fileName.includes("Summary");
  const hasContent = section.content.trim().length > 0;

  return (
    <div className={`border rounded-xl overflow-hidden ${
      isSummary ? "border-yellow-700/40 bg-yellow-900/5" : "border-gray-700 bg-gray-900"
    }`}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-4 py-3 text-xs text-gray-300 hover:text-white transition-colors"
      >
        <span className="font-semibold flex items-center gap-2">
          {isSummary
            ? <span className="text-yellow-400">📊</span>
            : <FileCode className="w-3.5 h-3.5 text-purple-400" />
          }
          <span className="truncate">{section.fileName}</span>
        </span>
        <span className="ml-3 flex shrink-0 items-center gap-2">
          <SectionStatusBadge section={section} />
          {open ? <ChevronUp className="w-3.5 h-3.5 text-gray-500" /> : <ChevronDown className="w-3.5 h-3.5 text-gray-500" />}
        </span>
      </button>
      {open && (
        <div className="border-t border-gray-700/50 px-4 py-3">
          {hasContent ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={reviewMdComponents}>
              {section.content}
            </ReactMarkdown>
          ) : (
            <div className="space-y-3 py-1">
              <div className="flex items-center gap-2 text-sm text-gray-300">
                {section.status === "skipped" ? (
                  <Activity className="w-4 h-4 text-gray-400" />
                ) : (
                  <Loader2 className="w-4 h-4 animate-spin text-yellow-400" />
                )}
                {section.statusMessage ?? "Review pending..."}
              </div>
              {section.status !== "skipped" && (
                <div className="space-y-2">
                  <div className="h-2 w-11/12 animate-pulse rounded bg-gray-800" />
                  <div className="h-2 w-8/12 animate-pulse rounded bg-gray-800" />
                  <div className="h-2 w-10/12 animate-pulse rounded bg-gray-800" />
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Folder-tree node ──────────────────────────────────────────────────────────
function FolderTreeNode({
  node,
  depth,
  selected,
  onToggleFile,
  onToggleFolder,
  defaultOpen,
}: {
  node: FolderNode;
  depth: number;
  selected: Set<string>;
  onToggleFile: (source: string) => void;
  onToggleFolder: (sources: string[]) => void;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  // Collect all file sources under this node (recursively)
  function allSources(n: FolderNode): string[] {
    const direct = n.files.map((tf) => tf.file.source);
    const nested = n.children.flatMap(allSources);
    return [...direct, ...nested];
  }

  const sources = allSources(node);
  const checkedCount = sources.filter((s) => selected.has(s)).length;
  const allChecked  = checkedCount === sources.length && sources.length > 0;
  const someChecked = checkedCount > 0 && !allChecked;

  const indent = depth * 12; // px

  return (
    <div>
      {/* Folder row — only rendered for non-root nodes */}
      {node.name && (
        <div
          className="flex items-center gap-1.5 py-1 px-1 rounded hover:bg-gray-800/60 group cursor-pointer"
          style={{ paddingLeft: `${indent}px` }}
        >
          {/* Folder select checkbox */}
          <button
            onClick={(e) => { e.stopPropagation(); onToggleFolder(sources); }}
            className="shrink-0 flex items-center justify-center w-4 h-4 text-gray-500 hover:text-yellow-400 transition-colors"
            title={allChecked ? "Deselect folder" : "Select folder"}
          >
            {allChecked
              ? <CheckSquare className="w-3.5 h-3.5 text-yellow-400" />
              : someChecked
                ? <Minus className="w-3.5 h-3.5 text-yellow-500/60" />
                : <Square className="w-3.5 h-3.5" />
            }
          </button>

          {/* Expand/collapse + folder icon */}
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-1.5 flex-1 min-w-0 text-left"
          >
            {open
              ? <FolderOpen className="w-3.5 h-3.5 shrink-0 text-yellow-400/80" />
              : <Folder     className="w-3.5 h-3.5 shrink-0 text-yellow-400/60" />
            }
            <span className="text-xs font-medium text-gray-300 truncate">{node.name}</span>
            <span className="ml-auto shrink-0 text-[10px] text-gray-600 font-mono pr-1">
              {sources.length}
            </span>
            {open
              ? <ChevronUp   className="w-3 h-3 shrink-0 text-gray-600" />
              : <ChevronDown className="w-3 h-3 shrink-0 text-gray-600" />
            }
          </button>
        </div>
      )}

      {/* Children — visible when open (or root node always visible) */}
      {(open || !node.name) && (
        <>
          {/* Nested folders */}
          {node.children.map((child) => (
            <FolderTreeNode
              key={child.path}
              node={child}
              depth={depth + (node.name ? 1 : 0)}
              selected={selected}
              onToggleFile={onToggleFile}
              onToggleFolder={onToggleFolder}
              defaultOpen={node.children.length <= 3}
            />
          ))}

          {/* Files in this folder */}
          {node.files.map((tf) => {
            const checked = selected.has(tf.file.source);
            const langColor = LANG_COLOR[tf.file.language] ?? "text-gray-400";
            const fileIndent = (depth + (node.name ? 1 : 0)) * 12;
            return (
              <button
                key={tf.file.source}
                onClick={() => onToggleFile(tf.file.source)}
                style={{ paddingLeft: `${fileIndent + 4}px` }}
                className={`w-full text-left flex items-center gap-2 pr-2 py-1 rounded text-xs transition-colors group ${
                  checked
                    ? "bg-yellow-700/15 text-yellow-300"
                    : "text-gray-400 hover:bg-gray-800/60 hover:text-gray-200"
                }`}
              >
                <span className="shrink-0">
                  {checked
                    ? <CheckSquare className="w-3.5 h-3.5 text-yellow-400" />
                    : <Square className="w-3.5 h-3.5 text-gray-600 group-hover:text-gray-400" />
                  }
                </span>
                <FileCode className={`w-3 h-3 shrink-0 ${langColor}`} />
                <span className="truncate" title={tf.relPath}>{tf.name}</span>
                <span className={`ml-auto shrink-0 font-mono text-[10px] ${langColor} opacity-70`}>
                  {tf.file.language}
                </span>
              </button>
            );
          })}
        </>
      )}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────
export function ReviewPanel({ indexedFiles, initialSelectedSource, onInitialSourceConsumed }: ReviewPanelProps) {
  const single = useReview();
  const multi  = useMultiReview();

  // Input tab: "file" (from repo tree) or "paste"
  const [tab, setTab] = useState<"file" | "paste" | "pr">("file");

  // File selection
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Paste mode
  const [pastedCode, setPastedCode]     = useState("");
  const [pasteLanguage, setPasteLanguage] = useState("python");
  const [pasteName, setPasteName]       = useState("snippet.py");

  // UI
  const [traceExpanded, setTraceExpanded] = useState(true);
  const outputRef = useRef<HTMLDivElement | null>(null);
  const outputEndRef = useRef<HTMLDivElement | null>(null);

  // ── Graph → Review navigation: pre-select the source the user double-clicked
  useEffect(() => {
    if (initialSelectedSource && indexedFiles.some((f) => f.source === initialSelectedSource)) {
      setSelected(new Set([initialSelectedSource]));
      setTab("file");
      onInitialSourceConsumed?.();
    }
  // Run once when initialSelectedSource arrives — indexedFiles and callbacks are stable
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSelectedSource]);

  // ── Build folder tree ─────────────────────────────────────────────────────
  const tree = useMemo(() => buildTree(indexedFiles), [indexedFiles]);
  const allSelected = indexedFiles.length > 0 && selected.size === indexedFiles.length;
  const someSelected = selected.size > 0 && !allSelected;

  // ── Selection helpers ─────────────────────────────────────────────────────
  const toggleFile = (source: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(source) ? next.delete(source) : next.add(source);
      return next;
    });
  };

  const toggleFolder = (sources: string[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const allIn = sources.every((s) => next.has(s));
      if (allIn) sources.forEach((s) => next.delete(s));
      else       sources.forEach((s) => next.add(s));
      return next;
    });
  };

  const toggleAll = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(indexedFiles.map((f) => f.source)));
  };

  // ── Run review ────────────────────────────────────────────────────────────
  const isMulti = selected.size > 1;

  const handleReview = () => {
    if (tab === "paste" && pastedCode.trim()) {
      single.reviewPaste(pastedCode, pasteLanguage, pasteName);
      return;
    }
    if (selected.size === 0) return;
    const files = indexedFiles.filter((f) => selected.has(f.source));
    if (files.length === 1) {
      single.reviewFile(files[0]);
    } else {
      multi.reviewFiles(files);
    }
  };

  // ── Download ──────────────────────────────────────────────────────────────
  const handleDownload = () => {
    if (isMulti && multi.sections.length > 0) {
      const orderedSections = [...multi.sections].sort((a, b) => {
        const aSummary = a.fileName.includes("Summary");
        const bSummary = b.fileName.includes("Summary");
        return aSummary === bSummary ? 0 : aSummary ? -1 : 1;
      });
      const content = orderedSections.map((s) => `## ${s.fileName}\n\n${s.content}`).join("\n\n---\n\n");
      triggerDownload(content, "codesage-multi-review.md");
    } else if (single.review) {
      const name = indexedFiles.find((f) => selected.has(f.source))?.file_name ?? pasteName ?? "review";
      triggerDownload(single.review, `codesage-review-${name}.md`);
    }
  };

  const triggerDownload = (content: string, filename: string) => {
    const blob = new Blob([content], { type: "text/markdown" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  };

  // ── Derived state ─────────────────────────────────────────────────────────
  const isActive = isMulti ? multi.isReviewing : single.isReviewing;

  const canReview = !isActive && (
    tab === "paste"
      ? pastedCode.trim().length > 0
      : selected.size > 0
  );

  const hasOutput = isMulti ? multi.sections.length > 0 : !!single.review;
  const activeError   = isMulti ? multi.error   : single.error;
  const currentStep   = isMulti ? multi.currentStep : single.currentStep;
  const singleToolSteps = single.agentSteps.filter((step): step is ActivityStep & { tool: string } => Boolean(step.tool));
  const orderedMultiSections = useMemo(() => (
    [...multi.sections].sort((a, b) => {
      const aSummary = a.fileName.includes("Summary");
      const bSummary = b.fileName.includes("Summary");
      return aSummary === bSummary ? 0 : aSummary ? -1 : 1;
    })
  ), [multi.sections]);

  useEffect(() => {
    if (!isActive) return;
    outputEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [
    isActive,
    single.review.length,
    single.agentSteps.length,
    multi.sections.length,
    multi.sections.map((section) => section.content.length).join(","),
    multi.agentSteps.length,
  ]);

  const buttonLabel = () => {
    if (isActive) return isMulti ? "Reviewing..." : "Agent running...";
    if (tab === "paste") return "Run Code Review";
    if (selected.size === 0) return "Run Code Review";
    return selected.size === 1 ? "Review 1 file" : `Review ${selected.size} files`;
  };

  return (
    <div className="flex flex-col h-full">

      {/* ── Header ── */}
      <div className="px-6 py-3 border-b border-gray-700 bg-gray-900 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Zap className="w-4 h-4 text-yellow-400" />
            Code Review Agent
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Select files from your repo, or paste code — AI reviews them autonomously
          </p>
        </div>
        {hasOutput && (
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-blue-400 transition-colors"
              title="Download as .md"
            >
              <Download className="w-3.5 h-3.5" />
              Download
            </button>
            <button
              onClick={() => { single.reset(); multi.reset(); setSelected(new Set()); }}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              New review
            </button>
          </div>
        )}
      </div>

      {/* ── Body ── */}
      {tab === "pr" ? (
        <div className="flex flex-1 overflow-hidden">
          <PRReviewPanel />
        </div>
      ) : (
      <div className="flex flex-1 overflow-hidden">

        {/* Left: input panel */}
        <div className="w-72 shrink-0 border-r border-gray-700 flex flex-col bg-gray-900">

          {/* Tabs: From repo | Paste code */}
          <div className="flex border-b border-gray-700">
            {(["file", "paste", "pr"] as const).map((t) => (
              <button
                key={t}
                onClick={() => { setTab(t); single.reset(); multi.reset(); }}
                className={`flex-1 flex items-center justify-center gap-1.5 py-2.5 text-xs font-medium transition-colors ${
                  tab === t
                    ? "text-yellow-400 border-b-2 border-yellow-400"
                    : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {t === "file"
                  ? <Files className="w-3.5 h-3.5" />
                  : t === "paste"
                    ? <ClipboardPaste className="w-3.5 h-3.5" />
                    : <GitPullRequest className="w-3.5 h-3.5" />
                }
                {t === "file" ? "From repo" : t === "paste" ? "Paste code" : "PR Diff"}
              </button>
            ))}
          </div>

          {/* ── FROM REPO: folder tree ── */}
          {tab === "file" && (
            <div className="flex flex-col flex-1 overflow-hidden">
              {indexedFiles.length === 0 ? (
                <div className="flex-1 flex items-center justify-center p-6">
                  <p className="text-xs text-gray-600 text-center">
                    No files indexed yet.<br />Add a repo in the sidebar first.
                  </p>
                </div>
              ) : (
                <>
                  {/* Select all row */}
                  <div className="px-3 pt-3 pb-2 border-b border-gray-800">
                    <button
                      onClick={toggleAll}
                      className={`w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs font-medium border transition-colors ${
                        allSelected
                          ? "border-yellow-600/50 bg-yellow-700/10 text-yellow-300 hover:bg-yellow-700/20"
                          : "border-gray-700 text-gray-400 hover:border-yellow-600/40 hover:text-yellow-300"
                      }`}
                    >
                      {allSelected
                        ? <CheckSquare className="w-3.5 h-3.5 text-yellow-400 shrink-0" />
                        : someSelected
                          ? <Minus className="w-3.5 h-3.5 text-yellow-500/60 shrink-0" />
                          : <Square className="w-3.5 h-3.5 shrink-0" />
                      }
                      <span>
                        {allSelected ? "Deselect all" : "Select entire repo"}
                      </span>
                      <span className="ml-auto font-mono text-gray-500 text-[10px]">
                        {indexedFiles.length} files
                      </span>
                    </button>

                    {selected.size > 0 && (
                      <p className="mt-1.5 text-[11px] text-yellow-500/70 px-1">
                        {selected.size} file{selected.size !== 1 ? "s" : ""} selected
                        {selected.size === 1 ? " — single review" : " — combined review"}
                      </p>
                    )}
                  </div>

                  {/* Tree */}
                  <div className="flex-1 overflow-y-auto py-2 px-2">
                    <FolderTreeNode
                      node={tree}
                      depth={0}
                      selected={selected}
                      onToggleFile={toggleFile}
                      onToggleFolder={toggleFolder}
                      defaultOpen={true}
                    />
                  </div>
                </>
              )}
            </div>
          )}

          {/* ── PASTE CODE ── */}
          {tab === "paste" && (
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              <div>
                <label className="block text-xs text-gray-400 mb-1">File name</label>
                <input type="text" value={pasteName} onChange={(e) => setPasteName(e.target.value)}
                  className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-yellow-500"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Language</label>
                <select value={pasteLanguage} onChange={(e) => setPasteLanguage(e.target.value)}
                  className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-yellow-500"
                >
                  {["python","javascript","typescript","go","java","rust","cpp"].map((l) => (
                    <option key={l} value={l}>{l}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Code</label>
                <textarea value={pastedCode} onChange={(e) => setPastedCode(e.target.value)}
                  placeholder="Paste your code here..." rows={14}
                  className="w-full bg-gray-800 text-white text-xs font-mono rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-yellow-500 resize-none placeholder-gray-600"
                />
              </div>
            </div>
          )}

          {/* Run button */}
          <div className="p-4 border-t border-gray-700">
            <button
              onClick={handleReview}
              disabled={!canReview}
              className="w-full flex items-center justify-center gap-2 bg-yellow-500 hover:bg-yellow-400 disabled:bg-gray-700 disabled:cursor-not-allowed text-gray-900 disabled:text-gray-500 font-semibold text-sm py-2.5 rounded-xl transition-colors"
            >
              {isActive
                ? <><Loader2 className="w-4 h-4 animate-spin" />{buttonLabel()}</>
                : <><Zap className="w-4 h-4" />{buttonLabel()}</>
              }
            </button>
          </div>
        </div>

        {/* ── Right: output ── */}
        <div ref={outputRef} className="flex-1 overflow-y-auto bg-gray-950 flex flex-col">

          {/* Empty state */}
          {!hasOutput && !isActive && !activeError && (
            <div className="flex-1 flex flex-col items-center justify-center text-center p-8 gap-4">
              <div className="w-14 h-14 rounded-2xl bg-yellow-500/10 border border-yellow-500/20 flex items-center justify-center">
                <Zap className="w-7 h-7 text-yellow-400" />
              </div>
              <div>
                <h3 className="text-white font-semibold mb-1">Autonomous Code Review</h3>
                <p className="text-sm text-gray-500 max-w-sm">
                  Select one file for a deep single-file review, or pick multiple files
                  for a combined review with a repo-wide health summary.
                </p>
              </div>
              <div className="grid grid-cols-3 gap-2 text-xs text-gray-500 mt-2">
                {["🗺️ Maps structure", "🔍 Searches patterns", "📊 Measures complexity"].map((s) => (
                  <div key={s} className="bg-gray-900 border border-gray-800 rounded-lg px-3 py-2">{s}</div>
                ))}
              </div>
            </div>
          )}

          {/* Single-file review output */}
          {!isMulti && (single.isReviewing || single.review || single.agentSteps.length > 0) && (
            <div className="p-6 space-y-4">
              {(single.isReviewing || single.agentSteps.length > 0) && (
                <ReviewActivityPanel
                  isActive={single.isReviewing}
                  currentStep={currentStep}
                  steps={single.agentSteps}
                  completed={single.isReviewing ? 0 : 1}
                  total={1}
                  mode={single.currentMode}
                />
              )}
              {singleToolSteps.length > 0 && (
                <div className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
                  <button onClick={() => setTraceExpanded((v) => !v)}
                    className="w-full flex items-center justify-between px-4 py-3 text-xs text-gray-400 hover:text-gray-200 transition-colors"
                  >
                    <span className="font-medium">
                      Tool trace ({singleToolSteps.length} tool{singleToolSteps.length !== 1 ? "s" : ""} called)
                    </span>
                    {traceExpanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                  </button>
                  {traceExpanded && (
                    <div className="px-4 pb-3 space-y-1.5 border-t border-gray-700 pt-3">
                      {singleToolSteps.map((step, i) => {
                        const label = TOOL_LABELS[step.tool] ?? step.tool;
                        return (
                          <div key={i} className="flex items-center gap-2 text-xs text-gray-400">
                            {activityIcon(step)}
                            <span className="text-gray-300 font-medium">{label}</span>
                            <span className="text-gray-600 font-mono">{step.tool}()</span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
              {single.isReviewing && currentStep && (
                <div className="flex items-center gap-2 text-xs text-yellow-400">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  {currentStep}
                </div>
              )}
              {single.review && (
                <div className="prose prose-invert prose-sm max-w-none">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      code({ className, children }: any) {
                        const match = /language-(\w+)/.exec(className || "");
                        return match ? (
                          <SyntaxHighlighter style={vscDarkPlus} language={match[1]} PreTag="div" className="rounded-lg text-xs">
                            {String(children).replace(/\n$/, "")}
                          </SyntaxHighlighter>
                        ) : (
                          <code className="bg-gray-800 text-purple-300 px-1.5 py-0.5 rounded text-xs font-mono">{children}</code>
                        );
                      },
                    }}
                  >
                    {single.review + (single.isReviewing ? " ▋" : "")}
                  </ReactMarkdown>
                </div>
              )}
              {single.error && (
                <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-3">
                  {single.error}
                </div>
              )}
            </div>
          )}

          {/* Multi-file review output */}
          {isMulti && (multi.isReviewing || multi.sections.length > 0) && (
            <div className="p-6 space-y-4">
              {(multi.isReviewing || multi.agentSteps.length > 0) && (
                <ReviewActivityPanel
                    isActive={multi.isReviewing}
                    currentStep={currentStep}
                    steps={multi.agentSteps}
                    completed={multi.completedFiles}
                    total={multi.totalFileCount || multi.totalFiles || selected.size}
                    currentFile={multi.currentFile}
                    mode={multi.currentMode}
                    reviewAccuracy={multi.reviewAccuracy}
                    llmReviewedCount={multi.llmReviewedCount}
                  />
              )}
              {orderedMultiSections.map((section, i) => (
                <SectionCard key={`${section.fileName}-${i}`} section={section} defaultOpen={section.fileName.includes("Summary") || i === 0} />
              ))}
              {multi.error && (
                <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-3">
                  {multi.error}
                </div>
              )}
            </div>
          )}

          {/* Error with no output yet */}
          {!hasOutput && activeError && (
            <div className="p-6">
              <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-3">
                {activeError}
              </div>
            </div>
          )}
          <div ref={outputEndRef} />
        </div>
      </div>
      )}
    </div>
  );
}
