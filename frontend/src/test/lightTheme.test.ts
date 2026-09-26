/**
 * lightTheme.test.ts — keeps the light-theme layer honest.
 *
 * index.css remaps the app's fixed dark utilities when <html> lacks `.dark`,
 * including `.text-white` → near-black. That is right for body text on a white
 * surface and wrong for the buttons that put `text-white` on a saturated
 * background: on bg-purple-600 the remap drops contrast from 5.38:1 to 3.32:1,
 * under WCAG AA, so those shades are excluded from the remap by an explicit
 * list. A list like that silently rots — a new accent button ships dark-on-dark
 * in light mode and nobody notices until someone switches theme.
 *
 * So the list is derived from the components rather than remembered beside
 * them: this test scans every .tsx for `text-white` sitting on an opaque accent
 * background and fails if the shade is missing from the CSS, or is listed there
 * but used nowhere.
 *
 * Components come from `import.meta.glob(..., '?raw')`, which keeps them inside
 * the vite module graph. index.css cannot: vitest's `css: false` turns any CSS
 * import into an empty module (checked — `?raw` and `?inline` both return ""),
 * so the stylesheet is read from disk instead.
 *
 * Not covered, deliberately: `bg-accent-500/20` (a translucent accent over a
 * remapped gray surface, where dark text IS correct — CommandPalette's selected
 * row) and `hover:bg-accent-*` with no opaque base (none exist today).
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const components = import.meta.glob("/src/**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

// index.css is read from disk. `import.meta.url` is not a file: URL under the
// jsdom environment, so the path comes from the vitest root (frontend/), with a
// loud failure rather than a silently empty stylesheet if that ever changes.
const INDEX_CSS = join(process.cwd(), "src", "index.css");

/** className values: plain strings and simple template literals. */
const CLASSNAME = /className=(?:"([^"]*)"|\{`([^`]*)`\})/g;
/** Neutral surfaces are remapped to slate by index.css; accents are not. */
const NEUTRAL = /^(gray|slate|zinc|neutral|stone)$/;
/** A standalone `bg-<hue>-<shade>` — not a `hover:` variant, not `bg-x-500/20`. */
const ACCENT_BG = /(?:^|[\s"'`])bg-([a-z]+)-(\d{3})(?![\w/-])/g;
const WHITE_TEXT = /(?:^|[\s"'`])text-white(?![\w-])/;

function accentsWithWhiteText(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const [file, source] of Object.entries(components)) {
    for (const [, plain, template] of source.matchAll(CLASSNAME)) {
      const classes = plain ?? template ?? "";
      if (!WHITE_TEXT.test(classes)) continue;
      for (const [, hue, shade] of classes.matchAll(ACCENT_BG)) {
        if (NEUTRAL.test(hue)) continue;
        const name = `bg-${hue}-${shade}`;
        const seen = found.get(name) ?? [];
        if (!seen.includes(file)) seen.push(file);
        found.set(name, seen);
      }
    }
  }
  return found;
}

function guardedShades(): Set<string> {
  if (!existsSync(INDEX_CSS)) {
    throw new Error(`index.css not found at ${INDEX_CSS} — run the suite from frontend/`);
  }
  const css = readFileSync(INDEX_CSS, "utf8");
  const shades = new Set<string>();
  for (const [, group] of css.matchAll(/:is\(([^)]*)\)/g)) {
    for (const [, name] of group.matchAll(/\.(bg-[a-z]+-\d{3})/g)) shades.add(name);
  }
  return shades;
}

describe("light theme — accent buttons keep a readable label", () => {
  const used = accentsWithWhiteText();
  const guarded = guardedShades();

  it("reads the sources it is meant to check", () => {
    // Guards against the glob silently matching nothing, which would make every
    // other assertion here vacuously true.
    expect(Object.keys(components).length).toBeGreaterThan(20);
    expect(guarded.size).toBeGreaterThan(0);
    expect(used.size).toBeGreaterThan(10);
    expect(used.has("bg-purple-600")).toBe(true);
  });

  it("excludes every accent used behind text-white from the .text-white remap", () => {
    const missing = [...used.keys()].filter((shade) => !guarded.has(shade)).sort();
    expect(
      missing,
      `text-white sits on ${missing.join(", ")} but index.css does not exempt ` +
        `those shades, so light mode renders their labels near-black on a saturated ` +
        `background. Add them to the :is(...) list.`,
    ).toEqual([]);
  });

  it("lists no shade that nothing uses", () => {
    const dead = [...guarded].filter((shade) => !used.has(shade)).sort();
    expect(
      dead,
      `index.css exempts ${dead.join(", ")} but no component puts text-white on it. ` +
        `Stale entries hide the ones that matter — remove them.`,
    ).toEqual([]);
  });
});
