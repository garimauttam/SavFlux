/**
 * GraphPanel.tsx — Interactive file dependency graph.
 *
 * Uses react-force-graph-2d to render a force-directed graph where:
 *   - Each node  = an indexed file  (size ∝ number of chunks)
 *   - Each edge  = an import relationship (A imports B)
 *   - Node color = language (Python=blue, JS=yellow, TS=cyan, Go=teal, etc.)
 *
 * Interactions:
 *   - Click a node      → highlights it + shows its file name in info panel
 *   - Double-click node → navigates to Code Review tab for that file
 *   - Hover             → highlights direct neighbours (imports + importers)
 *   - Search box        → filters nodes by file name, dims non-matching
 *
 * WHY THIS IS INTERVIEW-IMPRESSIVE:
 *   Most code intelligence tools show a flat file list. A dependency graph
 *   answers the question every engineer has on a new codebase: "what connects
 *   to what?" It's the visual that makes reviewers stop and say "oh, this is real."
 */

import { useState, useEffect, useCallback, useRef } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { Network, Search, RefreshCw, Loader2, Info, X } from "lucide-react";
import { IndexedRepo } from "../types";
import { apiFetch } from "../api";

// ── Language colour palette ───────────────────────────────────────────────────
const LANG_COLOR: Record<string, string> = {
  py:   "#60a5fa",  // blue-400
  js:   "#fde047",  // yellow-300
  jsx:  "#fde047",
  ts:   "#67e8f9",  // cyan-300
  tsx:  "#67e8f9",
  go:   "#34d399",  // emerald-400
  java: "#fb923c",  // orange-400
  rs:   "#f87171",  // red-400
  rb:   "#e879f9",  // fuchsia-400
  md:   "#9ca3af",  // gray-400
  json: "#86efac",  // green-300
  yaml: "#86efac",
  yml:  "#86efac",
  txt:  "#6b7280",  // gray-500
};
const DEFAULT_COLOR = "#8b5cf6"; // purple-500 — fallback

interface GraphNode {
  id: string;
  label: string;
  language: string;
  val: number;
  // runtime fields added by force-graph
  x?: number;
  y?: number;
  fx?: number;
  fy?: number;
  color?: string;
}

interface GraphEdge {
  source: string;
  target: string;
}

interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats?: { files: number; dependencies: number };
}

interface GraphPanelProps {
  indexedRepos: IndexedRepo[];
  activeRepoUrl: string | null;
  /** Called when user double-clicks a node — navigates to Code Review */
  onNavigateToReview: (fileSource: string) => void;
}

