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

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { Network, Search, RefreshCw, Loader2, Info, X, ShieldAlert, Flame, TestTube2, ArrowUpRight, Zap, Filter, Download, Eye, EyeOff, Layers } from "lucide-react";
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
  // Blast Radius state — Interactive Blast Radius Map
  const [blastData, setBlastData]       = useState<{ impact: any; file: string } | null>(null);
  const [blastLoading, setBlastLoading] = useState(false);
  // P2 Graph Enhancements — filters
  const [langFilter, setLangFilter]     = useState<string>("");
  const [depthFilter, setDepthFilter]   = useState<number>(0); // 0 = all
  const [isolateMode, setIsolateMode]   = useState(false);
  const [showLabels, setShowLabels]     = useState(false);

  // Derived: map backend impacted_files → node ids for highlighting
  const blastIds = useMemo(() => {
    if (!blastData?.impact?.impacted_files) return null;
    const impacted = blastData.impact.impacted_files as string[];
    const set = new Set<string>();
    const nodes = graphData?.nodes ?? [];
    const byLabel = new Map<string, string[]>();
    nodes.forEach((n) => {
      const k = n.label;
      if (!byLabel.has(k)) byLabel.set(k, []);
      byLabel.get(k)!.push(n.id);
    });
    for (const p of impacted) {
      const base = p.split("/").pop() ?? p;
      const ids = byLabel.get(base) ?? [];
      ids.forEach((id) => set.add(id));
      nodes.forEach((n) => {
        if (n.id.endsWith(p) || p.endsWith(n.id) || n.label === p) set.add(n.id);
      });
    }
    // always include the selected node itself in blast radius
    if (selectedNode) set.add(selectedNode.id);
    return set;
  }, [blastData, graphData, selectedNode]);

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

  // ── Blast Radius: fetch impact for selected node ───────────────────────
  useEffect(() => {
    if (!selectedNode) {
      setBlastData(null);
      return;
    }
    const fileParam = selectedNode.label;
    const url = activeRepoUrl
      ? `/api/v1/ingest/blast-radius?repo_url=${encodeURIComponent(activeRepoUrl)}&file=${encodeURIComponent(fileParam)}`
      : `/api/v1/ingest/blast-radius?file=${encodeURIComponent(fileParam)}`;
    setBlastLoading(true);
    apiFetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`Blast ${res.status}`);
        const data = await res.json();
        setBlastData({ impact: data.impact, file: data.file });
      })
      .catch(() => setBlastData(null))
      .finally(() => setBlastLoading(false));
  }, [selectedNode, activeRepoUrl]);

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

  // ── P2 Graph Filters — lang + depth + isolate ──────────────────────────
  const filteredGraph = (() => {
    if (!graphData) return null;
    let nodes = [...graphData.nodes];
    let edges = [...graphData.edges];
    // Language filter
    if (langFilter) {
      nodes = nodes.filter((n) => n.language === langFilter);
      const nodeIds = new Set(nodes.map((n) => n.id));
      edges = edges.filter((e) => {
        const src = typeof e.source === "object" ? (e.source as any).id : e.source;
        const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
        return nodeIds.has(src) && nodeIds.has(tgt);
      });
    }
    // Depth filter (BFS from selectedNode)
    if (depthFilter > 0 && selectedNode) {
      const adj: Record<string, string[]> = {};
      edges.forEach((e) => {
        const src = typeof e.source === "object" ? (e.source as any).id : e.source;
        const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
        (adj[src] ||= []).push(tgt);
        (adj[tgt] ||= []).push(src);
      });
      const visited = new Set<string>([selectedNode.id]);
      let frontier = [selectedNode.id];
      for (let d = 0; d < depthFilter; d++) {
        const next: string[] = [];
        frontier.forEach((id) => (adj[id] || []).forEach((nb) => { if (!visited.has(nb)) { visited.add(nb); next.push(nb); } }));
        frontier = next;
      }
      nodes = nodes.filter((n) => visited.has(n.id));
      edges = edges.filter((e) => {
        const src = typeof e.source === "object" ? (e.source as any).id : e.source;
        const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
        return visited.has(src) && visited.has(tgt);
      });
    }
    // Isolate mode (selected + blast radius only)
    if (isolateMode && selectedNode && blastIds) {
      const keep = new Set<string>(blastIds);
      nodes = nodes.filter((n) => keep.has(n.id));
      edges = edges.filter((e) => {
        const src = typeof e.source === "object" ? (e.source as any).id : e.source;
        const tgt = typeof e.target === "object" ? (e.target as any).id : e.target;
        return keep.has(src) && keep.has(tgt);
      });
    }
    return { nodes, edges, stats: { files: nodes.length, dependencies: edges.length } };
  })();

  const displayData = filteredGraph || graphData;

  // Export PNG helper
  const handleExportPng = () => {
    try {
      const canvas = document.querySelector("canvas") as HTMLCanvasElement;
      if (!canvas) return;
      const url = canvas.toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url; a.download = `graph-${activeRepoUrl ? new URL(activeRepoUrl).pathname.split("/").pop() : "repo"}.png`; a.click();
    } catch {}
  };

  // ── Node paint function ────────────────────────────────────────────────────
  const paintNode = useCallback((node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const n = node as GraphNode;
    const baseColor = LANG_COLOR[n.language] ?? DEFAULT_COLOR;
    const r = Math.sqrt(Math.max(0, n.val ?? 1)) * 3 + 3;

    const isHovered   = hoveredNode?.id === n.id;
    const isNeighbour = hoveredNode ? neighbourIds(hoveredNode.id).has(n.id) : false;
    const isSelected  = selectedNode?.id === n.id;
    const isSearchDim = matchingIds !== null && !matchingIds.has(n.id);
    const isBlast     = blastIds?.has(n.id) ?? false;
    const inBlastMode = blastIds !== null && selectedNode !== null;

    // Determine opacity: search > hover > blast > default
    let opacity = 1;
    if (isSearchDim) opacity = 0.15;
    else if (hoveredNode) opacity = (!isHovered && !isNeighbour) ? 0.3 : 1;
    else if (inBlastMode) opacity = isBlast ? 1 : 0.12;

    ctx.save();
    ctx.globalAlpha = opacity;

    // Outer glow for selected / hovered / blast
    if (isSelected || isHovered || isBlast) {
      ctx.beginPath();
      const glowR = isSelected ? r + 5 : isBlast ? r + 4 : r + 3;
      ctx.arc(n.x!, n.y!, glowR, 0, 2 * Math.PI);
      if (isSelected) ctx.fillStyle = "#ffffff44";
      else if (isBlast) {
        ctx.fillStyle =
          blastData?.impact?.risk_level === "high" ? "#ef444488" :
          blastData?.impact?.risk_level === "medium" ? "#f59e0b88" : "#fbbf2488";
      } else ctx.fillStyle = `${baseColor}44`;
      ctx.fill();
    }

    // Blast ring for impacted nodes (not the root)
    if (isBlast && !isSelected) {
      ctx.beginPath();
      ctx.arc(n.x!, n.y!, r + 2.2, 0, 2 * Math.PI);
      ctx.strokeStyle =
        blastData?.impact?.risk_level === "high" ? "#ef4444" :
        blastData?.impact?.risk_level === "medium" ? "#f59e0b" : "#fbbf24";
      ctx.lineWidth = 1.3;
      ctx.stroke();
    }

    // Main circle
    ctx.beginPath();
    ctx.arc(n.x!, n.y!, r, 0, 2 * Math.PI);
    ctx.fillStyle = baseColor;
    ctx.fill();

    // Border
    if (isBlast && !isSelected) {
      ctx.strokeStyle =
        blastData?.impact?.risk_level === "high" ? "#ef4444" :
        blastData?.impact?.risk_level === "medium" ? "#f59e0b" : "#fbbf24";
      ctx.lineWidth = 1.4;
      ctx.stroke();
    } else {
      ctx.strokeStyle = isSelected ? "#ffffff" : isNeighbour ? baseColor : "#1f2937";
      ctx.lineWidth = isSelected ? 2 : 1;
      ctx.stroke();
    }

    // Label — at zoom or when hovered/selected/blast or showLabels toggle
    if (showLabels || globalScale > 1.2 || isHovered || isSelected || isBlast) {
      const label = n.label.length > 20 ? n.label.slice(0, 18) + "…" : n.label;
      const fontSize = Math.max(8, 11 / globalScale);
      ctx.font = `${isSelected ? "bold " : isBlast ? "600 " : ""}${fontSize}px sans-serif`;
      ctx.fillStyle = isSearchDim ? "#4b5563" : isBlast ? "#fde68a" : "#e5e7eb";
      ctx.textAlign = "center";
      ctx.fillText(label, n.x!, n.y! + r + fontSize + 2);
    }

    ctx.restore();
  }, [hoveredNode, selectedNode, neighbourIds, matchingIds, blastIds, blastData, showLabels]);

  // ── Edge paint function ───────────────────────────────────────────────────
  const paintLink = useCallback((link: any, ctx: CanvasRenderingContext2D) => {
    const srcId = typeof link.source === "object" ? link.source.id : link.source;
    const tgtId = typeof link.target === "object" ? link.target.id : link.target;
    const isHoverActive = hoveredNode ? srcId === hoveredNode.id || tgtId === hoveredNode.id : false;
    const isSelectedActive = !hoveredNode && selectedNode ? srcId === selectedNode.id || tgtId === selectedNode.id : false;
    const isBlastEdge = blastIds ? blastIds.has(srcId) && blastIds.has(tgtId) : false;

    if (isHoverActive || isSelectedActive) {
      ctx.strokeStyle = "#60a5fa";
      ctx.lineWidth   = 1.5;
      ctx.globalAlpha = 0.9;
    } else if (isBlastEdge) {
      ctx.strokeStyle =
        blastData?.impact?.risk_level === "high" ? "#ef4444" :
        blastData?.impact?.risk_level === "medium" ? "#f59e0b" : "#fbbf24";
      ctx.lineWidth   = 1.4;
      ctx.globalAlpha = 0.85;
    } else {
      ctx.strokeStyle = "#374151";
      ctx.lineWidth   = 0.5;
      ctx.globalAlpha = blastIds ? 0.12 : 0.35;
    }
  }, [hoveredNode, selectedNode, blastIds, blastData]);

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
            {displayData
              ? `${displayData.stats?.files ?? displayData.nodes.length} files · ${displayData.stats?.dependencies ?? displayData.edges.length} import edges${ (langFilter || depthFilter || isolateMode) ? " (filtered)" : ""}`
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
          {/* P2 Filters */}
          <div className="flex items-center gap-1.5">
            <div className="relative">
              <Filter className="absolute left-2 top-2 w-3 h-3 text-gray-500" />
              <select value={langFilter} onChange={(e)=>setLangFilter(e.target.value)} className="bg-gray-800 text-white text-xs rounded-lg pl-7 pr-2 py-1.5 border border-gray-700 focus:outline-none focus:border-purple-500">
                <option value="">All langs</option>
                <option value="py">Python</option>
                <option value="js">JS</option>
                <option value="ts">TS</option>
                <option value="go">Go</option>
                <option value="rs">Rust</option>
                <option value="java">Java</option>
              </select>
            </div>
            <select value={depthFilter} onChange={(e)=>setDepthFilter(parseInt(e.target.value))} className="bg-gray-800 text-white text-xs rounded-lg px-2 py-1.5 border border-gray-700 focus:outline-none focus:border-purple-500" title="Depth from selected">
              <option value={0}>Depth: all</option>
              <option value={1}>Depth: 1</option>
              <option value={2}>Depth: 2</option>
              <option value={3}>Depth: 3</option>
            </select>
            <button onClick={()=>setIsolateMode(!isolateMode)} disabled={!selectedNode || !blastIds} className={`p-1.5 rounded border text-xs ${isolateMode ? "bg-purple-600 border-purple-500 text-white" : "bg-gray-800 border-gray-700 text-gray-500 hover:text-white"}`} title="Isolate blast radius">
              <Layers className="w-3.5 h-3.5" />
            </button>
            <button onClick={()=>setShowLabels(!showLabels)} className={`p-1.5 rounded border text-xs ${showLabels ? "bg-purple-600 border-purple-500 text-white" : "bg-gray-800 border-gray-700 text-gray-500 hover:text-white"}`} title="Toggle labels">
              {showLabels ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
            </button>
            <button onClick={handleExportPng} className="p-1.5 rounded bg-gray-800 border border-gray-700 text-gray-500 hover:text-white" title="Export PNG">
              <Download className="w-3.5 h-3.5" />
            </button>
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

          {displayData && !loading && displayData.nodes.length > 0 && (
            <ForceGraph2D
              ref={fgRef}
              width={dimensions.width}
              height={dimensions.height}
              graphData={{
                nodes: displayData.nodes as any[],
                links: displayData.edges as any[],
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
                .filter(([lang]) => (displayData || graphData)?.nodes.some((n) => n.language === lang))
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
          <div className="w-72 shrink-0 border-l border-gray-700 bg-gray-900 flex flex-col overflow-hidden">
            <div className="px-4 py-3 border-b border-gray-700 flex items-center justify-between shrink-0">
              <span className="text-xs font-semibold text-white flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5 text-purple-400" />
                File Details
              </span>
              <button onClick={() => setSelectedNode(null)} className="text-gray-500 hover:text-white">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              <div>
                <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">File</p>
                <p className="text-sm font-medium text-white break-all">{selectedNode.label}</p>
              </div>
              <div className="flex gap-4">
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
                  <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-0.5">Chunks</p>
                  <p className="text-sm text-gray-300">{selectedNode.val}</p>
                </div>
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

              {/* ── Blast Radius Panel ───────────────────────────── */}
              <div className="rounded-xl border bg-gray-800/80 p-3 space-y-2.5"
                   style={{
                     borderColor: blastData?.impact?.risk_level === "high" ? "#ef444455"
                       : blastData?.impact?.risk_level === "medium" ? "#f59e0b55" : "#374151",
                   }}>
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-white flex items-center gap-1.5">
                    <Flame className={`w-3.5 h-3.5 ${blastData?.impact?.risk_level === "high" ? "text-red-400" : blastData?.impact?.risk_level === "medium" ? "text-amber-400" : "text-amber-300"}`} />
                    Blast Radius
                  </span>
                  {blastLoading ? (
                    <span className="flex items-center gap-1 text-[10px] text-gray-500"><Loader2 className="w-3 h-3 animate-spin" /> analysing…</span>
                  ) : blastData ? (
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full uppercase tracking-wide flex items-center gap-1
                      ${blastData.impact.risk_level === "high" ? "bg-red-500/20 text-red-400 border border-red-500/30"
                        : blastData.impact.risk_level === "medium" ? "bg-amber-500/20 text-amber-400 border border-amber-500/30"
                        : "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"}`}>
                      {blastData.impact.risk_level === "high" && <ShieldAlert className="w-3 h-3" />}
                      {blastData.impact.risk_level}
                    </span>
                  ) : null}
                </div>

                {blastLoading && !blastData ? (
                  <div className="flex items-center gap-2 text-xs text-gray-500 py-2">
                    <Zap className="w-3.5 h-3.5 text-amber-400 animate-pulse" />
                    Computing transitive dependents…
                  </div>
                ) : blastData ? (
                  <>
                    <p className="text-xs text-gray-400 leading-relaxed">{blastData.impact.summary}</p>

                    <div>
                      <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-1 flex items-center gap-1">
                        <ArrowUpRight className="w-3 h-3" />
                        Impacted files — {blastData.impact.impacted_files.length} dependents
                        {blastIds && blastIds.size > 0 && <span className="text-amber-400">· {blastIds.size} highlighted</span>}
                      </p>
                      {blastData.impact.impacted_files.length > 0 ? (
                        <ul className="space-y-1 max-h-28 overflow-y-auto pr-1">
                          {blastData.impact.impacted_files.slice(0, 12).map((f: string) => (
                            <li key={f} className="text-xs text-amber-300/90 truncate flex items-center gap-1.5">
                              <span className="w-1 h-1 rounded-full bg-amber-400 shrink-0" />{f}
                            </li>
                          ))}
                          {blastData.impact.impacted_files.length > 12 && (
                            <li className="text-[10px] text-gray-500">+{blastData.impact.impacted_files.length - 12} more</li>
                          )}
                        </ul>
                      ) : (
                        <p className="text-xs text-emerald-400">No downstream dependents — isolated file.</p>
                      )}
                    </div>

                    {/* CVE & security flags (P0 #5.3) */}
                    {blastData.impact.security_flags?.length > 0 && (
                      <div>
                        <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-1 flex items-center gap-1">
                          <ShieldAlert className="w-3 h-3 text-red-400" /> Security flags · {blastData.impact.security_flags.length}
                        </p>
                        <ul className="space-y-1 max-h-32 overflow-y-auto pr-1">
                          {blastData.impact.security_flags.slice(0, 10).map((f: any, i: number) => {
                            const isCve = f.rule === "cve";
                            const isManifest = f.rule === "dependency-manifest-changed";
                            const sev = (f.severity || "").toLowerCase();
                            const sevColor = sev === "critical" ? "text-red-300" : sev === "high" ? "text-orange-300" : sev === "medium" || sev === "moderate" ? "text-amber-300" : "text-gray-400";
                            return (
                              <li key={i} className="text-xs flex items-start gap-1.5">
                                <span className={isCve ? "text-red-400 mt-0.5 shrink-0" : isManifest ? "text-amber-400 mt-0.5 shrink-0" : "text-gray-500 mt-0.5 shrink-0"}>•</span>
                                <span className="truncate">
                                  {isCve ? (
                                    <>
                                      <span className="text-red-300 font-medium">{f.package}</span>
                                      <span className="text-gray-500"> {f.installed_version || ""}</span>
                                      <span className="text-gray-400"> — {f.cve}</span>
                                      <span className={sevColor + " ml-1"}>[{f.severity}]</span>
                                      {f.fix_versions?.length > 0 && <span className="text-emerald-400 ml-1">→ {f.fix_versions[0]}</span>}
                                    </>
                                  ) : isManifest ? (
                                    <span className="text-amber-300">{f.message || "manifest changed"}</span>
                                  ) : (
                                    <span className="text-gray-400">{f.rule} ×{f.matches ?? 1}</span>
                                  )}
                                </span>
                              </li>
                            );
                          })}
                          {blastData.impact.security_flags.length > 10 && (
                            <li className="text-[10px] text-gray-500">+{blastData.impact.security_flags.length - 10} more</li>
                          )}
                        </ul>
                      </div>
                    )}

                    {blastData.impact.test_suggestions?.length > 0 && (
                      <div>
                        <p className="text-[10px] text-gray-500 uppercase tracking-wide mb-1 flex items-center gap-1">
                          <TestTube2 className="w-3 h-3" /> Suggested tests
                        </p>
                        <ul className="space-y-1">
                          {blastData.impact.test_suggestions.slice(0, 5).map((t: string, i: number) => (
                            <li key={i} className="text-xs text-cyan-300/90 truncate">• {t}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </>
                ) : (
                  <p className="text-xs text-gray-600">Select a file to see its blast radius.</p>
                )}
              </div>

              <button
                onClick={() => { onNavigateToReview(selectedNode.id); setSelectedNode(null); }}
                className="w-full bg-yellow-500 hover:bg-yellow-400 text-gray-900 text-xs font-semibold py-2.5 rounded-lg transition-colors flex items-center justify-center gap-1.5"
              >
                Review this file <ArrowUpRight className="w-3.5 h-3.5" />
              </button>
              <p className="text-[10px] text-gray-600 text-center">Click another node to compare blast radius · nodes dim outside radius</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
