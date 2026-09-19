/**
 * TrustLedger.tsx — Per-citation verification drawer (P1 #4).
 *
 * Opened by clicking a citation chip in MessageBubble. Shows why the source
 * earned its trust level, a content preview around the cited lines (via the
 * existing GET /write/file-content endpoint), and a jump to Code Review.
 */

import { useEffect, useState } from "react";
import {
  X, ShieldCheck, ShieldAlert, Shield, FileCode, Loader2,
  ArrowUpRight, AlertTriangle,
} from "lucide-react";
import { SourceFile } from "../types";
import { apiFetch } from "../api";

interface TrustLedgerDrawerProps {
  source: SourceFile;
  onClose: () => void;
  activeRepoUrl?: string | null;
  onOpenInReview?: (source: string) => void;
}

function explainTrust(src: SourceFile): { label: string; detail: string; level: "high" | "medium" | "low" } {
  const level = (src.trust_level === "high" || src.trust_level === "medium") ? src.trust_level : "low";
  if (level === "high") {
    return {
      level,
      label: "High trust",
      detail: "Top-ranked retrieval hit with strong lexical + semantic agreement. Safe to quote.",
    };
  }
  if (level === "medium") {
    return {
      level,
      label: "Medium trust",
      detail: "Relevant match but weaker ranking consensus. Skim the preview before relying on it.",
    };
  }
  return {
    level,
    label: "Low trust",
    detail: "Fallback or weakly-ranked source. Treat as a lead, not evidence — verify in Review.",
  };
}

export function TrustLedgerDrawer({ source, onClose, activeRepoUrl, onOpenInReview }: TrustLedgerDrawerProps) {
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trust = explainTrust(source);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPreview(null);
    apiFetch(`/api/v1/write/file-content?source=${encodeURIComponent(source.source)}`)
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) throw new Error(res.status === 404 ? "File no longer on disk — re-index to refresh." : `Server error ${res.status}`);
        const data = await res.json();
        let content: string = data.content ?? "";
        // Narrow to the cited line window (±10 context) when line info exists
        if (source.start_line && content) {
          const lines = content.split("\n");
          const from = Math.max(0, source.start_line - 11);
          const to = Math.min(lines.length, (source.end_line ?? source.start_line) + 10);
          const numbered = lines.slice(from, to).map((l, i) => {
            const n = from + i + 1;
            const mark = source.start_line && n >= source.start_line && n <= (source.end_line ?? source.start_line) ? "› " : "  ";
            return `${mark}${String(n).padStart(4, " ")} │ ${l}`;
          });
          content = numbered.join("\n");
        }
        setPreview(content.slice(0, 6000));
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Preview failed");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [source]);

  const Icon = trust.level === "high" ? ShieldCheck : trust.level === "medium" ? ShieldAlert : Shield;
  const iconColor = trust.level === "high" ? "text-emerald-400" : trust.level === "medium" ? "text-amber-400" : "text-gray-500";

  return (
    <div className="mt-2 overflow-hidden rounded-xl border border-purple-500/30 bg-gray-900">
      <div className="flex items-center gap-2 border-b border-gray-800 px-3 py-2">
        <FileCode className="w-3.5 h-3.5 text-purple-400 shrink-0" />
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-gray-200" title={source.source}>
          {source.file_name}
          {source.start_line ? `:${source.start_line}${source.end_line && source.end_line !== source.start_line ? `-${source.end_line}` : ""}` : ""}
        </span>
        <Icon className={`w-3.5 h-3.5 shrink-0 ${iconColor}`} />
        <span className="text-[11px] text-gray-400">{trust.label}</span>
        <button onClick={onClose} title="Close" className="rounded p-1 text-gray-500 hover:text-white">
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="space-y-2 px-3 py-2.5 text-xs">
        <p className="leading-snug text-gray-400">{trust.detail}</p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-gray-500">
          <span>lang: <span className="text-gray-300">{source.language || "?"}</span></span>
          {source.symbol_name && <span>symbol: <span className="text-gray-300">{source.symbol_name}</span></span>}
          {source.trust_score !== undefined && <span>score: <span className="text-gray-300">{String(source.trust_score)}</span></span>}
          {activeRepoUrl && <span className="truncate">repo: <span className="text-gray-300">{activeRepoUrl}</span></span>}
        </div>

        {loading && (
          <p className="flex items-center gap-2 text-gray-500">
            <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading preview…
          </p>
        )}
        {error && (
          <p className="flex items-center gap-2 text-amber-300">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> {error}
          </p>
        )}
        {preview && (
          <pre className="max-h-56 overflow-auto rounded-lg border border-gray-800 bg-gray-950 p-3 font-mono text-[11px] leading-relaxed text-gray-300">
            {preview}
          </pre>
        )}

        {onOpenInReview && (
          <button
            onClick={() => onOpenInReview(source.source)}
            className="flex items-center gap-1.5 rounded-lg border border-purple-500/40 bg-purple-500/10 px-3 py-1.5 text-xs text-purple-200 hover:bg-purple-500/20"
          >
            <ArrowUpRight className="w-3.5 h-3.5" /> Open in Code Review
          </button>
        )}
      </div>
    </div>
  );
}

export default TrustLedgerDrawer;
