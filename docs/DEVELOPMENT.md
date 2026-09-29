# Development and contributing

[Home](../README.md) / [Docs](README.md) / Development and contributing

Complete [Getting started](GETTING_STARTED.md) first, including Python and frontend dependencies. Run the following from the repository root with the backend virtual environment activated.

## Tests and builds

```bash
(cd backend && python -m pytest -q)
(cd frontend && npm test)
(cd frontend && npm run test:setup)
(cd frontend && npm run build && npm run report:bundle:gate)
python3 scripts/bench_security.py
python eval_rag.py --quiet --embedder offline --no-rerank \
  --gate benchmarks/rag_offline_baseline.json
python3 scripts/check_docs.py
python3 -m unittest discover -s scripts -p 'test_check_docs.py'
```

Backend tests mock external calls and set a test provider in `conftest.py` to avoid model downloads; this is not a live-provider certification. An explicit environment override can change that. Frontend tests exercise state, navigation, cancellation, rendering, and API contracts. CI configuration is in [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

Do not treat old static test-count badges as current status. Run the suite or inspect the relevant CI result. The latest comparison delivery was validated with 1,163 backend and 293 frontend tests; those are historical counts, not an assertion that this documentation edit reran them.

## Contribution rules

1. **Include negative cases.** A detector must stay quiet on the safe form of the same construct.
2. **Verify repairs.** Deterministic fixes must parse, remove the target finding, and introduce no equally or more severe findings.
3. **Test the real function.** Copying its implementation into a test only proves the copy works.
4. **Prove the regression test can fail.** Reintroduce the original defect where practical and confirm the test catches it.
5. **Keep side effects explicit.** Do not weaken auth, source matching, cancellation, approval, or provider-cost guards for convenience.
6. **Update measurements honestly.** Record corpus and method; leave unmeasured fields unknown. Refresh the offline baseline after backend Python changes.

For UI work, check both themes, narrow screens, keyboard focus, loading/error states, and stale-response behavior. Label mocked browser fixtures as fixtures rather than evidence of a live GitHub or model integration working.

## Documentation style

- Keep [the main README](../README.md) a landing page. Put detailed procedures in a focused guide and link it from the [docs index](README.md).
- Use one H1, descriptive H2/H3 headings, short paragraphs, tables for choices, and task-oriented links. Keep one blank line between blocks; avoid decorative horizontal rules and forced `<br>` spacing.
- Use relative repository links so documentation works on branches and in local previews. Validate file targets and heading anchors with `scripts/check_docs.py`.
- GitHub controls Markdown fonts and strips arbitrary CSS. Use native typography; limit custom font stacks and color to accessible SVG/Mermaid diagrams. Include textual explanations and alt text.
- The visual palette is violet for application/model work, blue for data, teal for evidence, and amber for approval. Always label nodes; never depend on color alone.
- Preserve code-block indentation and shell continuation backslashes. Avoid copying machine-specific absolute paths or credentials into shared setup instructions.

## Planning

[Roadmap](ROADMAP.md) separates shipped behavior from proposals. [Strategy](STRATEGY.md) is a historical planning document, not a current feature checklist.

**Continue:** [Architecture](ARCHITECTURE.md) · [Benchmarks](BENCHMARKS.md) · [Roadmap](ROADMAP.md)
