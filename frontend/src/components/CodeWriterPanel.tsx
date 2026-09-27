/**
 * CodeWriterPanel.tsx — Code Writer Agent with three modes.
 *
 * Generate — write a new file from a description, grounded in selected context files
 * Edit     — pick an existing indexed file + describe changes → get a rewritten version
 * Tests    — pick an existing indexed file → get a test suite matching repo style
 *
 * Why three modes?
 *   "Generate" = start from scratch (greenfield)
 *   "Edit"     = most real-world dev work: improve something that already exists
 *   "Tests"    = fastest way to add test coverage to an existing file
 */

import { useState, useCallback, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { diffLines, Change } from "diff";
import {
  Wand2,
  Loader2,
  RotateCcw,
  Download,
  ChevronDown,
  ChevronUp,
  FileCode,
  Pencil,
  FlaskConical,
  FolderOpen,
  CheckSquare,
  Square,
  Copy,
  Check,
  GitCompare,
  Code2,
  History,
} from "lucide-react";
import { IndexedFile } from "../types";
import { useCodeWriter } from "../hooks/useCodeWriter";
import { StopButton } from "./StopButton";
import { apiFetch } from "../api";
import { CodeHighlight } from "../lib/highlight";

interface CodeWriterPanelProps {
  indexedFiles: IndexedFile[];
}

type WriteMode = "generate" | "edit" | "tests";

const LANGUAGES = [
  "python", "typescript", "javascript", "go", "java", "rust",
  "cpp", "c", "ruby", "bash", "sql", "yaml",
];

const EXT_MAP: Record<string, string> = {
  python: "py", typescript: "ts", javascript: "js", go: "go",
  java: "java", rust: "rs", cpp: "cpp", c: "c", ruby: "rb",
  bash: "sh", sql: "sql", yaml: "yaml",
};

const LANG_COLOR: Record<string, string> = {
  py: "text-blue-400", js: "text-yellow-300", ts: "text-blue-300",
  jsx: "text-yellow-300", tsx: "text-blue-300", go: "text-cyan-400",
  java: "text-orange-400", rs: "text-orange-300", rb: "text-red-400",
  md: "text-gray-400", json: "text-green-400", yaml: "text-green-300",
  yml: "text-green-300", txt: "text-gray-500",
};

const STEP_LABELS: Record<string, string> = {
  context:    "📂 Fetching context",
  generating: "✍️  Generating",
  error:      "❌ Error",
};

// ── Copy button ───────────────────────────────────────────────────────────────
// Self-contained so it maintains its own "copied" flash state independently.
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }).catch(() => {
      // Fallback for browsers that block clipboard API (unlikely in modern browsers)
      const el = document.createElement("textarea");
      el.value = text;
      document.body.appendChild(el);
      el.select();
      document.execCommand("copy");
      document.body.removeChild(el);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      title={copied ? "Copied!" : "Copy to clipboard"}
      className={`flex items-center gap-1 text-[10px] font-medium px-2 py-1 rounded transition-all ${
        copied
          ? "text-green-400 bg-green-900/30"
          : "text-gray-500 hover:text-gray-200 hover:bg-gray-700"
      }`}
    >
      {copied
        ? <><Check className="w-3 h-3" />Copied!</>
        : <><Copy className="w-3 h-3" />Copy</>
      }
    </button>
  );
}

