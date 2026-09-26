#!/usr/bin/env node
/**
 * bundle-report.mjs — what the browser must download before SavFlux can paint.
 *
 * `vite build` prints every chunk it emitted, and chunk sizes are not the claim anyone
 * cares about. The claim is about the *entry graph*: the file `index.html` loads, plus
 * every chunk it statically imports, transitively. A chunk can be 3 kB and still cost a
 * second round trip; a 60 kB chunk can cost nothing if it is only reachable from a tab
 * the user never opens. So this reads `dist/.vite/manifest.json` (enabled in
 * `vite.config.ts`) and walks `imports` from each entry — never `dynamicImports` — and
 * measures raw and gzipped bytes for exactly that set.
 *
 * Usage, from `frontend/`:
 *   npm run build && node scripts/bundle-report.mjs
 *   node scripts/bundle-report.mjs --entry-budget-kb 320   # exit 1 over budget
 *   node scripts/bundle-report.mjs --json
 *
 * `--entry-budget-kb` compares the first-paint total (JS + CSS, gzipped) and is the
 * reason the number quoted in a changelog entry stays true: a future dependency that
 * drags 200 kB onto the critical path fails a gate instead of shipping quietly.
 */

import { readFileSync, existsSync, statSync } from "node:fs";
import { readdirSync } from "node:fs";
import { gzipSync } from "node:zlib";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const flag = (name) => {
  const i = argv.indexOf(name);
  if (i === -1) return undefined;
  const next = argv[i + 1];
  return next && !next.startsWith("--") ? next : true;
};

const distDir = resolve(typeof flag("--dist") === "string" ? flag("--dist") : join(HERE, "..", "dist"));
const budgetKb = Number(flag("--entry-budget-kb") ?? 0) || 0;
const manifestPath = join(distDir, ".vite", "manifest.json");

if (!existsSync(manifestPath)) {
  console.error(`${manifestPath} not found — run \`npm run build\` first (it needs build.manifest, see vite.config.ts).`);
  process.exit(2);
}

const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));

/**
 * A file the manifest names but `dist/` does not have. Checked up front because the
 * alternative is a report that quietly counts a partial build as a small one: `dist/` is
 * not cleared by every build path, and an under-budget number from missing chunks is the
 * worst possible failure mode for a gate.
 */
const missing = Object.values(manifest)
  .map((chunk) => chunk.file)
  .filter((file) => file && !existsSync(join(distDir, file)));
if (missing.length) {
  console.error(`manifest names ${missing.length} file(s) missing from ${distDir}: ${missing.join(", ")}`);
  console.error("rebuild with `npm run build` (a stale dist/ is not a measurement).");
  process.exit(2);
}

const bytesOf = (rel) => {
  const abs = join(distDir, rel);
  if (!existsSync(abs)) return null;
  const raw = statSync(abs).size;
  return { file: rel, raw, gzip: gzipSync(readFileSync(abs)).length };
};

const asset = (key) => (typeof manifest[key]?.file === "string" ? manifest[key].file : null);

/** Every manifest key reachable from the entries without crossing a dynamic import. */
function entryGraphKeys() {
  const seen = new Set();
  const stack = Object.entries(manifest)
    .filter(([, value]) => value.isEntry)
    .map(([key]) => key);
  while (stack.length) {
    const key = stack.pop();
    if (seen.has(key)) continue;
    seen.add(key);
    for (const next of manifest[key]?.imports ?? []) {
      // `imports` back to an entry (the lazy panel does it) is normal; `imports` only,
      // never `dynamicImports`, or a tab nobody opened would count as first paint.
      if (!seen.has(next)) stack.push(next);
    }
  }
  return seen;
}

const entryKeys = entryGraphKeys();
const jsOf = (keys) =>
  keys
    .filter((key) => (asset(key) ?? "").endsWith(".js"))
    .map((key) => ({ key, name: manifest[key].name ?? key, ...bytesOf(asset(key)) }))
    .filter((c) => c.raw != null);

