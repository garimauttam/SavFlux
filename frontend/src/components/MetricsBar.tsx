/**
 * MetricsBar.tsx — Persistent footer showing live token usage stats.
 *
 * Polls GET /api/v1/metrics every 30s (and immediately on mount).
 * Displays: total tokens, prompt/completion split, LLM call count,
 * per-feature request breakdown, and an estimated cost.
 *
 * WHY THIS MATTERS FOR A RESUME PROJECT:
 * Every production LLM product tracks token usage for cost management and
 * rate-limit planning. Showing interviewers that you instrumented observability
 * from day one signals production-level thinking, not just prototype-level.
 */

import { useState, useEffect, useCallback } from "react";
import { Activity, Zap, RefreshCw } from "lucide-react";
import { apiFetch } from "../api";

interface Metrics {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  llm_calls: number;
  chat_requests: number;
  review_requests: number;
  write_requests: number;
  estimated_cost_usd: number;
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export function MetricsBar() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const fetchMetrics = useCallback(async () => {
    try {
      const res = await apiFetch("/api/v1/metrics");
      if (res.ok) {
        const data = await res.json();
        setMetrics(data);
        setLastRefresh(new Date());
      }
    } catch {
      // silent — metrics are non-critical
    }
  }, []);

  useEffect(() => {
    fetchMetrics();
    const interval = setInterval(fetchMetrics, 30_000);
    return () => clearInterval(interval);
  }, [fetchMetrics]);

  const handleReset = async () => {
    try {
      await apiFetch("/api/v1/metrics", { method: "DELETE" });
      fetchMetrics();
    } catch {}
  };

  if (!metrics) return null;

  const hasActivity = metrics.total_tokens > 0 || metrics.llm_calls > 0;

  return (
    <div className="border-t border-gray-800 bg-gray-950 text-xs select-none">
      {/* ── Collapsed bar ─────────────────────────────────────────────────── */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 px-4 py-1.5 hover:bg-gray-900 transition-colors text-left"
        title="Toggle token usage details"
      >
        <Activity className="w-3 h-3 text-purple-400 shrink-0" />
        <span className="text-gray-500 font-medium">Tokens</span>

        <span className={`font-mono ${hasActivity ? "text-purple-300" : "text-gray-600"}`}>
          {fmt(metrics.total_tokens)}
        </span>

        <span className="text-gray-700">·</span>
        <span className="text-gray-500">LLM calls</span>
        <span className={`font-mono ${hasActivity ? "text-yellow-400" : "text-gray-600"}`}>
          {metrics.llm_calls}
        </span>

        {metrics.estimated_cost_usd > 0 && (
          <>
            <span className="text-gray-700">·</span>
            <span className="text-gray-500">est.</span>
            <span className="font-mono text-green-400">
              ${metrics.estimated_cost_usd < 0.001
                ? "<$0.001"
                : `$${metrics.estimated_cost_usd.toFixed(4)}`}
            </span>
          </>
        )}

        <span className="ml-auto text-gray-700 text-xs">
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {/* ── Expanded breakdown ────────────────────────────────────────────── */}
      {expanded && (
        <div className="px-4 pb-3 pt-1 grid grid-cols-2 gap-x-6 gap-y-1.5 border-t border-gray-800">
          {/* Token breakdown */}
          <div className="space-y-1">
            <div className="text-gray-500 font-semibold uppercase tracking-wider text-[10px] mb-1.5 flex items-center gap-1">
              <Zap className="w-2.5 h-2.5" />
              Token Usage
            </div>
            <Row label="Prompt" value={fmt(metrics.prompt_tokens)} color="text-blue-400" />
            <Row label="Completion" value={fmt(metrics.completion_tokens)} color="text-purple-400" />
            <Row label="Total" value={fmt(metrics.total_tokens)} color="text-white" bold />
            <Row
              label="Est. cost (GPT-4o-mini rate)"
              value={
                metrics.estimated_cost_usd < 0.001
                  ? "<$0.001"
                  : `$${metrics.estimated_cost_usd.toFixed(4)}`
              }
              color="text-green-400"
            />
          </div>

          {/* Request breakdown */}
          <div className="space-y-1">
            <div className="text-gray-500 font-semibold uppercase tracking-wider text-[10px] mb-1.5">
              Requests
            </div>
            <Row label="LLM calls" value={String(metrics.llm_calls)} color="text-yellow-400" />
            <Row label="Chat" value={String(metrics.chat_requests)} color="text-blue-400" />
            <Row label="Reviews" value={String(metrics.review_requests)} color="text-orange-400" />
            <Row label="Code writes" value={String(metrics.write_requests)} color="text-pink-400" />

            {/* Controls */}
            <div className="flex items-center gap-2 mt-2 pt-2 border-t border-gray-800">
              <button
                onClick={(e) => { e.stopPropagation(); fetchMetrics(); }}
                className="flex items-center gap-1 text-gray-500 hover:text-gray-300 transition-colors"
                title="Refresh metrics"
              >
                <RefreshCw className="w-3 h-3" />
                Refresh
              </button>
              <span className="text-gray-700">·</span>
              <button
                onClick={(e) => { e.stopPropagation(); handleReset(); }}
                className="text-gray-600 hover:text-red-400 transition-colors"
                title="Reset counters"
              >
                Reset
              </button>
              {lastRefresh && (
                <>
                  <span className="text-gray-700">·</span>
                  <span className="text-gray-700">
                    {lastRefresh.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                  </span>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Row({
  label,
  value,
  color,
  bold = false,
}: {
  label: string;
  value: string;
  color: string;
  bold?: boolean;
}) {
  return (
    <div className="flex justify-between items-center gap-2">
      <span className="text-gray-500 truncate">{label}</span>
      <span className={`font-mono ${color} ${bold ? "font-semibold" : ""} shrink-0`}>
        {value}
      </span>
    </div>
  );
}
