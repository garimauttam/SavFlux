/**
 * highlight.tsx — the app's one syntax highlighter, with the grammars it can meet.
 *
 * Every code fence in a review, a chat answer or a generated file used to go through
 * `react-syntax-highlighter`'s default Prism build, which imports *all* of refractor's
 * language definitions: 209 kB of the 469 kB gzipped bundle, 44% of everything the
 * browser had to download before a review could paint — in a product that reviews
 * source files, not ABAQUS input decks or Coq proofs. The light build plus the list
 * below is the same highlighting for the languages this product emits.
 *
 * So this is deliberately a *closed* list, and the honest consequence is stated in the
 * one place that could hide it: a fence in a language that is not here renders as plain
 * monospace text — readable, selectable, copyable, just uncoloured. That is the
 * tradeoff: 300 grammars in the default build, 40 here, so that a handful of exotic
 * fences stay uncoloured instead of everyone waiting on 209 kB. Adding a language is one
 * import and one entry in `GRAMMARS`.
 *
 * The fallback is not incidental. `react-syntax-highlighter` does survive an unlisted
 * language — its ast builder is wrapped in a try/catch that hands back the plain code —
 * but that is a library implementation detail, not a contract, and the same file
 * short-circuits only for the literal name `text`. Deciding in `resolveLanguage` puts
 * the behaviour in code we own, keeps the box identical, and gives the case a test.
 */

import type { CSSProperties } from "react";
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";

import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import c from "react-syntax-highlighter/dist/esm/languages/prism/c";
import clike from "react-syntax-highlighter/dist/esm/languages/prism/clike";
import cmake from "react-syntax-highlighter/dist/esm/languages/prism/cmake";
import cpp from "react-syntax-highlighter/dist/esm/languages/prism/cpp";
import csharp from "react-syntax-highlighter/dist/esm/languages/prism/csharp";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import docker from "react-syntax-highlighter/dist/esm/languages/prism/docker";
import elixir from "react-syntax-highlighter/dist/esm/languages/prism/elixir";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import graphql from "react-syntax-highlighter/dist/esm/languages/prism/graphql";
import haskell from "react-syntax-highlighter/dist/esm/languages/prism/haskell";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import ini from "react-syntax-highlighter/dist/esm/languages/prism/ini";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import kotlin from "react-syntax-highlighter/dist/esm/languages/prism/kotlin";
import lua from "react-syntax-highlighter/dist/esm/languages/prism/lua";
import makefile from "react-syntax-highlighter/dist/esm/languages/prism/makefile";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import objectivec from "react-syntax-highlighter/dist/esm/languages/prism/objectivec";
import perl from "react-syntax-highlighter/dist/esm/languages/prism/perl";
import php from "react-syntax-highlighter/dist/esm/languages/prism/php";
import powershell from "react-syntax-highlighter/dist/esm/languages/prism/powershell";
import properties from "react-syntax-highlighter/dist/esm/languages/prism/properties";
import protobuf from "react-syntax-highlighter/dist/esm/languages/prism/protobuf";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import r from "react-syntax-highlighter/dist/esm/languages/prism/r";
import ruby from "react-syntax-highlighter/dist/esm/languages/prism/ruby";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import scala from "react-syntax-highlighter/dist/esm/languages/prism/scala";
import scss from "react-syntax-highlighter/dist/esm/languages/prism/scss";
import solidity from "react-syntax-highlighter/dist/esm/languages/prism/solidity";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import swift from "react-syntax-highlighter/dist/esm/languages/prism/swift";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";

/**
 * Grammars by the name a markdown fence is likely to use.
 *
 * Both spellings a code indexer produces are here — the extension (`ts`) and the
 * language name (`typescript`) — because reviews echo whichever the file's metadata
 * carried, and a fence written by a model uses the name.
 */
