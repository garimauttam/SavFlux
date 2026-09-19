/**
 * AgentPanel.tsx — Deterministic code-task agent UI ($0, no LLM).
 *
 * Sends a goal to POST /agent/run and renders the streamed __STATUS__
 * telemetry as a live step timeline plus the final markdown report.
 * The tool catalogue from GET /agent/tools is shown for transparency.
 */

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bot, Loader2, Play, Wrench, CheckCircle2, AlertTriangle, ChevronRight,
} from "lucide-react";
import { apiFetch } from "../api";

interface AgentTool {
  name: string;
  description: string;
  args: string[];
}

interface AgentStep {
  step: string;
  message: string;
  tool?: string;
}

export function AgentPanel() {
  const [goal, setGoal] = useState("");
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [steps, setSteps] = useState<AgentStep[]>([]);
  const [report, setReport] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    apiFetch("/api/v1/agent/tools")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data) setTools(data.tools ?? []);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [steps, report]);

  const run = async () => {
    if (!goal.trim() || running) return;
    setRunning(true);
    setError(null);
    setSteps([]);
    setReport("");
    try {
      const res = await apiFetch("/api/v1/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: goal.trim(), max_steps: 6 }),
      });
      if (!res.ok || !res.body) throw new Error(`Server error ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Extract complete __STATUS__ markers; rest is report text
        let idx: number;
        while ((idx = buffer.indexOf("__STATUS__")) !== -1) {
          const end = buffer.indexOf("__STATUS_END__", idx);
          if (end === -1) break; // partial marker — wait for more
          const before = buffer.slice(0, idx);
          if (before) setReport((r) => r + before);
          try {
            const payload = JSON.parse(buffer.slice(idx + 10, end));
            setSteps((s) => [...s, payload]);
          } catch {}
          buffer = buffer.slice(end + 14);
        }
        // Flush plain text up to the last safe boundary (keep partial markers)
        const partial = buffer.search(/__STATUS__?$/);
        if (partial !== -1) {
          setReport((r) => r + buffer.slice(0, partial));
          buffer = buffer.slice(partial);
        } else if (!buffer.includes("__STATUS")) {
          setReport((r) => r + buffer);
          buffer = "";
        }
      }
      if (buffer && !buffer.includes("__STATUS")) setReport((r) => r + buffer);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Agent run failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex h-full overflow-hidden bg-gray-950">
      {/* Tool catalogue */}
      <div className="hidden w-64 shrink-0 flex-col gap-2 overflow-y-auto border-r border-gray-800 p-4 md:flex">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
          <Wrench className="w-3.5 h-3.5" /> Agent tools
        </div>
        {tools.map((t) => (
          <div key={t.name} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
            <p className="font-mono text-xs text-purple-300">{t.name}</p>
            <p className="mt-1 text-[11px] leading-snug text-gray-500">{t.description}</p>
          </div>
        ))}
        {tools.length === 0 && (
          <p className="text-xs text-gray-600">Tool list unavailable.</p>
        )}
      </div>

      {/* Run panel */}
      <div className="flex flex-1 flex-col overflow-hidden">
        <div className="border-b border-gray-800 p-4">
          <div className="flex items-center gap-2">
            <Bot className="w-5 h-5 text-pink-400" />
            <h2 className="text-sm font-semibold text-white">Deterministic Agent</h2>
            <span className="text-[11px] text-gray-500">$0 · no LLM · reproducible</span>
          </div>
          <div className="mt-3 flex gap-2">
            <input
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") run(); }}
              placeholder="Goal, e.g. map the authentication flow and its dependents…"
              className="flex-1 rounded-xl border border-gray-700 bg-gray-800 px-4 py-2.5 text-sm text-white placeholder-gray-500 focus:border-pink-500 focus:outline-none"
            />
            <button
              onClick={run}
              disabled={running || !goal.trim()}
              className="flex items-center gap-1.5 rounded-xl bg-pink-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-pink-500 disabled:bg-gray-700 disabled:cursor-not-allowed"
            >
              {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
              Run
            </button>
          </div>
          {error && (
            <div className="mt-2 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
              <AlertTriangle className="w-3.5 h-3.5" /> {error}
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {steps.length === 0 && !report && !running && (
            <p className="text-sm text-gray-600">
              Describe a code-investigation goal. The agent searches the index, reads the top
              files, maps dependents, and writes a findings report — all deterministic.
            </p>
          )}

          {/* Step timeline */}
          {steps.length > 0 && (
            <ol className="mb-4 space-y-1">
              {steps.map((s, i) => (
                <li key={i} className="flex items-start gap-2 text-xs text-gray-400">
                  {s.step === "tool_error"
                    ? <AlertTriangle className="w-3.5 h-3.5 shrink-0 text-red-400" />
                    : s.step === "complete"
                      ? <CheckCircle2 className="w-3.5 h-3.5 shrink-0 text-emerald-400" />
                      : <ChevronRight className="w-3.5 h-3.5 shrink-0 text-pink-400" />}
                  <span className="truncate">
                    {s.tool && <span className="mr-1 font-mono text-pink-300">{s.tool}</span>}
                    {s.message}
                  </span>
                </li>
              ))}
            </ol>
          )}

          {/* Report */}
          {report && (
            <div className="prose prose-invert prose-sm max-w-none rounded-xl border border-gray-800 bg-gray-900 p-5">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{report}</ReactMarkdown>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}

export default AgentPanel;
