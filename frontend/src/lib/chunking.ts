/**
 * chunking.ts — how the production bundle is split, and what is deliberately not in the
 * first-paint set.
 *
 * Built as one file, the bundle was 1.46 MB raw / 468.9 kB gzipped, and all of it was
 * first paint: every panel, every syntax grammar and the dependency-graph renderer had
 * to be downloaded before a review could paint, and any change to the app invalidated
 * the whole thing in the browser cache. `npm run report:bundle` prints the same number
 * split by chunk (kB here and in vite's output mean 1000 bytes).
 * The groups below buy two different things, and it is worth being precise about which
 * is which:
 *
 *   • `graph` is the only name kept out of the first paint. It is reachable solely from
 *     the dependency-graph panel, which `App.tsx` imports lazily, so anyone who never
 *     opens that tab never downloads d3, preact or the canvas renderer — 65.6 kB
 *     gzipped, measured by `npm run report:bundle`, which also gates a budget so the
 *     split cannot be undone quietly.
 *   • `react`, `highlight`, `markdown`, `icons` and `vendor` are **not** a size win —
 *     they load on every visit either way. Splitting them pays for itself in cache
 *     stability (editing a component does not re-download react) and in parallel
 *     fetching, and that is the whole claim being made.
 *
 * First-party code gets no group on purpose: rollup then keeps statically reachable app
 * code in the entry chunk and moves only the lazily imported panel (and its exclusive
 * dependencies) into a chunk of its own. Naming app code here would pull the lazy panel
 * back into the entry graph.
 */

/** Vendor subtrees, keyed by installed package directory, checked in this order. */
const VENDOR_GROUPS: Array<[name: string, test: RegExp]> = [
  [
    // The dependency-graph renderer and everything only it uses: kapsule, the d3
    // force/scale/zoom family, the preact build it bundles for its tooltip, tinycolor2
    // and tween.js. No other module in the app imports any of these — that is the
    // invariant `src/lib/chunking.test.ts › the graph chunk stays optional` guards,
    // because one stray static import anywhere else would put this whole subtree back on
    // the critical path while every number in the build output still looked split.
    "graph",
    /\/node_modules\/(?:react-force-graph-\d+d|force-graph|react-kapsule|kapsule|accessor-fn|index-array-by|canvas-color-tracker|float-tooltip|bezier-js|tinycolor2|preact|@tweenjs\/tween\.js|d3-[^/]+)\//,
  ],
  [
    // The highlighter runtime. *Which grammars* ship is decided in
    // `src/lib/highlight.tsx`, not here — that list is a product decision, this file is
    // only about where the bytes land.
    "highlight",
    /\/node_modules\/(?:react-syntax-highlighter|refractor|prismjs|lowlight|fault)\//,
  ],
  [
    // `react-markdown` plus the micromark/mdast/hast universe behind it. Listed before
    // `react` on purpose: `/node_modules\/react\//` would otherwise swallow
    // `react-markdown` and 158 kB of prose renderer would be billed to the react chunk.
    "markdown",
    /\/node_modules\/(?:react-markdown|remark-[^/]+|micromark[^/]*|mdast-[^/]+|hast-[^/]+|unist-[^/]+|unified|bail|trough|vfile[^/]*|zwitch|longest-streak|markdown-table|property-information|space-separated-tokens|comma-separated-tokens|decode-named-character-reference|character-entities[^/]*|html-url-attributes|github-slugger|ccount|is-alphabetical|is-decimal|is-hexadecimal|is-alphanumerical)\//,
  ],
  ["icons", /\/node_modules\/lucide-react\//],
  [
    "react",
    /\/node_modules\/(?:react|react-dom|scheduler|object-assign|prop-types)\//,
  ],
];

/**
 * Chunk for a module id, or `undefined` to leave it where rollup would put it.
 *
 * Ids arrive as posix-style absolute paths (vite normalises Windows ones identically)
 * with the package directory in them, which is stable enough to group on and is what
 * rollup's own examples key off.
 */
export function vendorChunkFor(id: string): string | undefined {
  if (!id.includes("/node_modules/")) return undefined;
  for (const [name, test] of VENDOR_GROUPS) {
    if (test.test(id)) return name;
  }
  // Everything else third-party, in one chunk. Its contents are not meaningfully
  // separable; the point is that it survives an app edit in the cache.
  return "vendor";
}

/**
 * The chunk names this policy can produce. `chunking.test.ts` asserts this list against
 * the groups themselves, so a group added without a representative — or a name changed
 * without the budget being told — fails a test instead of skewing a report.
 */
export const VENDOR_CHUNK_NAMES: string[] = [...VENDOR_GROUPS.map(([name]) => name), "vendor"];