// ── Diff view ────────────────────────────────────────────────────────────────
// Renders a line-level before/after diff using the `diff` package.
// Added lines are green, removed lines red, unchanged lines gray.
// This is the core feature that makes the Edit mode look like a real code editor.
function DiffView({ original, updated, language }: { original: string; updated: string; language: string }) {
  const changes: Change[] = diffLines(original, updated);

  // Summary counts for the header badge
  const added   = changes.filter((c) => c.added).reduce((n, c) => n + (c.count ?? 0), 0);
  const removed = changes.filter((c) => c.removed).reduce((n, c) => n + (c.count ?? 0), 0);

  return (
    <div className="rounded-lg overflow-hidden border border-gray-700 my-3 font-mono text-xs">
      {/* Diff header */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-gray-800 border-b border-gray-700">
        <span className="text-[10px] text-gray-500 uppercase tracking-wider">{language} · diff</span>
        <span className="flex items-center gap-2 text-[10px]">
          {added > 0 && (
            <span className="text-green-400 font-semibold">+{added} line{added !== 1 ? "s" : ""}</span>
          )}
          {removed > 0 && (
            <span className="text-red-400 font-semibold">-{removed} line{removed !== 1 ? "s" : ""}</span>
          )}
        </span>
      </div>

      {/* Diff body */}
      <div className="overflow-x-auto bg-gray-950">
        {changes.map((change, idx) => {
          const lines = change.value.replace(/\n$/, "").split("\n");
          const bg      = change.added   ? "bg-green-950/60"  : change.removed ? "bg-red-950/60"  : "";
          const border  = change.added   ? "border-l-2 border-green-500" : change.removed ? "border-l-2 border-red-500" : "border-l-2 border-transparent";
          const text    = change.added   ? "text-green-300"   : change.removed ? "text-red-300"   : "text-gray-400";
          const prefix  = change.added   ? "+"                : change.removed ? "-"               : " ";

          return lines.map((line, li) => (
            <div
              key={`${idx}-${li}`}
              className={`flex items-start ${bg} ${border} ${text} leading-5`}
            >
              {/* Gutter: +/- prefix */}
              <span className="select-none w-5 shrink-0 text-center opacity-60 py-0.5">{prefix}</span>
              {/* Line content with tab-safe pre-wrap */}
              <span className="flex-1 py-0.5 pr-4 whitespace-pre">{line || " "}</span>
            </div>
          ));
        })}
      </div>
    </div>
  );
}

// ── Mode config ───────────────────────────────────────────────────────────────
const MODES: { id: WriteMode; label: string; Icon: React.ElementType; color: string; description: string }[] = [
  {
    id: "generate",
    label: "Generate",
    Icon: Wand2,
    color: "text-green-400",
    description: "Write a new file from a description. Select context files to match your codebase style.",
  },
  {
    id: "edit",
    label: "Edit",
    Icon: Pencil,
    color: "text-blue-400",
    description: "Improve or extend an existing file. Describe the changes — the LLM rewrites the whole file.",
  },
  {
    id: "tests",
    label: "Tests",
    Icon: FlaskConical,
    color: "text-purple-400",
    description: "Generate a test suite for an existing file. Automatically matches your project's test style.",
  },
];

// ── Compact file picker (single-select) ───────────────────────────────────────
function FilePicker({
  files,
  selected,
  onSelect,
  accent,
}: {
  files: IndexedFile[];
  selected: IndexedFile | null;
  onSelect: (f: IndexedFile) => void;
  accent: string;
}) {
  const [open, setOpen] = useState(true);

  // Group by directory (derive from file_name — flat list, no tree needed here)
  return (
    <div className="border border-gray-700 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs bg-gray-800 text-gray-400 hover:text-gray-200"
      >
        <span className="flex items-center gap-1.5 font-medium">
          <FileCode className={`w-3.5 h-3.5 ${accent}`} />
          {selected ? selected.file_name : "Select a file"}
        </span>
        {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
      </button>
      {open && (
        <div className="max-h-48 overflow-y-auto divide-y divide-gray-800">
          {files.map((f) => {
            const isSelected = selected?.source === f.source;
            const langColor = LANG_COLOR[f.language] ?? "text-gray-400";
            return (
              <button
                key={f.source}
                onClick={() => onSelect(f)}
                className={`w-full text-left flex items-center gap-2 px-3 py-1.5 text-xs transition-colors ${
                  isSelected
                    ? `bg-${accent.replace("text-", "")}/10 ${accent} font-medium`
                    : "text-gray-400 hover:bg-gray-800/60 hover:text-gray-200"
                }`}
              >
                <FileCode className={`w-3 h-3 shrink-0 ${langColor}`} />
                <span className="truncate">{f.file_name}</span>
                <span className={`ml-auto shrink-0 text-[10px] font-mono ${langColor} opacity-60`}>{f.language}</span>
                {isSelected && <CheckSquare className={`w-3 h-3 shrink-0 ${accent}`} />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Context file checkbox list (Generate mode) ────────────────────────────────
function ContextPicker({
  files,
  selected,
  onToggle,
  onToggleAll,
}: {
  files: IndexedFile[];
  selected: Set<string>;
  onToggle: (src: string) => void;
  onToggleAll: () => void;
}) {
  const [open, setOpen] = useState(false);
  const allSelected = files.length > 0 && selected.size === files.length;

  return (
    <div className="border border-gray-700 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs bg-gray-800 text-gray-400 hover:text-gray-200"
      >
        <span className="flex items-center gap-1.5 font-medium">
          <FolderOpen className="w-3.5 h-3.5 text-green-400" />
          Context files
          {selected.size > 0 && (
            <span className="bg-green-700/40 text-green-300 text-[10px] px-1.5 py-0.5 rounded-full">
              {selected.size}
            </span>
          )}
        </span>
        {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
      </button>
      {open && (
        <div className="border-t border-gray-700">
          <button
            onClick={onToggleAll}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-gray-500 hover:text-green-300 border-b border-gray-800"
          >
            {allSelected
              ? <CheckSquare className="w-3 h-3 text-green-400" />
              : <Square className="w-3 h-3" />
            }
            <span className="font-medium">{allSelected ? "Deselect all" : `Select all (${files.length})`}</span>
          </button>
          <div className="max-h-40 overflow-y-auto">
            {files.map((f) => {
              const checked = selected.has(f.source);
              const langColor = LANG_COLOR[f.language] ?? "text-gray-400";
              return (
                <button
                  key={f.source}
                  onClick={() => onToggle(f.source)}
                  className={`w-full text-left flex items-center gap-2 px-3 py-1.5 text-xs transition-colors ${
                    checked ? "bg-green-700/10 text-green-300" : "text-gray-400 hover:bg-gray-800/60"
                  }`}
                >
                  {checked
                    ? <CheckSquare className="w-3 h-3 shrink-0 text-green-400" />
                    : <Square className="w-3 h-3 shrink-0 text-gray-600" />
                  }
                  <FileCode className={`w-3 h-3 shrink-0 ${langColor}`} />
                  <span className="truncate">{f.file_name}</span>
                  <span className={`ml-auto text-[10px] font-mono ${langColor} opacity-60`}>{f.language}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────
export function CodeWriterPanel({ indexedFiles }: CodeWriterPanelProps) {
  const { isGenerating, output, steps, currentStep, error, stopped, generate, reset, stop } = useCodeWriter();

  const [mode, setMode] = useState<WriteMode>("generate");

  // Generate mode state
  const [genPrompt, setGenPrompt]         = useState("");
  const [genLanguage, setGenLanguage]     = useState("python");
  const [genFileName, setGenFileName]     = useState("output.py");
  const [contextSelected, setContextSelected] = useState<Set<string>>(new Set());

  // Edit mode state
  const [editFile, setEditFile]           = useState<IndexedFile | null>(null);
  const [editInstructions, setEditInstructions] = useState("");

  // Tests mode state
  const [testFile, setTestFile]           = useState<IndexedFile | null>(null);
  const [testFrameworkHint, setTestFrameworkHint] = useState("");
  const [testLanguage, setTestLanguage]   = useState("python");

  // Diff state — original content fetched when user picks a file in Edit mode
  const [originalContent, setOriginalContent] = useState<string>("");
  const [viewMode, setViewMode] = useState<"diff" | "full">("diff");
  // Prevent double-fetching if editFile changes rapidly (e.g. user clicks fast)
  const fetchingSourceRef = useRef<string>("");

  // UI
  const [traceExpanded, setTraceExpanded] = useState(true);

  const activeMode = MODES.find((m) => m.id === mode)!;

  // ── Language → extension auto-update ─────────────────────────────────────
  // ── Fetch original file content when edit file is selected ───────────────
  // Runs immediately when the user picks a file so the diff is ready the moment
  // generation completes — no extra wait. Guarded with a ref so rapid selection
  // changes don't cause race conditions (only the last selection is used).
  useEffect(() => {
    if (!editFile) { setOriginalContent(""); return; }
    const src = editFile.source;
    fetchingSourceRef.current = src;
    apiFetch(`/api/v1/write/file-content?source=${encodeURIComponent(src)}`)
      .then((r) => r.ok ? r.json() : Promise.reject(r.status))
      .then((data) => {
        // Only apply result if this is still the active selection
        if (fetchingSourceRef.current === src) setOriginalContent(data.content ?? "");
      })
      .catch(() => { /* non-fatal — diff just won't render */ });
  }, [editFile]);

  // Reset view to "diff" whenever a new generation starts
  useEffect(() => {
    if (isGenerating) setViewMode("diff");
  }, [isGenerating]);

  const handleGenLanguage = (lang: string) => {
    setGenLanguage(lang);
    const base = genFileName.replace(/\.[^.]+$/, "");
    setGenFileName(`${base}.${EXT_MAP[lang] ?? lang}`);
  };

  // ── Context file toggles ──────────────────────────────────────────────────
  const toggleContext = (src: string) => {
    setContextSelected((prev) => {
      const next = new Set(prev);
      next.has(src) ? next.delete(src) : next.add(src);
      return next;
    });
  };
  const toggleAllContext = () => {
    if (contextSelected.size === indexedFiles.length) setContextSelected(new Set());
    else setContextSelected(new Set(indexedFiles.map((f) => f.source)));
  };

  // ── Run ───────────────────────────────────────────────────────────────────
  const handleGenerate = () => {
    if (isGenerating) return;

    if (mode === "generate") {
      if (!genPrompt.trim()) return;
      generate(genPrompt, genLanguage, genFileName, Array.from(contextSelected), "generate");

    } else if (mode === "edit") {
      if (!editFile || !editInstructions.trim()) return;
      generate(
        editInstructions,
        editFile.language,
        editFile.file_name,
        [editFile.source],
        "edit",
      );

    } else if (mode === "tests") {
      if (!testFile) return;
      generate(
        testFrameworkHint,
        testLanguage,
        `test_${testFile.file_name.replace(/\.[^.]+$/, "")}.${EXT_MAP[testLanguage] ?? testLanguage}`,
        [testFile.source],
        "tests",
      );
    }
  };

  // ── Download ──────────────────────────────────────────────────────────────
  const handleDownload = () => {
    if (!output) return;
    const codeMatch = output.match(/```(?:\w+)?\n([\s\S]*?)```/);
    const rawCode = codeMatch ? codeMatch[1] : output;

    let filename = "output.txt";
    if (mode === "generate") filename = genFileName;
    else if (mode === "edit") filename = editFile?.file_name ?? "edited.py";
    else if (mode === "tests") filename = `test_${testFile?.file_name ?? "file"}`;

    const blob = new Blob([rawCode], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const canRun = !isGenerating && (
    mode === "generate" ? genPrompt.trim().length > 0 :
    mode === "edit"     ? !!editFile && editInstructions.trim().length > 0 :
    /* tests */           !!testFile
  );

  const buttonLabel = isGenerating
    ? "Working..."
    : mode === "generate" ? "Generate Code"
    : mode === "edit"     ? "Apply Changes"
    : "Generate Tests";

  return (
    <div className="flex flex-col h-full">

      {/* Header */}
      <div className="px-6 py-3 border-b border-gray-700 bg-gray-900 flex items-center justify-between">
        <div>
          <h2 className={`text-sm font-semibold text-white flex items-center gap-2`}>
            <activeMode.Icon className={`w-4 h-4 ${activeMode.color}`} />
            Code Writer Agent
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">{activeMode.description}</p>
        </div>
        {output && (
          <div className="flex items-center gap-2">
            {/* Diff / Full toggle — only shown for Edit mode when original content is available */}
            {mode === "edit" && originalContent && !isGenerating && (
              <div className="flex items-center bg-gray-800 border border-gray-700 rounded-lg p-0.5">
                <button
                  onClick={() => setViewMode("diff")}
                  className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                    viewMode === "diff"
                      ? "bg-blue-600 text-white"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  <GitCompare className="w-3 h-3" />
                  Diff
                </button>
                <button
                  onClick={() => setViewMode("full")}
                  className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                    viewMode === "full"
                      ? "bg-blue-600 text-white"
                      : "text-gray-400 hover:text-gray-200"
                  }`}
                >
                  <Code2 className="w-3 h-3" />
                  Full file
                </button>
              </div>
            )}
            <button onClick={handleDownload}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-green-400 transition-colors"
            >
              <Download className="w-3.5 h-3.5" />Download
            </button>
            <button onClick={() => { reset(); setOriginalContent(""); }}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              <RotateCcw className="w-3.5 h-3.5" />New
            </button>
          </div>
        )}
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">

        {/* Left: input panel */}
        <div className="w-72 shrink-0 border-r border-gray-700 flex flex-col bg-gray-900">

          {/* Mode tabs */}
          <div className="flex border-b border-gray-700">
            {MODES.map(({ id, label, Icon, color }) => (
              <button
                key={id}
                onClick={() => { setMode(id); reset(); }}
                className={`flex-1 flex items-center justify-center gap-1 py-2.5 text-xs font-medium transition-colors ${
                  mode === id
                    ? `${color} border-b-2 border-current`
                    : "text-gray-500 hover:text-gray-300"
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
              </button>
            ))}
          </div>

          <div className="flex-1 overflow-y-auto p-4 space-y-4">

            {/* ── GENERATE mode ── */}
            {mode === "generate" && (
              <>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 font-medium">What do you want to build?</label>
                  <textarea
                    value={genPrompt}
                    onChange={(e) => setGenPrompt(e.target.value)}
                    placeholder={"e.g. A FastAPI endpoint that accepts a GitHub URL and returns a list of Python files with their sizes"}
                    rows={5}
                    className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-green-500 resize-none placeholder-gray-600"
                  />
                </div>

                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Language</label>
                    <select value={genLanguage} onChange={(e) => handleGenLanguage(e.target.value)}
                      className="w-full bg-gray-800 text-white text-xs rounded-lg px-2 py-2 border border-gray-600 focus:outline-none focus:border-green-500"
                    >
                      {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">File name</label>
                    <input type="text" value={genFileName} onChange={(e) => setGenFileName(e.target.value)}
                      className="w-full bg-gray-800 text-white text-xs rounded-lg px-2 py-2 border border-gray-600 focus:outline-none focus:border-green-500"
                    />
                  </div>
                </div>

                {indexedFiles.length > 0 && (
                  <ContextPicker
                    files={indexedFiles}
                    selected={contextSelected}
                    onToggle={toggleContext}
                    onToggleAll={toggleAllContext}
                  />
                )}

                {contextSelected.size > 0 && (
                  <p className="text-[11px] text-green-500/70">
                    ✓ LLM will mirror the style of {contextSelected.size} selected file{contextSelected.size !== 1 ? "s" : ""}
                  </p>
                )}
              </>
            )}

            {/* ── EDIT mode ── */}
            {mode === "edit" && (
              <>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 font-medium">File to edit</label>
                  {indexedFiles.length === 0 ? (
                    <p className="text-xs text-gray-600">No files indexed yet.</p>
                  ) : (
                    <FilePicker
                      files={indexedFiles}
                      selected={editFile}
                      onSelect={setEditFile}
                      accent="text-blue-400"
                    />
                  )}
                </div>

                {editFile && (
                  <div className="flex items-center gap-2 p-2 bg-blue-900/10 border border-blue-700/30 rounded-lg">
                    <FileCode className="w-3.5 h-3.5 text-blue-400 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="text-xs text-blue-300 font-medium truncate">{editFile.file_name}</p>
                      <p className="text-[10px] text-gray-500">{editFile.language} · will be rewritten</p>
                    </div>
                    {editFile.source.includes("::") && (
                      <button
                        onClick={() => window.dispatchEvent(new CustomEvent("savflux:open-history", { detail: editFile.source }))}
                        title="View file history (timeline + blame)"
                        className="flex shrink-0 items-center gap-1 rounded-lg border border-teal-600/40 bg-teal-500/10 px-2 py-1 text-[11px] text-teal-300 hover:bg-teal-500/20"
                      >
                        <History className="w-3 h-3" /> History
                      </button>
                    )}
                  </div>
                )}

                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 font-medium">What changes to make?</label>
                  <textarea
                    value={editInstructions}
                    onChange={(e) => setEditInstructions(e.target.value)}
                    placeholder={"e.g. Add rate limiting to all endpoints\ne.g. Refactor the loop in process() to be async\ne.g. Fix the bug where empty inputs crash the validator"}
                    rows={6}
                    className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-blue-500 resize-none placeholder-gray-600"
                  />
                </div>

                <div className="text-[11px] text-gray-600 leading-relaxed">
                  💡 The full file content is sent to the LLM, which rewrites the entire file with only your requested changes applied.
                </div>
              </>
            )}

            {/* ── TESTS mode ── */}
            {mode === "tests" && (
              <>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 font-medium">File to test</label>
                  {indexedFiles.length === 0 ? (
                    <p className="text-xs text-gray-600">No files indexed yet.</p>
                  ) : (
                    <FilePicker
                      files={indexedFiles}
                      selected={testFile}
                      onSelect={setTestFile}
                      accent="text-purple-400"
                    />
                  )}
                </div>

                {testFile && (
                  <div className="flex items-center gap-2 p-2 bg-purple-900/10 border border-purple-700/30 rounded-lg">
                    <FlaskConical className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs text-purple-300 font-medium truncate">{testFile.file_name}</p>
                      <p className="text-[10px] text-gray-500">tests will be generated for this file</p>
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-xs text-gray-400 mb-1">Test language</label>
                    <select value={testLanguage} onChange={(e) => setTestLanguage(e.target.value)}
                      className="w-full bg-gray-800 text-white text-xs rounded-lg px-2 py-2 border border-gray-600 focus:outline-none focus:border-purple-500"
                    >
                      {LANGUAGES.map((l) => <option key={l} value={l}>{l}</option>)}
                    </select>
                  </div>
                </div>

                <div>
                  <label className="block text-xs text-gray-400 mb-1">
                    Extra instructions <span className="text-gray-600">(optional)</span>
                  </label>
                  <textarea
                    value={testFrameworkHint}
                    onChange={(e) => setTestFrameworkHint(e.target.value)}
                    placeholder={"e.g. Use pytest with fixtures\ne.g. Mock the database calls\ne.g. Focus on the validation logic"}
                    rows={4}
                    className="w-full bg-gray-800 text-white text-xs rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-purple-500 resize-none placeholder-gray-600"
                  />
                </div>

                <div className="text-[11px] text-gray-600 leading-relaxed">
                  💡 SavFlux reads your existing test files (if any) and generates tests that match the same framework and style.
                </div>
              </>
            )}
          </div>

          {/* Run button */}
          <div className="p-4 border-t border-gray-700">
            <button
              onClick={handleGenerate}
              disabled={!canRun}
              className={`w-full flex items-center justify-center gap-2 disabled:bg-gray-700 disabled:cursor-not-allowed disabled:text-gray-500 font-semibold text-sm py-2.5 rounded-xl transition-colors text-white ${
                mode === "generate" ? "bg-green-600 hover:bg-green-500" :
                mode === "edit"     ? "bg-blue-600 hover:bg-blue-500" :
                                      "bg-purple-600 hover:bg-purple-500"
              }`}
            >
              {isGenerating
                ? <><Loader2 className="w-4 h-4 animate-spin" />{buttonLabel}</>
                : <><activeMode.Icon className="w-4 h-4" />{buttonLabel}</>
              }
            </button>
            {isGenerating && (
              <div className="mt-2">
                <StopButton onClick={stop} className="w-full" />
              </div>
            )}
          </div>
        </div>

        {/* Right: output */}
        <div className="flex-1 overflow-y-auto bg-gray-950 flex flex-col">

          {/* Empty state */}
          {!output && !isGenerating && !error && (
            <div className="flex-1 flex flex-col items-center justify-center text-center p-8 gap-6">
              <div className="grid grid-cols-3 gap-4 max-w-lg w-full">
                {MODES.map(({ id, label, Icon, color, description }) => (
                  <button
                    key={id}
                    onClick={() => setMode(id)}
                    className={`flex flex-col items-center gap-2 p-4 rounded-xl border transition-colors cursor-pointer ${
                      mode === id
                        ? `border-current ${color} bg-gray-900`
                        : "border-gray-800 text-gray-600 hover:border-gray-700 hover:text-gray-400"
                    }`}
                  >
                    <Icon className="w-6 h-6" />
                    <span className="text-xs font-semibold">{label}</span>
                    <span className="text-[10px] leading-relaxed text-gray-600">{description}</span>
                  </button>
                ))}
              </div>
              <p className="text-xs text-gray-600 max-w-sm">
                {mode === "generate"
                  ? "Describe what you want to build. Optionally select context files so the LLM mirrors your existing style."
                  : mode === "edit"
                  ? "Pick an indexed file and describe the changes. The LLM receives the full file and returns a rewritten version."
                  : "Pick an indexed file. SavFlux reads your repo's existing tests to match the framework and style."
                }
              </p>
            </div>
          )}

          {/* Output */}
          {(isGenerating || output || steps.length > 0) && (
            <div className="p-6 space-y-4">

              {/* Trace */}
              {steps.length > 0 && (
                <div className="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
                  <button onClick={() => setTraceExpanded((v) => !v)}
                    className="w-full flex items-center justify-between px-4 py-3 text-xs text-gray-400 hover:text-gray-200"
                  >
                    <span className="font-medium">Trace ({steps.length} step{steps.length !== 1 ? "s" : ""})</span>
                    {traceExpanded ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
                  </button>
                  {traceExpanded && (
                    <div className="px-4 pb-3 space-y-1.5 border-t border-gray-700 pt-3">
                      {steps.map((s, i) => (
                        <div key={i} className="text-xs text-gray-400">
                          {STEP_LABELS[s.step] ?? `⚙️  ${s.step}`}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Current step — and, once the reader stops it, where it stopped */}
              {currentStep && (isGenerating || stopped) && (
                <div
                  role={stopped ? "status" : undefined}
                  className={`flex items-center gap-2 text-xs ${
                    stopped ? "text-amber-300/90 bg-amber-500/10 border border-amber-500/25 rounded-lg px-3 py-2" : activeMode.color
                  }`}
                >
                  {isGenerating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  {currentStep}
                </div>
              )}

              {/* Output — Diff view (Edit mode) or full ReactMarkdown */}
              {output && (() => {
                // Extract the first fenced code block for diff/copy targets
                const codeBlockMatch = output.match(/```(?:\w+)?\n([\s\S]*?)```/);
                const extractedCode = codeBlockMatch ? codeBlockMatch[1].replace(/\n$/, "") : "";
                const editLang = editFile?.language ?? "text";

                // In Edit mode with original content + Diff view selected: show DiffView
                if (mode === "edit" && originalContent && viewMode === "diff" && !isGenerating) {
                  return (
                    <div>
                      <DiffView
                        original={originalContent}
                        updated={extractedCode || output}
                        language={editLang}
                      />
                      {/* Render any non-code explanation text below the diff */}
                      {output.replace(/```[\s\S]*?```/g, "").trim() && (
                        <div className="prose prose-invert prose-sm max-w-none mt-4 text-xs text-gray-400">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {output.replace(/```[\s\S]*?```/g, "").trim()}
                          </ReactMarkdown>
                        </div>
                      )}
                    </div>
                  );
                }

                // Default: full file view with syntax highlighting + copy button
                return (
                  <div className="prose prose-invert prose-sm max-w-none">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        code({ className, children }: any) {
                          const match = /language-(\w+)/.exec(className || "");
                          const codeText = String(children).replace(/\n$/, "");
                          return match ? (
                            <div className="rounded-lg overflow-hidden border border-gray-700 my-3">
                              <div className="flex items-center justify-between px-3 py-1.5 bg-gray-800 border-b border-gray-700">
                                <span className="text-[10px] font-mono text-gray-500 uppercase tracking-wider">
                                  {match[1]}
                                </span>
                                <CopyButton text={codeText} />
                              </div>
                              <CodeHighlight
                                language={match[1]}
                                className="!mt-0 !rounded-none text-xs"
                                customStyle={{ margin: 0, borderRadius: 0 }}
                              >
                                {codeText}
                              </CodeHighlight>
                            </div>
                          ) : (
                            <code className={`px-1.5 py-0.5 rounded text-xs font-mono bg-gray-800 ${activeMode.color}`}>{children}</code>
                          );
                        },
                      }}
                    >
                      {output + (isGenerating ? " ▋" : "")}
                    </ReactMarkdown>
                  </div>
                );
              })()}

              {error && (
                <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-3">
                  {error}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
