/**
 * ShareView.tsx — Public read-only view for a shared Q&A pair (P1 #5.5).
 *
 * Route: /s/{id} — App.tsx renders this component instead of the full app
 * when the path starts with /s/. No sidebar, no API key needed: the
 * backend's GET /share/{id} is intentionally public (unguessable id).
 */

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Loader2, AlertTriangle, Share2, FileCode, ArrowLeft } from "lucide-react";
import { apiFetch } from "../api";

interface SharedSource {
  file_name?: string;
  source?: string;
  language?: string;
}

interface Share {
  id: string;
  question: string;
  answer: string;
  sources: SharedSource[];
  repo_url: string | null;
  created_at: number;
}

function shareIdFromPath(): string {
  try {
    const m = window.location.pathname.match(/^\/s\/([A-Za-z0-9]+)/);
    return m ? m[1] : "";
  } catch {
    return "";
  }
}

export function ShareView() {
  const [share, setShare] = useState<Share | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const id = shareIdFromPath();
    if (!id) {
      setError("Invalid share link.");
      setLoading(false);
      return;
    }
    apiFetch(`/api/v1/share/${encodeURIComponent(id)}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(res.status === 404 ? "Share link not found or expired." : `Server error ${res.status}`);
        setShare(await res.json());
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load share"))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex justify-center px-4 py-10">
      <div className="w-full max-w-3xl">
        <div className="flex items-center justify-between mb-6">
          <button
            onClick={() => { window.location.href = "/"; }}
            className="flex items-center gap-1.5 text-sm text-gray-400 hover:text-white"
          >
            <ArrowLeft className="w-4 h-4" /> Back to SavFlux
          </button>
          <span className="flex items-center gap-1.5 text-xs text-gray-500">
            <Share2 className="w-3.5 h-3.5" /> Shared answer
          </span>
        </div>

        {loading && (
          <div className="flex items-center gap-2 text-gray-400 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading shared answer…
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
            <AlertTriangle className="w-4 h-4 shrink-0" /> {error}
          </div>
        )}

        {share && (
          <article className="space-y-5">
            <h1 className="text-xl font-semibold leading-snug">{share.question}</h1>
            {share.repo_url && (
              <p className="text-xs text-gray-500 font-mono break-all">{share.repo_url}</p>
            )}
            <div className="prose prose-invert prose-sm max-w-none rounded-xl border border-gray-800 bg-gray-900 p-5">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{share.answer || "_No answer recorded._"}</ReactMarkdown>
            </div>
            {share.sources.length > 0 && (
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Sources</h2>
                <ul className="space-y-1">
                  {share.sources.map((s, i) => (
                    <li key={i} className="flex items-center gap-2 text-sm text-gray-300">
                      <FileCode className="w-3.5 h-3.5 text-purple-400 shrink-0" />
                      <span className="font-mono text-xs truncate">{s.file_name || s.source}</span>
                      {s.language && <span className="text-[10px] text-gray-600">{s.language}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <p className="text-[11px] text-gray-600">
              Shared {new Date(share.created_at * 1000).toLocaleString()} · SavFlux
            </p>
          </article>
        )}
      </div>
    </div>
  );
}

export default ShareView;
