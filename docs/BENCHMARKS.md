# Benchmarks

[Home](../README.md) / [Docs](README.md) / Benchmarks

Measurements belong with their corpus and method, not in a permanently green badge. Use the committed [offline baseline](../benchmarks/rag_offline_baseline.json) and the commands below to reproduce a run.

## Security detection

The labeled security corpus contains 17 Python examples (9 vulnerable, 8 safe). The earlier regex comparison measured F1 0.67; AST/dataflow analysis measured 1.00 on that small corpus. These are corpus results, not a claim that all vulnerabilities are detected.

From the repository root, with backend dependencies installed:

```bash
python3 scripts/bench_security.py
# Historical comparison requires this commit in your local Git history:
python3 scripts/bench_security.py --compare 2b049e9
```

The security gate expects F1 ≥ 0.95. Each new rule needs safe negative cases as well as examples it should flag.

## Retrieval regression gate

```bash
python eval_rag.py --quiet --embedder offline --no-rerank \
  --json-out eval_results.json \
  --gate benchmarks/rag_offline_baseline.json
```

This evaluates 44 queries against the repository's backend source. `offline` uses deterministic hashing, not a semantic embedding model; reranking is disabled. It validates retrieval plumbing and regressions, **not production retrieval quality or generated-answer faithfulness**.

| Metric | What it measures |
| --- | --- |
| File Hit@K | Whether an expected file appears within the first K results |
| Span Hit@K | Whether a retrieved unit itself contains the expected symbol |
| MRR | Rank of the first relevant result |
| Symbol recall | Expected symbols represented in the retrieved evidence |
| Per-leg metrics | BM25, dense, fused, and optionally reranked behavior |
| Stage timing | Retrieval, fusion, reranking; harness scoring is reported separately |

The committed baseline at this documentation refresh (2026-09-29, implementation `c29ac48`) records fused file Hit@5 **63.64%**, span Hit@5 **52.27%**, MRR **0.438**, and symbol recall **65.85%**. The recorded corpus is 177 source files, 2,257 chunks, and 3,345 windows. Consult the JSON for full metrics, hashes, and timing; future corpus changes can move these values.

The gate checks per-metric floors and comparability, not just a permissive single hit-rate threshold. A changed corpus is reported; incompatible scoring definitions are not silently compared. A missing measurement stays absent rather than becoming zero.

## Refresh a baseline deliberately

Backend Python files, including tests, are part of the corpus. Record the baseline **after** all such edits, inspect the metric changes, and commit it with the code. Do not weaken thresholds to make a regression pass.

```bash
python eval_rag.py --embedder offline --no-rerank \
  --save-baseline benchmarks/rag_offline_baseline.json
```

The tests check corpus hashes and counts, including reproducibility across absolute checkout paths. For stable before/after experiments, use the benchmark's `--corpus-dir` option to pin inputs.

## Real-model measurements

For a local semantic model, use the CLI's `--embedding-model` option with cached weights and inspect `--help` for the complete configuration. The model benchmark refuses paid embedding APIs; do not substitute offline hashing results for real-model quality. No general generated-answer-quality score is published here.

Indexing skips embedding only when a file's content **and** embedding/chunking pipeline identity match the stored index. That optimization is not cached review reuse: new reviews run fresh and can make new model calls. Historical cached-review speedups are not current behavior.

## Browser bundle budget

```bash
(cd frontend && npm run build && npm run report:bundle:gate)
```

The gate reads Vite's manifest and applies a **320 kB gzip** budget to the initial JS/CSS graph. Fonts and all deferred assets are not included in that headline. The graph and model-connections surfaces can load on demand; shared syntax highlighting uses a bounded grammar registry. Unsupported code fences remain plain monospace.

Use `npm run report:bundle` inside `frontend/` for the current breakdown. Vendor chunk splitting improves cache stability; it does not itself remove bytes. Timing measurements depend on hardware, load, network, and model availability, so old test-duration or model-throughput numbers are not promises.

**Continue:** [Development](DEVELOPMENT.md) · [Architecture](ARCHITECTURE.md)
