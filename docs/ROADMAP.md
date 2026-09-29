# Roadmap

[Home](../README.md) / [Docs](README.md) / Roadmap

This is a planning reference, not a release guarantee. Current setup and behavior live in the task guides. The longer [strategy document](STRATEGY.md) records an earlier baseline; some recommendations have since shipped.

## Recently shipped

- Multiple explicit free-tier provider connections, task routes, and local Ollama fallback.
- Code-first review with source-matched suggestions, no review-result reuse, and bounded/cancellable fix requests.
- Clear Profile navigation, a top-positioned sidebar toggle, and consistent monospace code typography.
- Public read-only branch comparison without a separate GitHub connection; scoped indexed-file comparison with explicit previews and error states.

## Next priorities

- [ ] **Answer-quality eval on the free path** — the *deterministic* half shipped:
      every finished answer is checked against the evidence it was actually given,
      and a citation pointing outside that evidence is reported under the answer
      (`backend/app/services/grounding_check.py`, streamed as `__GROUNDING__`).
      What is still missing is the judgement half — whether a cited line *supports*
      the claim made about it, and a benchmark of that across models. That needs a
      reader or a second model, and is recorded per answer (Task 2) rather than
      guessed at, because a cheap model grading its own citation is worse than
      no check at all.
- [ ] **Cache the models in CI** so the reranked leg (the product default) and a real
      embedder get benchmarked instead of skipped
- [ ] **Pin the CI benchmark corpus** — adding a test file still moves every measured
      number, which is why the baseline has to be re-recorded whenever `backend/`
      changes. A frozen fixture corpus (instead of this repo's own source) would stop
      that; the tests added here now make the staleness loud instead of silent
- [ ] **VS Code extension** — thin client over `POST /chat/stream`
- [ ] **Watcher-driven re-index** — delta ingest landed (a file re-embeds only when
      its bytes changed or the pipeline that embedded it did, so a re-ingest of an
      unchanged repo embeds nothing); `watcher_service.poll_once()` reports upstream
      changes but does not yet trigger one
- [ ] **Symbol graph** for jump-to-definition
- [ ] **Autofix for JS/TS** — currently Python only
- [ ] **Cross-session memory** — history compacts to 6 turns today
- [ ] **Eval dashboard** charting `GET /metrics`
- [ ] **GitHub OAuth** onboarding instead of a manual PAT

**Continue:** [Development](DEVELOPMENT.md) · [Historical strategy](STRATEGY.md)
