/**
 * ExportButton.tsx — Client-side answer export ($0, no backend).
 *
 * Exports a Q&A pair as Markdown download / JSON download / clipboard copy,
 * including the citation list and trust-ledger snapshot. Pure browser APIs
 * (Blob + clipboard), so it works offline and costs nothing.
 */

import { useEffect, useRef, useState } from "react";
import { Download, Copy, Check, FileJson, FileText } from "lucide-react";

interface ExportButtonProps {
  question: string;
  answer: string;
  sources?: { file_name?: string; source?: string; language?: string; trust_level?: string }[];
  repoUrl?: string | null;
  ledger?: { sources?: unknown[] };
}

function download(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function slug(text: string): string {
  return (
    text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) ||
    "savflux-answer"
  );
}

export function ExportButton({ question, answer, sources, repoUrl, ledger }: ExportButtonProps) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Close the menu on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open ]);

  const markdown = [
    `# ${question}`,
    "",
    ...(repoUrl ? [`> Repo: ${repoUrl}`, ""] : []),
    answer,
    "",
    "## Sources",
    ...(sources && sources.length > 0
      ? sources.map((s) => `- \`${s.file_name || s.source}\`${s.trust_level ? ` (trust: ${s.trust_level})` : ""}`)
      : ["_none_"]),
    "",
    `_Exported from SavFlux · ${new Date().toLocaleString()}_`,
    "",
  ].join("\n");

  const json = JSON.stringify(
    { question, answer, sources: sources ?? [], repo_url: repoUrl ?? null, ledger: ledger ?? null, exported_at: new Date().toISOString() },
    null,
    2
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {}
    setOpen(false);
  };

  const item = "flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-gray-300 hover:bg-gray-700";

  return (
    <div className="relative" ref={menuRef}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Export answer (markdown / JSON / copy)"
        className="flex items-center gap-1 rounded-full border border-gray-700 bg-gray-800 px-2 py-1 text-[11px] text-gray-400 hover:text-white"
      >
        <Download className="w-3 h-3" /> Export
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-20 mb-1 w-48 overflow-hidden rounded-lg border border-gray-700 bg-gray-900 shadow-xl">
          <button
            className={item}
            onClick={() => { download(`${slug(question)}.md`, markdown, "text/markdown"); setOpen(false); }}
          >
            <FileText className="w-3.5 h-3.5 text-blue-400" /> Download .md
          </button>
          <button
            className={item}
            onClick={() => { download(`${slug(question)}.json`, json, "application/json"); setOpen(false); }}
          >
            <FileJson className="w-3.5 h-3.5 text-amber-400" /> Download .json
          </button>
          <button className={item} onClick={copy}>
            {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5 text-gray-400" />}
            {copied ? "Copied!" : "Copy markdown"}
          </button>
        </div>
      )}
    </div>
  );
}

export default ExportButton;