const GRAMMARS: Record<string, unknown> = {
  bash,
  c,
  clike,
  cmake,
  cpp,
  csharp,
  css,
  diff,
  docker,
  elixir,
  go,
  graphql,
  haskell,
  ini,
  java,
  javascript,
  json,
  kotlin,
  lua,
  makefile,
  markdown,
  markup,
  objectivec,
  perl,
  php,
  powershell,
  properties,
  protobuf,
  python,
  r,
  ruby,
  rust,
  scala,
  scss,
  solidity,
  sql,
  swift,
  toml,
  typescript,
  yaml,
};

/** Fence-info aliases → a key of `GRAMMARS`. */
const ALIASES: Record<string, string> = {
  "c++": "cpp",
  cc: "cpp",
  cfg: "toml",
  console: "bash",
  cs: "csharp",
  dockerfile: "docker",
  h: "c",
  hpp: "cpp",
  html: "markup",
  js: "javascript",
  jsx: "javascript",
  jupyter: "python",
  md: "markdown",
  mdx: "markdown",
  objc: "objectivec",
  ps1: "powershell",
  py: "python",
  repl: "python",
  rb: "ruby",
  rs: "rust",
  sh: "bash",
  shell: "bash",
  "shell-session": "bash",
  svelte: "markup",
  ts: "typescript",
  tsx: "typescript",
  vue: "markup",
  xml: "markup",
  zsh: "bash",
};

// Registration is a module side effect on purpose: `PrismLight` exposes
// `registerLanguage`, and the languages must be in place before the first fence
// renders. Importing this file is what configures the highlighter, so no view can
// reach an unconfigured one.
for (const [name, grammar] of Object.entries(GRAMMARS)) {
  SyntaxHighlighter.registerLanguage(name, grammar as never);
}
for (const [alias, target] of Object.entries(ALIASES)) {
  SyntaxHighlighter.registerLanguage(alias, GRAMMARS[target] as never);
}

const REGISTERED = new Set([...Object.keys(GRAMMARS), ...Object.keys(ALIASES)]);

/**
 * The fence info string (`"python title=auth.py"`, `ts`, `TSX`) → a registered name,
 * or `null` when there is no grammar for it.
 *
 * Returning `null` rather than guessing keeps the failure quiet and visible: plain
 * text in the right box, not the wrong colour scheme and not an error boundary.
 */
export function resolveLanguage(info: string | undefined | null): string | null {
  const token = String(info ?? "")
    .trim()
    .split(/[\s,=:]+/)[0]
    .toLowerCase();
  if (!token) return null;
  const name = ALIASES[token] ?? token;
  return REGISTERED.has(name) ? name : null;
}

/** Names this build can highlight, for anything that wants to advertise or test it. */
export const HIGHLIGHTABLE_LANGUAGES = [...REGISTERED].sort();

export interface CodeHighlightProps {
  /** The fence's info string, raw — it goes through `resolveLanguage`. */
  language?: string | null;
  children: string;
  className?: string;
  /**
   * Passed to the highlighter unchanged. Deliberately no default here: the four call
   * sites already differ (a fenced block in a review wants a different box than the
   * one inside a generated-file card), and this file's job is which grammars load,
   * not how the app looks.
   */
  customStyle?: CSSProperties;
}

/**
 * A code block, highlighted when there is a grammar for it and monospace when not.
 *
 * `PreTag="div"` is not decoration: these blocks live inside `<p>`-producing markdown,
 * and a `<pre>` inside a `<p>` is invalid HTML that React fixes silently and jsdom
 * does not — the shape every call site already used is kept so no layout moves.
 */
export function CodeHighlight({ language, children, className, customStyle }: CodeHighlightProps) {
  const resolved = resolveLanguage(language);

  if (!resolved) {
    return (
      <pre className={className} style={customStyle}>
        <code style={{ display: "block", padding: "0.5em 1em", whiteSpace: "pre-wrap" }}>
          {children}
        </code>
      </pre>
    );
  }

  return (
    <SyntaxHighlighter
      language={resolved}
      style={vscDarkPlus}
      PreTag="div"
      className={className}
      customStyle={customStyle}
    >
      {children}
    </SyntaxHighlighter>
  );
}
