/**
 * TrustLedgerPanel.tsx — Commit verification ledger (P1 #4).
 *
 * Shows which exact upstream commit each indexed repo was built from, and
 * whether the local index still matches it (verified / stale / unknown).
 * Rendered inside the Health tab; degrades to an informational empty state
 * when no repo is selected or the trust backend is unavailable.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ShieldCheck, ShieldAlert, ShieldQuestion, Loader2, RefreshCw, GitCommitHorizontal,
} from "lucide-react";
import { apiFetch } from "../api";

interface TrustEntry {
  repo_url: string;
  indexed_sha: string | null;
  indexed_at: number | null;
  upstream_sha: string | null;
  status: "verified" | "stale" | "unknown";
  files_indexed: number;
  detail: string;
}

interface TrustLedgerPanelProps {
  repoUrl: string | null;
}

function StatusIcon({ status }: { status: TrustEntry["status"] }) {
  if (status === "verified") return <ShieldCheck className="w-4 h-4 text-emerald-400" />;
  if (status === "stale") return <ShieldAlert className="w-4 h-4 text-amber-400" />;
  return <ShieldQuestion className="w-4 h-4 text-gray-500" />;
}

export function TrustLedgerPanel({ repoUrl }: TrustLedgerPanelProps) {
  const [entry, setEntry] = useState<TrustEntry | null>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  const fetchEntry = useCallback(async () => {
    if (!repoUrl) {
      setEntry(null);
      return;
    }
    setLoading(true);
    setUnavailable(false);
    try {
      const res = await apiFetch(`/api/v1/trust/ledger?repo_url=${encodeURIComponent(repoUrl)}`);
      if (res.status === 404) {
        // No ledger row yet (repo indexed before verification existed)
        setEntry(null);
        return;
      }
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      setEntry(await res.json());
    } catch {
      setUnavailable(true);
    } finally {
      setLoading(false);
    }
  }, [repoUrl]);

  useEffect(() => {
    fetchEntry();
  }, [fetchEntry]);

  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <GitCommitHorizontal className="w-4 h-4 text-emerald-400" />
          <h3 className="text-sm font-semibold text-white">Trust Ledger</h3>
          <span className="text-[11px] text-gray-500">commit verification</span>
        </div>
        {repoUrl && (
          <button
            onClick={fetchEntry}
            disabled={loading}
            title="Re-check verification status"
            className="rounded-lg border border-gray-700 bg-gray-800 p-1.5 text-gray-400 hover:text-white disabled:opacity-50"
          >
            {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          </button>
        )}
      </div>

      {!repoUrl && (
        <p className="mt-2 text-xs text-gray-500">
          Select an active repo to see its verification status.
        </p>
      )}

      {repoUrl && loading && !entry && (
        <p className="mt-2 flex items-center gap-2 text-xs text-gray-500">
          <Loader2 className="w-3.5 h-3.5 animate-spin" /> Checking…
        </p>
      )}

      {repoUrl && !loading && unavailable && (
        <p className="mt-2 text-xs text-gray-500">
          Verification backend unavailable — index content is still fully usable.
        </p>
      )}

      {repoUrl && !loading && !unavailable && !entry && (
        <p className="mt-2 text-xs text-gray-500">
          No verification record for this repo yet. Re-index it to capture the upstream commit.
        </p>
      )}

      {entry && (
        <div className="mt-3 space-y-2 text-xs">
          <div className="flex items-center gap-2">
            <StatusIcon status={entry.status} />
            <span className="font-medium capitalize text-gray-200">{entry.status}</span>
            <span className="text-gray-500">· {entry.detail}</span>
          </div>
          <div className="grid gap-1.5 font-mono text-[11px]">
            <div className="flex justify-between gap-3">
              <span className="text-gray-500">indexed commit</span>
              <span className="truncate text-gray-300">{entry.indexed_sha ?? "—"}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-gray-500">upstream HEAD</span>
              <span className="truncate text-gray-300">{entry.upstream_sha ?? "—"}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-gray-500">files indexed</span>
              <span className="text-gray-300">{entry.files_indexed}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default TrustLedgerPanel;
