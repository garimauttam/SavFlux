# Continuous code review workspace

[Home](../README.md) / [Docs](README.md) / Continuous code review workspace

Indexed repository reviews now have a folder/file navigation tree and one continuous scrolling source view. Source files load near the viewport (or on tree selection), not all at once. Short files stay intact; longer files can expand hidden context.

## Using the review

1. Run a **new indexed repository review**. This is an indexed snapshot, not a diff
   of your local working tree. Re-index first if the source has changed.
2. Scroll through the files, or click a file in the folder tree. The selected tree
   entry follows the file you are reading. Search and the comments-only filter
   narrow the displayed files without changing the run's totals.
3. Source is syntax highlighted in the selected light/dark theme. A reported
   range is highlighted only after the fetched source hash matches the review.
   Complexity findings now carry the whole function range, not just its first line.
4. Read the inline comment, then open **Suggested fix**. Existing concrete edits
   show a colored diff immediately. Advice-only findings offer **Generate code fix**;
   this explicitly invokes your configured review model and may incur provider charges.
5. Switch between **Unified** and **Before / after**, or use the expand icon for a
   larger comparison. **Copy replacement** copies only the new code, preserving
   indentation. Paste it into your editor at the indicated range, inspect it, and
   run your tests. SavFlux does not apply or execute this proposal.
6. Escape closes the expanded viewer and returns keyboard focus. Cancel or collapse
   stops an in-progress generation request; leaving the workspace also aborts it.
   **Mark reviewed** is in-memory discussion state, not a saved edit.

Hash/range verification proves the location, not that a proposed edit is correct. When the source no longer matches, comments stay separate and code-fix generation is disabled. Re-index and review again. An unavailable model produces an explicit error, not a fabricated edit; static recommendations remain readable.

## Updating a local checkout

Follow [the shared update instructions](GETTING_STARTED.md#update-an-existing-checkout): keep local work safe, pull your chosen branch, run `npm ci` inside `frontend/`, and restart both services. Re-index changed source and run a fresh review.

Individual fix generation is disabled while this workspace's repository review is running. Finish or stop the batch first. For large refactors and model timeouts, see [model diagnostics](MODEL_CONNECTIONS.md#fix-timeouts-during-a-repository-review).

## Historical validation

These results describe the original review-workspace delivery, not the latest checkout. Run [the current validation commands](DEVELOPMENT.md#tests-and-builds) for current results.

- `cd frontend && npm test`: 268 tests passed.
- `cd backend && .venv/bin/pytest -q`: 1,133 tests passed (external services mocked).
- `cd frontend && npm run build && npm run report:bundle:gate`: passed;
  316.7 kB gzip first paint against the existing 320 kB budget.
- Chromium fixture check: 288-file tree, lazy source loading, distant-file navigation,
  multiline syntax/22px line alignment, dark/light palettes, narrow layout, and
  expanded before/after comparison. Fixture source/proposals were mocked; this was
  not a live-provider correctness evaluation.
- The repository's offline RAG baseline was regenerated because backend Python files
  are part of its corpus. Retrieval code and thresholds were not changed. The fused
  file hit rate remains 63.64%; the full measured metrics are in the baseline JSON.

Tests cover exact-source proposal validation, stale-source rejection, authentication, provider cancellation/timeout, deletion suggestions, clipboard behavior and failure handling. Existing build circular-chunk and AnyIO deprecation warnings remain.

**Continue:** [Documentation index](README.md) · [Troubleshooting](TROUBLESHOOTING.md)