export function GraphPanel({ indexedRepos, activeRepoUrl, onNavigateToReview }: GraphPanelProps) {
  const [graphData, setGraphData]       = useState<GraphData | null>(null);
  const [loading, setLoading]           = useState(false);
  const [error, setError]               = useState<string | null>(null);
  const [search, setSearch]             = useState("");
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredNode, setHoveredNode]   = useState<GraphNode | null>(null);

  const lastClickRef = useRef<{ id: string; time: number } | null>(null);
  const fgRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });

  // Track container size for the canvas
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      setDimensions({ width: el.offsetWidth, height: el.offsetHeight });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // ── Fetch graph data ────────────────────────────────────────────────────────
  const fetchGraph = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSelectedNode(null);
    try {
      const path = activeRepoUrl
        ? `/api/v1/ingest/dependency-graph?repo_url=${encodeURIComponent(activeRepoUrl)}`
        : "/api/v1/ingest/dependency-graph";
      const res = await apiFetch(path);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const data: GraphData = await res.json();
      setGraphData(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load graph");
    } finally {
      setLoading(false);
    }
  }, [activeRepoUrl]);

  // Auto-fetch when repo changes or on first mount
  useEffect(() => {
    if (indexedRepos.length > 0) fetchGraph();
  }, [fetchGraph, indexedRepos.length]);

  // ── Derived: highlighted neighbours for hover ────────────────────────────
  const neighbourIds = useCallback((nodeId: string): Set<string> => {
    if (!graphData) return new Set();
    const ids = new Set<string>();
    for (const e of graphData.edges) {
      const src = typeof e.source === "object" ? (e.source as any).id : e.source;
      const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
      if (src === nodeId) ids.add(tgt);
      if (tgt === nodeId) ids.add(src);
    }
    return ids;
  }, [graphData]);

  // ── Search filter ────────────────────────────────────────────────────────
  const searchLower = search.toLowerCase();
  const matchingIds = search
    ? new Set(graphData?.nodes.filter((n) => n.label.toLowerCase().includes(searchLower)).map((n) => n.id))
    : null;

  // ── Node paint function ────────────────────────────────────────────────────
  const paintNode = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const n = node as GraphNode;
    const baseColor = LANG_COLOR[n.language] ?? DEFAULT_COLOR;
    const r = Math.sqrt(Math.max(0, n.val ?? 1)) * 3 + 3;

    const isHovered   = hoveredNode?.id === n.id;
    const isNeighbour = hoveredNode ? neighbourIds(hoveredNode.id).has(n.id) : false;
    const isSelected  = selectedNode?.id === n.id;
    const isSearchDim = matchingIds !== null && !matchingIds.has(n.id);

    // Determine opacity
    const opacity = isSearchDim ? 0.15 : (hoveredNode && !isHovered && !isNeighbour) ? 0.3 : 1;

    ctx.save();
    ctx.globalAlpha = opacity;

    // Outer glow for selected/hovered
    if (isSelected || isHovered) {
      ctx.beginPath();
      ctx.arc(n.x!, n.y!, r + 4, 0, 2 * Math.PI);
      ctx.fillStyle = isSelected ? "#ffffff44" : `${baseColor}44`;
      ctx.fill();
    }

    // Main circle
    ctx.beginPath();
    ctx.arc(n.x!, n.y!, r, 0, 2 * Math.PI);
    ctx.fillStyle = baseColor;
    ctx.fill();

    // White border
    ctx.strokeStyle = isSelected ? "#ffffff" : isNeighbour ? baseColor : "#1f2937";
    ctx.lineWidth = isSelected ? 2 : 1;
    ctx.stroke();

    // Label — only at reasonable zoom or when hovered
    if (globalScale > 1.2 || isHovered || isSelected) {
      const label = n.label.length > 20 ? n.label.slice(0, 18) + "…" : n.label;
      const fontSize = Math.max(8, 11 / globalScale);
      ctx.font = `${isSelected ? "bold " : ""}${fontSize}px sans-serif`;
      ctx.fillStyle = isSearchDim ? "#4b5563" : "#e5e7eb";
      ctx.textAlign = "center";
      ctx.fillText(label, n.x!, n.y! + r + fontSize + 2);
    }

    ctx.restore();
  }, [hoveredNode, selectedNode, neighbourIds, matchingIds]);

  // ── Edge paint function ───────────────────────────────────────────────────
  const paintLink = useCallback((link: any, ctx: CanvasRenderingContext2D) => {
    const srcId = typeof link.source === "object" ? link.source.id : link.source;
    const tgtId = typeof link.target === "object" ? link.target.id : link.target;
    const isActive = hoveredNode
      ? srcId === hoveredNode.id || tgtId === hoveredNode.id
      : selectedNode
      ? srcId === selectedNode.id || tgtId === selectedNode.id
      : false;

    ctx.strokeStyle = isActive ? "#60a5fa" : "#374151";
    ctx.lineWidth   = isActive ? 1.5 : 0.5;
    ctx.globalAlpha = isActive ? 0.9 : 0.35;
  }, [hoveredNode, selectedNode]);

  if (indexedRepos.length === 0) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center p-8">
        <div className="w-14 h-14 rounded-2xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center">
          <Network className="w-7 h-7 text-purple-400" />
        </div>
        <div>
          <h3 className="text-white font-semibold mb-1">No repo indexed yet</h3>
          <p className="text-sm text-gray-500">Index a GitHub repo to see its dependency graph.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">

      {/* Header */}
      <div className="px-6 py-3 border-b border-gray-700 bg-gray-900 flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Network className="w-4 h-4 text-purple-400" />
            Dependency Graph
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {graphData
              ? `${graphData.stats?.files ?? graphData.nodes.length} files · ${graphData.stats?.dependencies ?? graphData.edges.length} import edges`
              : "File import relationships extracted from indexed code"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* Search */}
          <div className="relative">
            <Search className="absolute left-2.5 top-2 w-3.5 h-3.5 text-gray-500" />
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter files..."
              className="bg-gray-800 text-white text-xs rounded-lg pl-8 pr-3 py-1.5 border border-gray-700 focus:outline-none focus:border-purple-500 w-36"
            />
            {search && (
              <button onClick={() => setSearch("")} className="absolute right-2 top-2">
                <X className="w-3 h-3 text-gray-500 hover:text-white" />
              </button>
            )}
          </div>
          {/* Refresh */}
          <button
            onClick={fetchGraph}
            disabled={loading}
            className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-purple-400 transition-colors disabled:opacity-40"
            title="Refresh graph"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">

        {/* Graph canvas */}
        <div ref={containerRef} className="flex-1 relative bg-gray-950">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center z-10">
              <div className="flex items-center gap-2 text-sm text-gray-400">
                <Loader2 className="w-4 h-4 animate-spin" />
                Building dependency graph...
              </div>
            </div>
          )}

          {error && (
            <div className="absolute inset-0 flex items-center justify-center z-10">
              <div className="text-red-400 text-sm bg-red-900/20 border border-red-800 rounded-lg px-4 py-3">
                {error}
              </div>
            </div>
          )}

          {graphData && !loading && graphData.nodes.length === 0 && (
            <div className="absolute inset-0 flex items-center justify-center">
              <p className="text-gray-500 text-sm">No files found for this repo.</p>
            </div>
          )}

          {graphData && !loading && graphData.nodes.length > 0 && (
            <ForceGraph2D
              ref={fgRef}
              width={dimensions.width}
              height={dimensions.height}
              graphData={{
                nodes: graphData.nodes as any[],
                links: graphData.edges as any[],
              }}
              nodeId="id"
              linkSource="source"
              linkTarget="target"
              nodeCanvasObject={paintNode}
              nodeCanvasObjectMode={() => "replace"}
              linkCanvasObject={paintLink}
              linkCanvasObjectMode={() => "replace"}
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={1}
              linkDirectionalArrowColor={() => "#4b5563"}
              onNodeHover={(node) => setHoveredNode(node as GraphNode | null)}
              onNodeClick={(node: any) => {
                const n = node as GraphNode;
                const now = Date.now();
                const last = lastClickRef.current;
                // Detect double-click: same node within 400ms
                if (last && last.id === n.id && now - last.time < 400) {
                  lastClickRef.current = null;
                  onNavigateToReview(n.id);
                } else {
                  lastClickRef.current = { id: n.id, time: now };
                  setSelectedNode((prev) => prev?.id === n.id ? null : n);
                }
              }}
              backgroundColor="#030712"
              cooldownTicks={120}
              onEngineStop={() => fgRef.current?.zoomToFit(400, 60)}
            />
          )}

          {/* Legend */}
          {graphData && !loading && (
            <div className="absolute bottom-4 left-4 bg-gray-900/90 border border-gray-700 rounded-lg p-3 text-xs space-y-1.5">
              <p className="text-gray-400 font-medium mb-2">Language</p>
              {Object.entries({py: "Python", js: "JavaScript", ts: "TypeScript", go: "Go", rs: "Rust", java: "Java"})
                .filter(([lang]) => graphData.nodes.some((n) => n.language === lang))
                .map(([lang, label]) => (
                  <div key={lang} className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: LANG_COLOR[lang] }} />
                    <span className="text-gray-400">{label}</span>
                  </div>
                ))}
              <p className="text-gray-600 mt-2 text-[10px]">Double-click → Review</p>
            </div>
          )}
        </div>

        {/* Node info sidebar — shown when a node is selected */}
        {selectedNode && (
          <div className="w-64 shrink-0 border-l border-gray-700 bg-gray-900 flex flex-col">
            <div className="px-4 py-3 border-b border-gray-700 flex items-center justify-between">
              <span className="text-xs font-semibold text-white flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5 text-purple-400" />
                File Details
              </span>
              <button onClick={() => setSelectedNode(null)} className="text-gray-500 hover:text-white">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="p-4 space-y-3">
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">File</p>
                <p className="text-sm font-medium text-white break-all">{selectedNode.label}</p>
              </div>
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">Language</p>
                <span
                  className="text-xs font-mono px-2 py-0.5 rounded"
                  style={{ background: `${LANG_COLOR[selectedNode.language] ?? DEFAULT_COLOR}22`, color: LANG_COLOR[selectedNode.language] ?? DEFAULT_COLOR }}
                >
                  {selectedNode.language}
                </span>
              </div>
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">Chunks indexed</p>
                <p className="text-sm text-gray-300">{selectedNode.val}</p>
              </div>
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">Imports</p>
                {(() => {
                    const imported = graphData?.nodes.filter((n) => {
                    const edgeExists = graphData.edges.some((e) => {
                      const src = typeof e.source === "object" ? (e.source as any).id : e.source;
                      return src === selectedNode.id && (typeof e.target === "object" ? (e.target as any).id : e.target) === n.id;
                    });
                    return edgeExists;
                  }) ?? [];
                  return imported.length > 0
                    ? <ul className="space-y-0.5">
                        {imported.map((n) => (
                          <li key={n.id} className="text-xs text-blue-400 truncate">→ {n.label}</li>
                        ))}
                      </ul>
                    : <p className="text-xs text-gray-600">No internal imports detected</p>;
                })()}
              </div>
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">Imported by</p>
                {(() => {
                  const importedBy = graphData?.nodes.filter((n) => {
                    return graphData.edges.some((e) => {
                      const src = typeof e.source === "object" ? (e.source as any).id : e.source;
                      const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
                      return src === n.id && tgt === selectedNode.id;
                    });
                  }) ?? [];
                  return importedBy.length > 0
                    ? <ul className="space-y-0.5">
                        {importedBy.map((n) => (
                          <li key={n.id} className="text-xs text-purple-400 truncate">← {n.label}</li>
                        ))}
                      </ul>
                    : <p className="text-xs text-gray-600">Not imported by other files</p>;
                })()}
              </div>

              <button
                onClick={() => { onNavigateToReview(selectedNode.id); setSelectedNode(null); }}
                className="w-full mt-2 bg-yellow-500 hover:bg-yellow-400 text-gray-900 text-xs font-semibold py-2 rounded-lg transition-colors"
              >
                Review this file →
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
