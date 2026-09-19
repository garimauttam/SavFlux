/**
 * AnalyticsPanel.tsx — Request-latency analytics ($0, zero-dep SVG sparklines).
 *
 * Reads GET /analytics/summary (written by the backend middleware on every
 * request) and renders per-endpoint counts, avg/p95 latency, and a latency
 * sparkline. No chart library — pure SVG keeps the bundle lean.
 */

import { useCallback, useEffect, useState } from "react";
import { BarChart3, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { apiFetch } from "../api";

interface EndpointStat {
  path: string;
  count: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  max_latency_ms: number;
}

interface Sample {
  ts: number;
  path: string;
  latency_ms: number;
  status: number;
}

interface Summary {
  total_requests: number;
  errors: number;
  endpoints: EndpointStat[];
  recent: Sample[];
}

function Sparkline({ samples }: { samples: Sample[] }) {
  if (samples.length < 2) return <p className="text-xs text-gray-600">Not enough data yet.</p>;
  const W = 560, H = 90, PAD = 6;
  const max = Math.max(...samples.map((s) => s.latency_ms), 1);
  const pts = samples.map((s, i) => {
    const x = PAD + (i / (samples.length - 1)) * (W - PAD * 2);
    const y = H - PAD - (s.latency_ms / max) * (H - PAD * 2);
    return { x, y, err: s.status >= 400 };
  });
  const line = pts.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-24 w-full">
      <polyline points={line} fill="none" stroke="#818cf8" strokeWidth="1.5" />
      {pts.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r="2" fill={p.err ? "#f87171" : "#818cf8"} opacity="0.7">
          <title>{`${samples[i].path} — ${samples[i].latency_ms}ms (${samples[i].status})`}</title>
        </circle>
      ))}
    </svg>
  );
}

export default function AnalyticsPanel() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/analytics/summary");
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setSummary(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load analytics");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSummary();
  }, [fetchSummary]);

  const clear = async () => {
    if (!window.confirm("Clear all analytics samples?")) return;
    try {
      await apiFetch("/api/v1/analytics/history", { method: "DELETE" });
      fetchSummary();
    } catch {}
  };

  return (
    <div className="h-full overflow-y-auto bg-gray-950 p-6">
      <div className="mx-auto max-w-3xl space-y-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-indigo-400" />
            <h2 className="text-lg font-semibold text-white">Analytics</h2>
            {summary && (
              <span className="text-xs text-gray-500">
                {summary.total_requests} requests · {summary.errors} errors
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={clear}
              title="Clear analytics history"
              className="rounded-lg border border-gray-700 bg-gray-800 p-2 text-gray-400 hover:text-red-300"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={fetchSummary}
              disabled={loading}
              className="flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:text-white disabled:opacity-50"
            >
              {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
              Refresh
            </button>
          </div>
        </div>

        {error && <p className="text-sm text-red-300">{error}</p>}

        {summary && (
          <>
            <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                Recent latency (ms) — red dots are errors
              </h3>
              <Sparkline samples={summary.recent} />
            </div>

            <div className="overflow-hidden rounded-xl border border-gray-800">
              <table className="w-full text-sm">
                <thead className="bg-gray-900 text-left text-xs uppercase text-gray-500">
                  <tr>
                    <th className="px-4 py-2 font-medium">Endpoint</th>
                    <th className="px-4 py-2 text-right font-medium">Hits</th>
                    <th className="px-4 py-2 text-right font-medium">Avg</th>
                    <th className="px-4 py-2 text-right font-medium">p95</th>
                    <th className="px-4 py-2 text-right font-medium">Max</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800 bg-gray-900/50">
                  {summary.endpoints.map((e) => (
                    <tr key={e.path}>
                      <td className="max-w-[260px] truncate px-4 py-2 font-mono text-xs text-gray-300" title={e.path}>
                        {e.path}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-xs text-gray-400">{e.count}</td>
                      <td className="px-4 py-2 text-right font-mono text-xs text-gray-400">{e.avg_latency_ms}ms</td>
                      <td className="px-4 py-2 text-right font-mono text-xs text-gray-400">{e.p95_latency_ms}ms</td>
                      <td className="px-4 py-2 text-right font-mono text-xs text-gray-400">{e.max_latency_ms}ms</td>
                    </tr>
                  ))}
                  {summary.endpoints.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-4 py-6 text-center text-xs text-gray-600">
                        No requests recorded yet — use the app and come back.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
