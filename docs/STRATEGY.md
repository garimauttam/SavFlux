# SavFlux — Strategy & Next Steps

> **Status:** decision document. Nothing here is implemented yet.
> **Baseline:** `main` @ `667fa92` (merge of PR #7).
> **Purpose:** choose the next work so SavFlux is *outstanding*, not merely featureful.

---

## 1. The recommendation in one paragraph

**Do not build the VS Code extension first.** Order it third, behind (a) cross-language
parity via tree-sitter and (b) SARIF output. The reason is not taste — it is a specific,
verifiable mismatch in the current code:

> SavFlux's most valuable behaviour is Python-only, and its intended next audience is
> overwhelmingly JavaScript/TypeScript.

Concretely, from the code:

- `ast_chunker.py` docstring, line 33: *"Python only — other languages use
  RecursiveCharacterTextSplitter as before."* Only `chunk_python_file()` produces
  AST-boundary chunks. Everything else is character-count splitting, which is exactly the
  problem that module exists to solve (it splits functions in half, mixes unrelated
  helpers into one chunk).
- `ingestion_service.py:63-64`: `.ts` and `.tsx` both map to `Language.JS` —
  *"TS shares JS splitter rules."* TypeScript is parsed with JavaScript rules.
- `autofix.py:44` `FIXABLE_RULES` = 6 rules, all `PY-SEC-*` / `PY-EXC-*`.
- `fix_service.py:184` hard-gates rewriting on `PYTHON_EXTENSIONS`.

Meanwhile, per the Stack Overflow 2025 Developer Survey (49k+ respondents), JavaScript is
used by 66% of developers and TypeScript by ~44%; TypeScript leads GitHub in monthly
contributors (~2.64M, Aug 2025). VS Code is the default editor for that population.

**So shipping a VS Code extension today ships a thin client over a Python-only core to a
JS/TS-heavy audience.** They will install it, point it at a React repo, and get
character-split chunks, weaker citations, and no fixes. That is a first-impression bug you
only get to make once.

Fix the core first. The extension is then a 1-week wrapper over something genuinely good.

---

## 2. Where the project actually stands

The feature list that circulated (10 "P0/P1" items) was largely stale. Verified against
`main`:

| Claimed gap | Actual state |
|---|---|
| Line-precise citations missing | **Shipped** — `openFile.ts` carries `startLine`/`endLine`/`lineRanges`; `test_citations.py` asserts span precision, chunk merging, relevance ordering. Spans are attached for non-Python files too (`ingestion_service.py:214`). |
| `create_pr` agent tool missing | **Shipped** — `agent_tools.py` `TOOL_SPECS` + `MUTATING_TOOLS`; digest-bound confirm gate. |
| Apply-fix not wired to UI | **Shipped** — `ReviewPanel.tsx:1208` → `ApplyFixPanel` → `CreatePRDialog`. |
| PR webhook auto-review missing | **Shipped** — `.github/workflows/savflux-pr-review.yml`. |
| CVE audit missing | **Shipped** — `security_service.py` queries OSV across `requirements.txt`, `package.json`, `go.mod`. |
| Policy gates not enforced | **Shipped** — `risk_policy.py`, `RISK_GATE_ENABLED`, approval tokens bound to the assessment. |
| `eval_rag.py` not in CI | **Wrong** — `ci.yml` has a `rag-eval` job that fails the build below a hit-rate floor. |
| No latency dashboard | **Half-wrong** — `AnalyticsPanel.tsx` already plots avg/p95 + sparkline. Missing is *RAG quality* (hit@k, faithfulness), not latency. |

Genuinely pending: cross-language parity, SARIF, symbol graph, incremental indexing
(watcher only notifies — `watcher_service.py:63`), cross-session memory, eval dashboard,
GitHub OAuth.

> **Housekeeping:** the linked issue #6 ("Wire autofix, build_patch and create_pr into the
> agent and UI") describes work that is **already on main** — `agent_tools.py`'s own
> docstring says it was created because those three endpoints "sat unreachable." It should
> be closed as completed, not built. Note it lives in the fork `garimauttam/SavFlux`;
> `origin` has issues disabled, so the roadmap cannot live there.

---

## 3. Positioning: what to be, and what not to be

**Do not compete on "ask your codebase questions."** That is commoditized — Cursor,
Copilot, Claude Code and a dozen free tools all do it, with frontier models and better
distribution. A $0 local-model clone loses that fight on the axis it is fought on.

**Compete on verifiability.** SavFlux's defensible assets are already built and are rare:

1. **Line-precise citations + trust scoring** — every claim carries the exact evidence span.
2. **Re-analysis-verified fixes** — a proposed fix is re-parsed and re-analysed; a fix that
   regresses is rejected. Competitors *assert* a fix; SavFlux *proves* one.
3. **Deterministic-first economics** — parser-settled files skip the model entirely;
   `REVIEW_CACHE_ENABLED` means unchanged repos make **zero** model calls.
4. **Offline/private by default** — Ollama + local MiniLM + local cross-encoder + Chroma on
   disk. No vendor sees the code.

The line to own: **"the reviewer that can't lie to you."** Every competitor at the $0 tier
asks you to trust it. SavFlux shows its work. That is a *judgment* position, and judgment is
what senior interviews and differentiated products are made of.

---

## 4. The architecture principle that makes small models behave like pro models

This is the answer to *"use basic open-source models and make them work like pro models,"*
and it is also the answer to latency and cost. It is one idea:

> **Shift work from probabilistic to deterministic. Never ask a model to find what a parser
> can prove.**

SavFlux already gestures at this (README §"The trick that makes small models punch above
their weight"). Make it the architecture instead of a trick:

| Tier | Who does the work | Cost | Latency | Can it hallucinate? |
|---|---|---|---|---|
| **T0** Deterministic | tree-sitter queries, taint/dataflow, AST rules | $0 | ~ms | No |
| **T1** Ranking | local cross-encoder rerank of *evidence* | $0 | ~50ms CPU | No |
| **T2** Narration | model called **only** on what T0 can't settle, with T0's facts injected as constraints | local/free | seconds | Constrained to verified facts |
| **T3** Verification | re-parse + re-analyse the proposed fix; reject regressions | $0 | ~ms | No |

Precision comes from T0. Fluency comes from T2. Verification comes from T3 — which is
**already built** and is the crown jewel of the repo.

The strategic implication: **every finding moved from T2 to T0 is simultaneously faster,
cheaper, and more accurate.** That is the only lever in this project that improves all three
at once, and it is why the plan below leads with deterministic parsing.

---

## 5. The plan

### P0-a — Cross-language parity (tree-sitter) · ~3–4 days · highest leverage

One MIT-licensed dependency, `tree-sitter-language-pack` (prebuilt wheels, 100+ grammars,
`pip install tree-sitter-language-pack`). **Retires three roadmap items at once:**

1. **AST-boundary chunking for JS/TS/Go/Java/Rust/Ruby** — a tree-sitter query for
   `function_declaration` / `method_definition` / `arrow_function` / `class_declaration`
   replaces `RecursiveCharacterTextSplitter`. This is the single biggest retrieval-quality
   win available, and it fixes `.ts`/`.tsx` being parsed as JS.
2. **Symbol graph** — the same trees give symbol extraction and reference resolution, which
   is the roadmap's jump-to-definition item. `dep_graph.py` is file-level today
   (`_extract_imports`); symbol-level is a query away once trees exist.
3. **JS/TS autofix** — deterministic rules for the JS/TS equivalents of the existing
   `PY-SEC-*` set, satisfying the repo's own three gates (re-parse, re-analyse, mutation-check).

**Why first:** it is the difference between "works on my Python portfolio repo" and "works on
the repos people actually use."

### P0-b — SARIF output · ~half a day · highest credibility per hour

No SARIF exists anywhere in the repo (verified). Emit SARIF 2.1.0 from a `POST /review/sarif`
endpoint plus a CLI entry point, and SavFlux becomes consumable by **GitHub code scanning**,
any CI, and any IDE that reads SARIF — including, later, the VS Code extension, for free.
It is also the artifact that makes "we're a real static analyser" legible to a reviewer in
one glance.

### P1 — VS Code extension · ~1 week, *after* the above

Only now does it make sense. Do it in the phased order — the ordering matters more than the
features:

- **Phase 0 — extract the stream protocol first.** `useChat.ts` hand-parses a bespoke wire
  protocol (`__SOURCES__`, `__STATUS__`, `__DIAGNOSTIC__`) with ~60 lines of hardened
  chunk-boundary logic; its comments document a real, previously-fixed bug where a split
  end-marker bled raw marker text into the answer. A new client **will** reintroduce that bug
  class. Extract the parser to a framework-free `frontend/src/lib/streamProtocol.ts` with a
  Vitest suite driven by adversarial byte-split fixtures; the extension imports that one
  module. One parser, one test suite, no drift.
- **Phase 1** — `vscode/` in-monorepo so CI covers it; **esbuild** bundling; typed client
  generated from `/openapi.json` (`openapi-typescript`) with a CI drift check so the
  extension cannot diverge from backend schemas; API key in `context.secrets` (never
  `settings.json`); auto-detect repo from the workspace git remote for zero-config setup.
- **Phase 2 — the wedge is diagnostics, not a chat box.** VS Code **Chat Participant API**
  (`@savflux` + `/review`, `/fix`, `/explain`, `/pr`) *and* `vscode.lm.registerTool` so
  SavFlux's retrieval is callable from Copilot itself. On-save runs the **deterministic-only**
  path into a `DiagnosticCollection`. Combined with the parse-settled skip and the content-hash
  cache, **unchanged files make zero model calls** — the $0 guarantee holds *in the editor*,
  and review feels like a linter, not a web app.
- **Phase 3** — `revealRange` + decoration on the exact cited span; CodeLens "Apply verified
  fix" → `/review/autofix` → `vscode.diff` preview → confirm → `/review/build-patch` →
  `/review/create-pr` **with `confirm_digest`**. The extension must never auto-push; that
  would break the safety invariant the backend deliberately built.
- **Phase 4** — `vsce package`, publish to **Open VSX** (free) + Marketplace, attach the
  `.vsix` to releases in CI.

**Additions worth making:** version-negotiate on `/openapi.json` so a stale extension fails
loudly instead of mysteriously; verify whether `analyze_file` has a content-hash LRU — if not,
adding one makes on-save squiggles ~1ms; surface `__DIAGNOSTIC__` markers as "degraded mode"
notices, because an honest "BM25 unavailable" is on-brand and a silent wrong answer is not.

### P2 — Proof and flywheel

- **Incremental indexing** — `watcher_service.poll_once()` only emits a notification; nothing
  re-ingests. Wire it to the existing delta path (`test_delta_ingest.py`).
- **Eval dashboard** — serve `eval_rag.py` results so hit@k and faithfulness are visible, not
  just CI-gated. Chart *retrieval quality*, not latency (latency is already covered).
- **GitHub OAuth** — a growth lever that matters only once the extension exists.
- **Cross-session memory** — lowest priority; see §6.

---

## 6. What NOT to build yet

Judgment is mostly about refusal. Each of these is in the circulating list; none should be next:

- **"AI chat with your codebase" marketing.** Commoditized; invites comparison on the axis you
  lose.
- **Multi-modal ingest (PDF/design docs).** High cost, no moat, off-pitch.
- **GitHub OAuth onboarding.** Premature until there is a surface worth onboarding into.
- **Cross-session memory.** The 6-turn truncation is arguably a *feature*: predictable cost,
  no stale-context bugs. Adding memory adds cost, latency, and a whole new failure mode
  (confidently recalling a refactor that no longer exists) — against a product whose entire
  pitch is that it does not assert things it cannot prove.
- **VS Code extension before P0-a.** Per §1.
- **Chasing Cursor/Copilot feature-for-feature.** You cannot win on model quality or
  distribution. Do not fight there.

---

## 7. How to prove it, not claim it

README already does the right thing — *"Security detection — reproducible right now"* with a
stated F1 0.67 → 1.00. Formalize that instinct into `benchmarks/`:

- Pinned public corpus + pinned dataset; a published table regenerated in CI, not hand-typed.
- Report **precision / recall / F1**, **$ cost**, and **p50/p95 latency** against baselines.
- Publish the two metrics nobody else in this space reports, because they are the moat:
  1. **% of findings resolved with zero model calls** (deterministic-first coverage)
  2. **citation → evidence exact-line correctness %**
- The repo's own contribution rules already demand negative cases and mutation-checking. Keep
  enforcing them; that discipline is itself a differentiator.

A competitor cannot match those two metrics without abandoning their unit economics. That is
what a moat looks like on paper.

---

## 8. Progress

### P0-a, part 1 — cross-language AST chunking: **done** (JS/TS)

Shipped in two steps, both with tests and mutation checks:

- **Step 1** — `tree_sitter_langs.py`: path → grammar resolution, thread-local parsers,
  graceful degradation, `capabilities()` for diagnostics. 28 tests.
- **Step 2** — `code_chunker.py`: AST-boundary chunking through one entry point,
  `chunk_code_file`, now wired into `_load_and_split`. 33 tests.

Definition of done, as written above, met:

| Requirement | Where it is proved |
|---|---|
| Both chunkers interchangeable behind one entry point | `test_python_goes_through_the_ast_chunker`, `test_metadata_matches_the_python_contract` |
| `.ts`/`.tsx` no longer parsed with JS rules | `test_typescript_and_javascript_families_map_to_distinct_grammars`, `test_tsx_component_chunks` |
| Functions not split across chunks | `test_a_long_function_is_not_cut_in_half_by_character_counting` |
| Citation contract did not weaken | `test_line_spans_are_attached_for_the_citation_contract`, `test_every_span_round_trips_to_its_symbol` |
| Negative test per repo rule | `test_data_and_doc_formats_have_no_grammar`, `test_a_constant_is_not_a_definition`, `test_parse_errors_drop_the_broken_definition_but_keep_the_rest` |
| Mutation check | 7 mutations, all caught (see the Step 2 commit) |

Measured on this repo's own frontend (45 React/TSX files, a real corpus):

- 353 symbol chunks, **0** files unparsed, **0** out-of-bounds spans
- **0** orphaned code lines — every line lands in some chunk
- 3.7 ms per file, pure CPU, no model call

Two invariants hold here that the Python path does not: a line is never in zero chunks,
and a definition overlapping a parse error is refused rather than cited.

### P0-a, part 2 — JS/TS analysis and autofix: **done**

Shipped in two steps:

- **Step 3** — `code_analysis/js_analyzer.py`: structural detection for the patterns the
  regex pass provably cannot reach, merged into `analyze_generic`. 38 tests.
- **Step 4** — `code_analysis/autofix_js.py`: tree-sitter byte-surgical fixers behind the
  same three gates, wired into `fix_service` alongside Python. 40 tests.

**A real bug, found by running the analyzer rather than reading it.** Every rule matches
against `line.code`, which has string *contents* blanked — correct for most patterns, but
it made two shipped security patterns unreachable:

| Pattern | Stripped form | Consequence |
|---|---|---|
| `createHash('md5')` | `createHash('   ')` | MD5/SHA-1 in Node never reported |
| `NODE_TLS_REJECT_UNAUTHORIZED = '0'` | `... = ' '` | TLS checking disabled via the documented escape hatch never reported |

Fixed by parsing, not by loosening the regexes — matching raw lines would fire on
`// don't use createHash('md5')` and on `expect(src).not.toContain(...)`, the latter being
in this repo's own test suite. A third gap surfaced while testing: `{ 'rejectUnauthorized':
false }` puts a quote between the name and the colon, so the regex misses it too. That
spelling disables TLS just as effectively, and is now detected.

**The fixable set is deliberately three transformations**, all provable from the tree:

```
rejectUnauthorized: false                → rejectUnauthorized: true
NODE_TLS_REJECT_UNAUTHORIZED = '0' | 0   → NODE_TLS_REJECT_UNAUTHORIZED = '1'
createHash('md5' | 'sha1')               → createHash('sha256')
```

`eval(x)`, `innerHTML` with a variable, a template literal in SQL, a hardcoded secret: all
left alone, because each needs a decision about intent that only the author can make. An
autofix that guesses gets merged.

Both fixers now return the same `FixResult`, so the re-analysis, score delta and patch
generation in `fix_service` are identical for either language and cannot diverge.

Verified end to end: `POST /review/autofix` on a TS file goes 1 finding → 0, score 8 → 10,
and emits a one-token diff that `git apply` accepts.

### P0-c — the retrieval stack: the model that decides what the LLM is allowed to read

The chat model is a code specialist (`qwen2.5-coder`). The two models that choose
*which code it sees* were not: `all-MiniLM-L6-v2` is a 2021 general-sentence model
(~56.3 MTEB, the lowest tier), and the cross-encoder is trained on web passages, not
code. Retrieval quality is a hard ceiling on answer quality — the right 7B code model
handed the wrong three chunks still answers wrong.

**Defect 1, measured: the default embedder cannot read most of a chunk.**

`all-MiniLM-L6-v2` reads **256 tokens and silently discards the rest.** The chunker's
ceiling is `MAX_CHUNK_CHARS = 3000`. Measured over this repo's own corpus:

| Corpus | Chunks | Median | Truncated (256 tok) | Content never embedded |
|---|---|---|---|---|
| `frontend/src` TS/TSX | 353 | 803 chars | 47–55% | 58–67% |
| `backend/app` Python | 665 | 793 chars | 46–57% | 41–53% |

Two honest caveats. The measurement is a **proxy** — character counts against assumed
chars/token ratios (2.5 / 3.0 / 3.5), because the real BERT WordPiece tokenizer needs
weights this environment cannot fetch. The conclusion is stable across all three
ratios, and the largest chunks exceed 256 tokens under *any* plausible ratio. Also,
only the **dense** leg truncates: BM25 indexes `page_content` in full
(`hybrid_retriever.py:67`), so exact-identifier lookups still work. What degrades is
semantic retrieval — the half dense search exists for.

**Defect 2, fixed: the indexing path was throttled on purpose-but-not-deliberately.**

`batch_size` was hardcoded to `1` and `device` to `"cpu"`. Both are now settings, with
`batch_size` defaulting to 32 and `device` to `auto` (CUDA when torch sees a GPU, CPU
otherwise). **This changes speed, not vectors** — each row is encoded independently and
padding is attention-masked — so it needs no re-index. `EMBEDDING_DEVICE=cpu` restores
the old behaviour exactly.

Also fixed: `get_provider_name()` said "local MiniLM embeddings" unconditionally. That
string is what `/health` prints and what the benchmark is stamped with, so it was
about to start describing a model that was not running.

**The swap itself: enabled, not yet measured.**

`EMBEDDING_MODEL` now takes any HuggingFace id. The recommended target is
`jinaai/jina-embeddings-v2-base-code` — **Apache-2.0**, 768d, **8K context** (it
swallows the chunker's 3000-char ceiling whole), trained on `github-code` plus 150M
code Q&A pairs, 30 languages, and it led 9/15 CodeNetSearch benchmarks. `bge-m3` (MIT)
is the multilingual alternative; `Qwen3-Embedding` has **conflicting licence reports**
and should be checked at the model card before use.

Two traps worth writing down:

- **Jina v3 is CC-BY-NC**, not free for commercial use. The newer model is the wrong
  one. Same for `NV-Embed-v2`.
- **Swapping the reranker silently breaks trust scores.** `citation_service.py`
  calibrates `HIGH_TRUST_SCORE = 2.0` / `MEDIUM_TRUST_SCORE = -3.0` in ms-marco's logit
  space. A different cross-encoder keeps rendering "high confidence" using thresholds
  from a model that is no longer running. That is why the reranker name is now a module
  constant (`reranker.RERANKER_MODEL`), reported in every benchmark run.

**Why it is still not measured HERE:** this sandbox cannot reach `huggingface.co` (nor
`hf-mirror.com`), and no weights are cached, so a real before/after comparison is
impossible in it. Rather than publish inferred numbers, the harness was made to report
its own provenance — `eval_rag.py` now stamps every run with the embedder, device,
batch size, reranker and provider, so two result files can never be confused.

**The harness was fixed first, because it was measuring the wrong pipeline.** It had
three defects pointing the same way:

| # | Defect | Consequence |
|---|---|---|
| 1 | **No dense branch** — it built a `BM25Index` and never loaded an embedder | `EMBEDDING_MODEL` could not move any number it printed |
| 2 | **A different fusion algorithm** — flat `reciprocal_rank_fusion` over BM25 lists, where production uses weighted `two_branch_rrf` | `two_branch_rrf`'s own docstring explains why the flat one is wrong for one dense list plus N lexical ones; the benchmark committed exactly that mistake |
| 3 | **A corpus polluted by the virtualenv** — `rglob("*.py")` matched **19,501** files by descending into `.venv/` | 100x slower than necessary, and the corpus **depended on which packages were installed**, so CI and laptop numbers were not comparable |

Defect 3 is the one that would have wasted the most time. Fixing it took the corpus
to **127 files**, the run from **3m03s to 1.8s**, and moved every metric:

| Leg | polluted corpus | clean corpus |
|---|---|---|
| `bm25_only` | 40.9% | **72.7%** |
| `dense_only` | 9.1% | **36.4%** |
| `fused` | 61.4% | **79.5%** |

The benchmark now reports **per-leg metrics** — `bm25_only`, `dense_only`, `fused`,
`fused_reranked` — and records which embedder drove the dense leg. That decomposition
is what decides whether an embedder swap is worth a re-index: on this query mix dense
adds **+6.8 points** of hit rate over BM25 alone (79.5 vs 72.7) while *lowering* MRR
(0.451 vs 0.487), which is the recognisable RRF signature — fusion buys recall at the
top and pays for it in ordering precision.

```bash
# 1. baseline, production pipeline, configured model
python eval_rag.py --quiet --json-out eval_results.minilm.json

# 2. swap the embedder and re-index (768d ≠ 384d, so old vectors are unusable)
export EMBEDDING_MODEL=jinaai/jina-embeddings-v2-base-code
rm -rf backend/chroma_data/
python eval_rag.py --quiet --json-out eval_results.jina.json

# 3. compare metrics.by_leg — both files now name the embedder that produced them
```

CI runs `--embedder offline --no-rerank`: the dense branch is exercised with a
deterministic hashing embedder (no download, no network), and reranking is skipped
because otherwise each query retries a cross-encoder download with exponential backoff
before giving up, turning a 2-second job into several minutes and corrupting the
latency figure. **Offline numbers are labelled as such in the JSON and in the report**
— they prove the plumbing works and say nothing about retrieval quality.

Then decide the default from the numbers. Flipping it costs every existing user a
re-index, so `tests/test_embedding_config.py` pins the current default and is expected
to fail when it changes — deliberately.

### Still open

- **Symbol graph / jump-to-def.** Grammars are loaded and trees are built, so symbol
  extraction is a query away, but `dep_graph.py` is still file-level.
- **JS/TS detection breadth.** Analysis is still mostly regex; the structural pass covers
  only the string-content patterns. `eval`/`innerHTML` are detected but not fixable.
- **More grammars.** go/java/rust/ruby/c/cpp are registered but their wheels are not
  installed, so those languages still fall back. Adding one is a single `pip install`.

### Then

**P0-b — SARIF output**, then **P1 — the VS Code extension**. See §5.

