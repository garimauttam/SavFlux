import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  CodeHighlight,
  HIGHLIGHTABLE_LANGUAGES,
  resolveLanguage,
} from "./highlight";

/**
 * Tests for the app's only syntax highlighter.
 *
 * `src/lib/highlight.tsx` exists because the default Prism build cost 44% of the
 * bundle, so the set of grammars is now closed — which turns a styling detail into a
 * behaviour other code depends on. Two things are pinned here: that a fence is resolved
 * the way a repository indexer writes one (aliases, trailing attributes, wrong case),
 * and that a language with no grammar degrades to readable text instead of throwing or
 * swallowing the code. The second is the failure mode the closed list creates, so it is
 * the one worth a test.
 */

describe("resolveLanguage", () => {
  it("accepts the spellings a review actually emits", () => {
    expect(resolveLanguage("python")).toBe("python");
    expect(resolveLanguage("py")).toBe("python");
    expect(resolveLanguage("ts")).toBe("typescript");
    expect(resolveLanguage("TSX")).toBe("typescript");
    expect(resolveLanguage("jsx")).toBe("javascript");
    expect(resolveLanguage("sh")).toBe("bash");
    expect(resolveLanguage("shell-session")).toBe("bash");
    expect(resolveLanguage("dockerfile")).toBe("docker");
    expect(resolveLanguage("html")).toBe("markup");
    expect(resolveLanguage("xml")).toBe("markup");
    expect(resolveLanguage("md")).toBe("markdown");
  });

  it("reads a fence info string, not just a bare name", () => {
    // ```python title=auth.py and ```ts hl_lines=3 both come out of models and out of
    // react-markdown's className; the grammar is the first token and the rest is noise.
    expect(resolveLanguage("python title=auth.py")).toBe("python");
    expect(resolveLanguage("ts,hl_lines=2-3")).toBe("typescript");
    expect(resolveLanguage("  YAML  ")).toBe("yaml");
    expect(resolveLanguage("json:")).toBe("json");
  });

  it("resolves to nothing rather than to a guess", () => {
    for (const raw of ["fortran", "cobol", "text", "", "   ", null, undefined]) {
      expect(resolveLanguage(raw), String(raw)).toBeNull();
    }
    // `text` is in the list on purpose: a bare ``` fence for prose is common in review
    // output, and colouring it with some C-like grammar would be worse than none.
  });

  it("keeps every alias pointing at a grammar that exists", () => {
    // The list is both the grammars and the aliases into them, and a dangling alias is
    // invisible everywhere but here: the UI shows plain text either way and the bundle
    // report cannot tell a live grammar from a dead name.
    for (const name of HIGHLIGHTABLE_LANGUAGES) {
      const resolved = resolveLanguage(name);
      expect(resolved, name).not.toBeNull();
      expect(HIGHLIGHTABLE_LANGUAGES, `${name} -> ${resolved}`).toContain(resolved as string);
    }
    expect(HIGHLIGHTABLE_LANGUAGES.length).toBeGreaterThan(20);
  });
});

describe("CodeHighlight", () => {
  it("highlights a language it knows", () => {
    const code = ["def verify(token):", "    return token == SECRET", ""].join("\n");
    const { container } = render(<CodeHighlight language="python">{code}</CodeHighlight>);
    // `token` is refractor's class for a grammar span. Asserting the class rather than
    // colours keeps this about "a grammar ran"; if highlighting silently stops working,
    // every fence would still look like a code block and no one would notice.
    expect(container.querySelectorAll(".token").length).toBeGreaterThan(0);
    expect(container.textContent).toContain("def verify(token):");
  });

  it("shows a language it does not know, uncoloured but intact", () => {
    const code = ["module Main where", "  main = putStrLn \"hi\"", ""].join("\n");
    const { container } = render(<CodeHighlight language="haskell-not-registered">{code}</CodeHighlight>);
    expect(container.querySelectorAll(".token").length).toBe(0);
    expect(container.querySelector("code")?.textContent).toBe(code);
  });

  it("does not lose indentation or trailing blank lines in either path", () => {
    const code = "  indented:\n    nested: 1\n";
    for (const language of ["yaml", "fortran"]) {
      const { container, unmount } = render(<CodeHighlight language={language}>{code}</CodeHighlight>);
      expect(container.textContent, language).toContain("  indented:");
      expect(container.textContent, language).toContain("    nested: 1");
      unmount();
    }
  });

  it("carries the call site's box styling through the fallback too", () => {
    // The fence chrome (border radius, padding overrides) is decided by the panels, and
    // an unlisted language must not silently land in a differently sized box.
    const { container } = render(
      <CodeHighlight language="fortran" className="rounded-lg" customStyle={{ margin: 0, fontSize: "11px" }}>
        x
      </CodeHighlight>,
    );
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.className).toContain("rounded-lg");
    expect(pre?.getAttribute("style")).toContain("margin: 0px");
  });

  it("renders a text fence as plain text without a grammar", () => {
    // react-markdown gives an info string to *every* fenced block, including the bare
    // ``` fences used for diffs-of-prose in review output.
    render(<CodeHighlight language={null}>plain as the day</CodeHighlight>);
    expect(screen.getByText("plain as the day")).toBeInTheDocument();
  });
});