const cssOf = (keys) => {
  const files = new Set();
  for (const key of keys) for (const rel of manifest[key]?.css ?? []) files.add(rel);
  // Vite records a stylesheet on the chunk that needs it, so a CSS file listed on a
  // deferred chunk is fetched with that chunk and not at first paint; `assets/*.css`
  // files no chunk in the group names are ignored rather than guessed at.
  return [...files].map((rel) => ({ file: rel, ...bytesOf(rel) })).filter((c) => c.raw != null);
};

const allKeys = Object.keys(manifest).filter((key) => asset(key));
const deferredKeys = allKeys.filter((key) => !entryKeys.has(key));

const entryJs = jsOf([...entryKeys]);
const entryCss = cssOf([...entryKeys]);
const deferredJs = jsOf(deferredKeys);
const deferredCss = cssOf(deferredKeys);

const total = (list) => list.reduce((acc, x) => acc + x.raw, 0);
const totalGz = (list) => list.reduce((acc, x) => acc + x.gzip, 0);
// kB as vite and every bundler report it (÷1000), so these numbers can be read against
// `vite build`'s own output and against a changelog without a unit conversion.
const kb = (n) => +(n / 1000).toFixed(1);

const entryGzKb = kb(totalGz(entryJs) + totalGz(entryCss));
const allFiles = existsSync(join(distDir, "assets"))
  ? readdirSync(join(distDir, "assets")).reduce((acc, f) => acc + statSync(join(distDir, "assets", f)).size, 0)
  : 0;

const payload = {
  dist: distDir,
  first_paint: {
    js: entryJs.map((c) => ({ name: c.name, file: c.file, raw_kb: kb(c.raw), gzip_kb: kb(c.gzip) })),
    css: entryCss.map((c) => ({ file: c.file, raw_kb: kb(c.raw), gzip_kb: kb(c.gzip) })),
    gzip_kb: entryGzKb,
    raw_kb: kb(total(entryJs) + total(entryCss)),
  },
  deferred: {
    js: deferredJs.map((c) => ({ name: c.name, file: c.file, raw_kb: kb(c.raw), gzip_kb: kb(c.gzip) })),
    css: deferredCss.map((c) => ({ file: c.file, raw_kb: kb(c.raw), gzip_kb: kb(c.gzip) })),
    gzip_kb: kb(totalGz(deferredJs) + totalGz(deferredCss)),
  },
  dist_total_raw_kb: kb(allFiles),
  budget_kb: budgetKb || null,
  over_budget: budgetKb > 0 && entryGzKb > budgetKb,
};

if (flag("--json") === true) {
  console.log(JSON.stringify(payload, null, 2));
} else {
  // Biggest first: the row that matters is the one at the top of each list.
  const row = (list, note) =>
    [...list]
      .sort((a, b) => b.gzip_kb - a.gzip_kb)
      .forEach((c) =>
        console.log(`  ${String(c.gzip_kb).padStart(7)} kB gz  ${String(c.raw_kb).padStart(8)} kB raw  ${c.name ?? c.file}${note ?? ""}`),
      );
  console.log(`dist: ${distDir}`);
  console.log(`\nFirst paint — ${payload.first_paint.js.length} JS chunk(s), fetched before the app can run:`);
  row(payload.first_paint.js);
  row(payload.first_paint.css, "  (css)");
  console.log(`  ${String(payload.first_paint.gzip_kb).padStart(7)} kB gz  ${String(payload.first_paint.raw_kb).padStart(8)} kB raw  = first paint`);
  console.log(`\nDeferred — only fetched when the tab that needs them is opened:`);
  row(payload.deferred.js);
  row(payload.deferred.css, "  (css)");
  console.log(`  ${String(payload.deferred.gzip_kb).padStart(7)} kB gz            = deferred`);
  console.log(`\ndist/assets total: ${payload.dist_total_raw_kb} kB raw (everything, before gzip)`);
  if (budgetKb > 0) {
    console.log(`\nbudget: first paint ${payload.first_paint.gzip_kb} kB gz vs ${budgetKb} kB → ${payload.over_budget ? "OVER" : "ok"}`);
  }
}

process.exit(payload.over_budget ? 1 : 0);
