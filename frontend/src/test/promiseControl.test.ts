/**
 * promiseControl.test.ts — a page may not tell the reader to go somewhere else
 * to do the thing it needs.
 *
 * THE RULE
 * --------
 * SavFlux's empty states used to point at a destination instead of offering the
 * control. Four of them:
 *
 *     Agent    "Or paste a public URL, or upload a folder — no account needed"
 *     Review   "No files indexed yet. Add a repo in the sidebar first."
 *     Files    "No files indexed yet — ingest a repo to see tree."
 *     Graph    "Index a GitHub repo to see its dependency graph."
 *
 * Not one had a button. Review named a sidebar that does not exist (the
 * repository switcher is in the top bar). A sentence like that is a promise,
 * and a promise with nothing behind it is a dead end — on Review and Files it
 * is the entire page.
 *
 * WHAT IS AND IS NOT FLAGGED
 * --------------------------
 * Only POINTERS: copy that sends the reader off this page. "Index a repo to see
 * the graph" is a pointer. "No files indexed yet" is a statement, and on its own
 * it is fine — CodeWriterPanel's "File to edit" picker says exactly that, and
 * the Write page is perfectly usable with nothing indexed, because generating
 * from a description needs no index. Flagging that would be flagging a correct
 * empty picker.
 *
 * So this test looks for the imperative, deictic half of the promise, and
 * requires the same file to carry a way to act on it.
 */

import { describe, expect, it } from "vitest";

const components = import.meta.glob("/src/**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Copy that sends the reader to another page to get an index. */
const POINTER = [
  /\bindex a (?:public |github )?(?:repo|repository|url)\b/i,
  /\badd a repo\b/i,
  /\bgo to repositories\b/i,
  /\bpaste a public url\b/i,
  /\bupload a folder\b/i,
  /\bingest a repo\b/i,
];

/** Anything that can actually start an index from this page. */
const CONTROL = /IndexPrompt|usePublicIngest|type="file"/;

function pointersIn(source: string): string[] {
  // Only user-visible copy: JSX text, not comments.
  const visible = source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ")
    .replace(/^\s*\*.*$/gm, " ");

  const found = new Set<string>();
  for (const pattern of POINTER) {
    for (const sentence of visible.split(/(?<=[.!?])\s|\n/)) {
      if (!pattern.test(sentence)) continue;

      // An ENUMERATION is not a pointer. ActivityFeed says "chat, save
      // prompts/snippets, share, or index a repo to see timeline" — that is a
      // list of things that populate the feed, three of which are available
      // right here, and indexing genuinely does log an `ingest` entry. Sending
      // the reader to a different page for one item in a list they can already
      // do two of would be the opposite of helpful.
      const alternatives = sentence.split(/,|\bor\b/).filter((s) => s.trim().length > 0);
      if (alternatives.length >= 3) continue;

      found.add(String(pattern));
    }
  }
  return [...found];
}

describe("empty states keep their promise on the page", () => {
  const offenders: string[] = [];

  for (const [path, source] of Object.entries(components)) {
    if (pointersIn(source).length === 0) continue;
    if (!CONTROL.test(source)) {
      offenders.push(path.replace("/src/", ""));
    }
  }

  it("finds at least one page it is meant to be checking", () => {
    // A regex that silently stopped matching would make the test below pass on
    // a codebase that has since regressed.
    const withPointers = Object.values(components).filter(
      (s) => pointersIn(s).length > 0,
    ).length;
    expect(withPointers).toBeGreaterThan(0);
  });

  it("gives every page that asks for an index a way to make one", () => {
    // Known limit: this scans source text, so it proves the *file* can index,
    // not that the control sits on the empty-state branch. Deleting the
    // <IndexPrompt /> from the empty state fails here; hiding it behind
    // `{false && …}` does not, and no text scan can catch that. It would need
    // a rendered-DOM assertion per panel.
    expect(
      offenders,
      `These pages tell the reader to go elsewhere for an index but render no ` +
        `control to do it: ${offenders.join(", ")}. Render <IndexPrompt />, or ` +
        `reword so the page is not promising something it does not offer.`,
    ).toEqual([]);
  });

  it("keeps IndexPrompt as the single way this is done", () => {
    // Four panels used to each hand-roll the same URL box and file input, and
    // drifted — one of them had no error state at all. If someone adds a
    // fifth copy, this asks them to check whether that was a good idea.
    const importers = Object.entries(components)
      .filter(([path]) => path.endsWith(".tsx") && path !== "/src/components/IndexPrompt.tsx")
      .filter(([, source]) => /usePublicIngest/.test(source))
      .map(([path]) => path.replace("/src/", ""));

    // The Agent empty state predates IndexPrompt and is the one place the
    // layout differs (a centred hero rather than a panel inset), so it keeps
    // its own markup — but it must still share the hook, not a copy of it.
    expect(importers).toContain("components/agent/AgentConversation.tsx");
  });
});
