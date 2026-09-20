/**
 * DiffBlock.tsx — Render a unified diff the way a reviewer reads one.
 *
 * WHY NOT A SYNTAX HIGHLIGHTER?
 * A diff is not a language, and highlighting it as Python colours the `-` lines
 * as if they were still live code. The three colours that matter here are
 * "removed", "added" and "where am I in the file", so they are applied by line
 * prefix — the same rule `git diff` uses.
 *
 * Every place in the product that shows a diff (autofix, PR preview, the diff
 * viewer) needs copy and download, so they live here rather than being
 * re-implemented per panel with slightly different labels.
 */

import { useMemo, useState } from "react";
import { Check, Copy, Download } from "lucide-react";

interface DiffBlockProps {
  diff: string;
  /** Suggested filename for the download button. */
  downloadName?: string;
  /** Tailwind max-height class. Defaults to a scrollable 60vh. */
  maxHeightClass?: string;
  /** Hide the copy/download toolbar (e.g. when the parent already offers them). */
  hideToolbar?: boolean;
  /** Field label for the copy button, e.g. "Copy patch". */
  copyLabel?: string;
}

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "text-cyan-300 bg-cyan-500/10";
  if (line.startsWith("+++") || line.startsWith("---")) return "text-gray-500";
  if (line.startsWith("diff --git") || line.startsWith("index ") ||
      line.startsWith("new file mode") || line.startsWith("deleted file mode") ||
      line.startsWith("\\ No newline")) {
    return "text-gray-500";
  }
  if (line.startsWith("+")) return "text-emerald-300 bg-emerald-500/10";
  if (line.startsWith("-")) return "text-red-300 bg-red-500/10";
  return "text-gray-300";
}

export function DiffBlock({
  diff,
  downloadName = "savflux.patch",
  maxHeightClass = "max-h-[60vh]",
  hideToolbar = false,
  copyLabel = "Copy diff",
}: DiffBlockProps) {
  const [copied, setCopied] = useState(false);
  const lines = useMemo(() => diff.split("\n"), [diff]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(diff);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard is unavailable over plain HTTP on some hosts — the download
      // button is still there, so this is a no-op rather than an error banner.
    }
  };

  const download = () => {
    const blob = new Blob([diff], { type: "text/x-patch" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = downloadName;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="rounded-lg border border-gray-800 bg-black/40 overflow-hidden">
      {!hideToolbar && (
        <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-800 bg-gray-900/60">
          <span className="text-[11px] font-mono text-gray-500">
            {lines.length} line{lines.length === 1 ? "" : "s"} · apply with{" "}
            <span className="text-gray-400">git apply</span>
          </span>
          <div className="flex items-center gap-3">
            <button
              onClick={copy}
              className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-white transition-colors"
            >
              {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
              {copied ? "Copied" : copyLabel}
            </button>
            <button
              onClick={download}
              className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-white transition-colors"
            >
              <Download className="w-3 h-3" />
              .patch
            </button>
          </div>
        </div>
      )}
      <pre
        className={`text-[11px] leading-relaxed font-mono p-3 overflow-auto ${maxHeightClass} whitespace-pre`}
      >
        {lines.map((line, index) => (
          <div key={index} className={lineClass(line)}>
            {line || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}

export default DiffBlock;
