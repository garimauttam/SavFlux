/**
 * ArchitectureDiagram.tsx — Auto architecture diagram (P1 #3).
 *
 * Rendered in the chat empty-state: a collapsible card showing a layered
 * SVG map of the repo's hub files (derived from the RAG dependency graph,
 * $0 deterministic) plus the Mermaid `graph TD` source with copy/download.
 */

import { useEffect, useState } from "react";
import {
  Network, Loader2, ChevronDown, ChevronUp, Copy, Check, Download,
  AlertTriangle,
} from "lucide-react";
import { apiFetch } from "../api";

interface DiagramNode {
  id: string;
  label: string;
  language: string;
  degree: number;
  layer: string;
}

interface DiagramEdge {
  source: string;
  target: string;
}

interface DiagramLayer {
  name: string;
  files: string[];
}

interface DiagramData {
  mermaid: string;
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  layers: DiagramLayer[];
  stats: { files: number; shown: number; dependencies: number };
}

interface ArchitectureDiagramProps {
  activeRepoUrl: string | null;
  hasIndexedFiles: boolean;
}

// Deterministic layout: layers become columns, files stack vertically.
function layout(nodes: DiagramNode[], layers: DiagramLayer[]) {
  const COL_W = 150, ROW_H = 30, PAD = 16;
  const pos = new Map<string, { x: number; y: number }>();
  layers.forEach((layer, li) => {
    const members = nodes.filter((n) => n.layer === layer.name);
    members.forEach((n, i) => {
      pos.set(n.id, { x: PAD + li * COL_W, y: PAD + i * ROW_H });
    });
  });
  const width = PAD * 2 + Math.max(1, layers.length) * COL_W;
  const height = PAD * 2 + Math.max(...layers.map((l) => l.files.length), 1) * ROW_H;
  return { pos, width, height };
}

export function ArchitectureDiagram({ activeRepoUrl, hasIndexedFiles }: ArchitectureDiagramProps) {
  const [data, setData] = useState<DiagramData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(true);
  const [showMermaid, setShowMermaid] = useState(false);
  const [copied, setCopied] = useState(false);
  const [selected, setSelected] = useState<DiagramNode | null>(null);

  useEffect(() => {
    if (!hasIndexedFiles) return;
    setLoading(true);
    setError(null);
    const url = activeRepoUrl
      ? `/api/v1/architecture/diagram?repo_url=${encodeURIComponent(activeRepoUrl)}&max_nodes=30`
      : "/api/v1/architecture/diagram?max_nodes=30";
    apiFetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`Server error ${res.status}`);
        setData(await res.json());
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Diagram failed"))
      .finally(() => setLoading(false));
  }, [activeRepoUrl, hasIndexedFiles]);

  if (!hasIndexedFiles) return null;

  const copy = async () => {
    if (!data) return;
    try {
      await navigator.clipboard.writeText(data.mermaid);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {}
  };

  const download = () => {
    if (!data) return;
    const blob = new Blob([data.mermaid], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "architecture.mmd";
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  };

  const { pos, width, height } = data ? layout(data.nodes, data.layers) : { pos: new Map(), width: 0, height: 0 };

  return (
    <div className="w-full max-w-2xl overflow-hidden rounded-2xl border border-gray-700 bg-gray-900 text-left">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-3 hover:bg-gray-800/50"
      >
        <Network className="h-4 w-4 text-purple-400" />
        <span className="text-sm font-semibold text-white">Architecture map</span>
        {data && (
          <span className="text-xs text-gray-500">
            {data.stats.shown} of {data.stats.files} files · {data.layers.length} layers
          </span>
        )}
        <span className="ml-auto text-gray-500">
          {open ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </span>
      </button>

      {open && (
        <div className="border-t border-gray-800 p-4">
          {loading && (
            <p className="flex items-center gap-2 text-sm text-gray-400">
              <Loader2 className="h-4 w-4 animate-spin" /> Mapping architecture…
            </p>
          )}
          {error && (
            <p className="flex items-center gap-2 text-sm text-amber-300">
              <AlertTriangle className="h-4 w-4" /> {error}
            </p>
          )}

          {data && data.nodes.length > 0 && (
            <>
              <div className="overflow-x-auto rounded-xl border border-gray-800 bg-gray-950">
                <svg width={width} height={height} className="min-w-full">
                  {data.edges.map((e, i) => {
                    const a = pos.get(e.source);
                    const b = pos.get(e.target);
                    if (!a || !b) return null;
                    return (
                      <line
                        key={i}
                        x1={a.x + 130} y1={a.y + 11} x2={b.x} y2={b.y + 11}
                        stroke="#6d28d9" strokeOpacity="0.45" strokeWidth="1.2"
                      />
                    );
                  })}
                  {data.layers.map((layer, li) => (
                    <text key={layer.name} x={16 + li * 150} y={12} fill="#6b7280" fontSize="10" fontFamily="monospace">
                      {layer.name}/
                    </text>
                  ))}
                  {data.nodes.map((n) => {
                    const p = pos.get(n.id);
                    if (!p) return null;
                    const isSel = selected?.id === n.id;
                    return (
                      <g key={n.id} onClick={() => setSelected(isSel ? null : n)} className="cursor-pointer">
                        <rect
                          x={p.x} y={p.y} width="130" height="22" rx="5"
                          fill={isSel ? "#4c1d95" : "#1f2937"}
                          stroke={isSel ? "#a78bfa" : "#4b5563"}
                          strokeWidth="1"
                        />
                        <title>{`${n.label} — ${n.degree} connections`}</title>
                        <text x={p.x + 7} y={p.y + 15} fill={isSel ? "#fff" : "#d1d5db"} fontSize="10.5" fontFamily="monospace">
                          {n.label.length > 17 ? n.label.slice(0, 16) + "…" : n.label}
                        </text>
                      </g>
                    );
                  })}
                </svg>
              </div>

              {selected && (
                <p className="mt-2 truncate font-mono text-xs text-gray-400" title={selected.id}>
                  <span className="text-purple-300">{selected.label}</span>
                  {" "}· {selected.degree} connections · layer {selected.layer}
                </p>
              )}

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  onClick={() => setShowMermaid((v) => !v)}
                  className="rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 text-xs text-gray-300 hover:text-white"
                >
                  {showMermaid ? "Hide" : "View"} Mermaid source
                </button>
                <button
                  onClick={copy}
                  className="flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 text-xs text-gray-300 hover:text-white"
                >
                  {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                  {copied ? "Copied" : "Copy .mmd"}
                </button>
                <button
                  onClick={download}
                  className="flex items-center gap-1.5 rounded-lg border border-gray-700 bg-gray-800 px-2.5 py-1.5 text-xs text-gray-300 hover:text-white"
                >
                  <Download className="h-3.5 w-3.5" /> architecture.mmd
                </button>
              </div>

              {showMermaid && (
                <pre className="mt-2 max-h-56 overflow-auto rounded-xl border border-gray-800 bg-gray-950 p-3 font-mono text-[11px] leading-relaxed text-gray-300">
                  {data.mermaid}
                </pre>
              )}
            </>
          )}

          {data && data.nodes.length === 0 && !loading && (
            <p className="text-sm text-gray-500">Index a repo to generate its architecture map.</p>
          )}
        </div>
      )}
    </div>
  );
}

export default ArchitectureDiagram;
