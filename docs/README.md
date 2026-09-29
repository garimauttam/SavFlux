# SavFlux documentation

[Home](../README.md) / Documentation

Choose a guide for the task in front of you. Installation, daily use, and implementation details live on separate pages instead of one long README.

## Start and operate

| Guide | Covers |
| --- | --- |
| [Getting started](GETTING_STARTED.md) | Prerequisites, Supabase, local models, two-terminal setup, updates |
| [Configuration](CONFIGURATION.md) | Environment files, review throughput, policy, Docker and deployment caveats |
| [Model connections](MODEL_CONNECTIONS.md) | Multiple keys, hosted free tiers, reasoning effort, task routing, local fallback |
| [Troubleshooting](TROUBLESHOOTING.md) | Missing packages/fonts, sign-in, ports, model timeouts, disabled comparisons |

## Use the workspace

| Guide | Covers |
| --- | --- |
| [Workspace](WORKSPACE.md) | Navigation, first-session workflow, keyboard shortcuts, Profile |
| [Review workspace](REVIEW_WORKSPACE.md) | Inline findings, source checks, suggested edits, cancellation |
| [Changes workspace](CHANGES_WORKSPACE.md) | Branches versus Agent worktrees versus indexed files |
| [GitHub workflow](GITHUB_WORKFLOW.md) | Access, branch selection, indexing, comparison, explicit PR approval |

## Understand and contribute

| Guide | Covers |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | System diagram, retrieval, analysis, autofix, tools and streaming |
| [Security and limits](SECURITY.md) | Evidence levels, data storage, costs, consent, approval gates |
| [Benchmarks](BENCHMARKS.md) | Reproducible measurements, corpus limits, regression gates, bundle budget |
| [Development](DEVELOPMENT.md) | Tests, contribution rules, documentation checks and visual conventions |
| [Roadmap](ROADMAP.md) | Recently shipped work and next priorities |
| [Historical strategy](STRATEGY.md) | Earlier planning baseline; not a current implementation checklist |

**Suggested reading paths**

- **New user:** Getting started → Workspace → Review workspace.
- **Repository maintainer:** GitHub workflow → Changes workspace → Security and limits.
- **Contributor:** Architecture → Development → Benchmarks.

GitHub controls body fonts and Markdown layout. The diagrams use labeled, high-contrast blue, violet, teal, and amber nodes; every diagram also has a text explanation.
