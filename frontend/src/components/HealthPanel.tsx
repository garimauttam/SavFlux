/**
 * HealthPanel.tsx — Deep health dashboard (P1 #7).
 *
 * Polls GET /health (LLM provider + ChromaDB deep checks) with auto-refresh,
 * and renders per-check status cards plus the active provider. A "Trust
 * Ledger" section (commit verification) is rendered below when the trust
 * backend is available — see TrustLedgerPanel.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Activity, CheckCircle2, XCircle, RefreshCw, Loader2,
  Cpu, Database, Server,
} from "lucide-react";
import { apiFetch } from "../api";
import { TrustLedgerPanel } from "./TrustLedgerPanel";

interface HealthResponse {
  status: string;
  service: string;
  provider?: string;
  checks: Record<string, string>;
}

interface HealthPanelProps {
  activeRepoUrl: string | null;
}

const CHECK_ICON: Record<string, typeof Cpu> = {
  llm: Cpu,
  chromadb: Database,
};

function isOk(value: string): boolean {
  return value.trim().toLowerCase().startsWith("ok");
}

export function HealthPanel({ activeRepoUrl }: HealthPanelProps) {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const fetchHealth = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/health");
      const data: HealthResponse = await res.json();
      // 503 still carries the per-check breakdown — show it, don't throw.
      setHealth(data);
      setLastChecked(new Date());
      if (!res.ok) setError(`Service degraded (HTTP ${res.status}) — see checks below.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Health check failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchHealth();
    const id = window.setInterval(fetchHealth, 30000); // 30s auto-refresh
    return () => window.clearInterval(id);
  }, [fetchHealth]);

  const overallOk = health?.status === "ok";

  return (
    <div className="h-full overflow-y-auto bg-gray-950 p-6">
      <div className="mx-auto max-w-3xl space-y-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Activity className="w-5 h-5 text-emerald-400" />
            <h2 className="text-lg font-semibold text-white">System Health</h2>
            {health && (
              <span
                className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${
                  overallOk
                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                    : "border-red-500/30 bg-red-500/10 text-red-300"
                }`}
              >
                {overallOk ? "All systems operational" : "Degraded"}
              </span>
            )}
          </div>
          <button
            onClick={fetchHealth}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-800 px-3 py-1.5 text-xs text-gray-300 hover:text-white disabled:opacity-50"
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            Refresh
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm text-amber-300">
            <XCircle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}

        {!health && !error && (
          <div className="flex items-center gap-2 text-sm text-gray-400">
            <Loader2 className="w-4 h-4 animate-spin" /> Checking services…
          </div>
        )}

        {health && (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <Server className="w-3.5 h-3.5" /> Service
                </div>
                <p className="mt-1 text-sm font-medium text-white">{health.service}</p>
              </div>
              <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <Cpu className="w-3.5 h-3.5" /> LLM Provider
                </div>
                <p className="mt-1 text-sm font-medium text-white">{health.provider ?? "—"}</p>
              </div>
              <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <Activity className="w-3.5 h-3.5" /> Last checked
                </div>
                <p className="mt-1 text-sm font-medium text-white">
                  {lastChecked ? lastChecked.toLocaleTimeString() : "—"}
                </p>
              </div>
            </div>

            <div className="space-y-2">
              {Object.entries(health.checks ?? {}).map(([name, value]) => {
                const ok = isOk(value);
                const Icon = CHECK_ICON[name] ?? Activity;
                return (
                  <div
                    key={name}
                    className={`flex items-start gap-3 rounded-xl border p-4 ${
                      ok ? "border-gray-800 bg-gray-900" : "border-red-500/30 bg-red-500/5"
                    }`}
                  >
                    {ok
                      ? <CheckCircle2 className="w-5 h-5 shrink-0 text-emerald-400" />
                      : <XCircle className="w-5 h-5 shrink-0 text-red-400" />}
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Icon className="w-3.5 h-3.5 text-gray-500" />
                        <span className="text-sm font-medium capitalize text-white">{name}</span>
                      </div>
                      <p className="mt-1 break-words font-mono text-xs text-gray-400">{value}</p>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}

        {/* Commit verification ledger for the active repo (P1 #4) */}
        <TrustLedgerPanel repoUrl={activeRepoUrl} />
      </div>
    </div>
  );
}

export default HealthPanel;
