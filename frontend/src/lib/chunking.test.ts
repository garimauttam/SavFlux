import { describe, expect, it } from "vitest";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { VENDOR_CHUNK_NAMES, vendorChunkFor } from "./chunking";

/**
 * Tests for the bundling policy in `chunking.ts`.
 *
 * What a policy is worth depends on an invariant the bundler cannot check for itself:
 * the `graph` chunk is optional only while nothing outside the lazily imported
 * dependency-graph panel imports it. That is a fact about `src/`, not about rollup, so
 * it is asserted here — reading the real files rather than a fixture, because a fixture
 * cannot regress.
 */

// vitest's jsdom environment rewrites import.meta.url to an http URL, so the project root
// is read from the working directory — `frontend/` for every way this suite is run (`npm
// test`, `npx vitest`). The guard below is what makes a wrong cwd a clear failure instead
// of a scan that vacuously passes because it found no files to read.
const FRONTEND = process.cwd();
const SRC = join(FRONTEND, "src");
if (!existsSync(join(SRC, "lib", "chunking.ts"))) {
  throw new Error(`no src/lib/chunking.ts under ${FRONTEND} - run vitest from frontend/`);
}

const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((entry) => {
    const abs = join(dir, entry);
    return statSync(abs).isDirectory() ? walk(abs) : abs.endsWith(".tsx") || abs.endsWith(".ts") ? [abs] : [];
  });

// Only shipped modules count: `index.html` imports no test file, so a unit test is free
// to import the panel statically, and `chunking.ts` itself is the policy under test — it
// names every package the scan below looks for, in strings rather than imports.
const isTest = /\.test\.tsx?$/;
const sources = walk(SRC).filter(
  (file) => !file.endsWith(`${SRC}/lib/chunking.ts`) && !isTest.test(file),
);
const rel = (file: string) => relative(SRC, file).split("\\").join("/");
const read = (file: string) => readFileSync(file, "utf8");
const find = (needle: string) => sources.filter((file) => read(file).includes(needle));

/** `vendor` is the fallback bucket, so it gets no representative of its own here. */
const REPRESENTATIVES: Record<string, string> = {
  graph: "/repo/node_modules/d3-force/build/d3-force.js",
  highlight: "/repo/node_modules/react-syntax-highlighter/dist/esm/prism-light.js",
  markdown: "/repo/node_modules/react-markdown/index.js",
  icons: "/repo/node_modules/lucide-react/dist/esm/lucide-react.js",
  react: "/repo/node_modules/react-dom/client.js",
};

describe("vendorChunkFor", () => {
  it("leaves first-party code where rollup would put it", () => {
    // A lazily imported panel can only leave the entry graph if its own code is not
    // pinned into a named chunk: `src/App.tsx` must be grouped by reachability, not here.
    for (const id of [
      "/repo/src/App.tsx",
      "/repo/src/components/GraphPanel.tsx",
      "/repo/src/lib/highlight.tsx",
      // A directory named after a vendor group must not be mistaken for that vendor.
      "/repo/src/vendor/utils.js",
      "/repo/src/styles/markdown.css",
    ]) {
      expect(vendorChunkFor(id), id).toBeUndefined();
    }
  });

  it("groups each vendor subtree under the name the budget talks about", () => {
    for (const [name, id] of Object.entries(REPRESENTATIVES)) {
      expect(vendorChunkFor(id), id).toBe(name);
    }
    expect(vendorChunkFor("/repo/node_modules/escape-goat/index.js")).toBe("vendor");
  });

  it("names exactly the chunks the groups can produce", () => {
    expect([...VENDOR_CHUNK_NAMES].sort()).toEqual(
      [...Object.values(REPRESENTATIVES).map((id) => vendorChunkFor(id)), "vendor"].sort(),
    );
  });

  it("does not let the react prefix swallow a package that merely starts with react", () => {
    // `react-markdown` in the `react` chunk would bill 158 kB of prose renderer to a
    // 142 kB runtime and, worse, hide it from the markdown budget line.
    for (const id of [
      "/repo/node_modules/react-markdown/index.js",
      "/repo/node_modules/react-syntax-highlighter/dist/esm/index.js",
      "/repo/node_modules/react-force-graph-2d/src/Graph.js",
      "/repo/node_modules/react-kapsule/dist/esm/kapsule.js",
      "/repo/node_modules/react-is/cjs/react-is.production.min.js",
    ]) {
      expect(vendorChunkFor(id), id).not.toBe("react");
    }
  });

  it("keeps every package the graph panel drags in out of the other groups", () => {
    for (const id of [
      "/repo/node_modules/force-graph/dist/force-graph.mjs",
      "/repo/node_modules/d3-scale/src/index.js",
      "/repo/node_modules/d3-color/src/index.js",
      "/repo/node_modules/@tweenjs/tween.js/dist/tween.esm.js",
      "/repo/node_modules/tinycolor2/esm/tinycolor.js",
      "/repo/node_modules/preact/dist/preact.module.js",
      "/repo/node_modules/bezier-js/src/bezier.js",
      "/repo/node_modules/canvas-color-tracker/dist/canvas-color-tracker.mjs",
      "/repo/node_modules/float-tooltip/dist/float-tooltip.mjs",
    ]) {
      expect(vendorChunkFor(id), id).toBe("graph");
    }
  });
});

describe("the graph chunk stays optional", () => {
  const graphPackages = /(d3-[a-z0-9-]+|force-graph|react-force-graph-\d+d|kapsule|accessor-fn|index-array-by|canvas-color-tracker|float-tooltip|bezier-js|tinycolor2|preact|@tweenjs\/tween\.js)/;
  const panelFiles = find("GraphPanel");

  it("is imported by no module but the lazy panel", () => {
    // One static import of a graph package anywhere else in `src` and the whole subtree
    // is back on the critical path, with a build log that still looks split. Specifiers
    // only, so a comment naming d3 cannot fail a build.
    const SPECIFIER = /(?:\bfrom|\bimport)\s*\(?\s*["']([^"']+)["']/g;
    const offenders = sources
      .filter((file) => rel(file) !== "components/GraphPanel.tsx")
      .filter((file) => [...read(file).matchAll(SPECIFIER)].some((m) => graphPackages.test(m[1])));
    expect(offenders.map(rel)).toEqual([]);
  });

  it("is reached through a dynamic import, and only one", () => {
    // Only two shipped files may name it: the lazy declaration and the panel itself.
    expect(panelFiles.map(rel).sort()).toEqual(["App.tsx", "components/GraphPanel.tsx"]);

    const app = read(join(SRC, "App.tsx"));
    expect(app).toMatch(/lazy\(\s*\(\)\s*=>\s*\n?\s*import\(["']\.\/components\/GraphPanel["']\)/);
    expect(app).not.toMatch(/^import\s+\{?\s*GraphPanel/m);
    // Suspense is not decoration: without a fallback the first paint is blank instead of
    // showing the loader, which is the only thing the delay costs the user.
    expect(app).toMatch(/<Suspense[^>]*fallback=/);
    expect(app).toMatch(/<Suspense[\s\S]*?<GraphPanel/);
  });
});

describe("the config wires the policy up", () => {
  const config = readFileSync(join(FRONTEND, "vite.config.ts"), "utf8");

  it("uses the policy object, not a copy of it", () => {
    // Restating the patterns in vite.config.ts is how the tested policy and the shipped
    // bundle drift apart.
    expect(config).toMatch(/import\s*\{\s*vendorChunkFor\s*\}\s*from\s*["']\.\/src\/lib\/chunking["']/);
    expect(config).toMatch(/manualChunks:\s*vendorChunkFor/);
  });

  it("emits the manifest the bundle report measures", () => {
    // `manifest: false` and the report silently reports the *previous* build.
    expect(config).toMatch(/manifest:\s*true/);
  });
});
