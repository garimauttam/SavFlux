# Security and limits

[Home](../README.md) / [Docs](README.md) / Security and limits

Read findings as evidence, not guarantees. A source match verifies a location; it does not prove an AI suggestion's correctness.

## Evidence levels

| Signal | Meaning | Not a claim of |
| --- | --- | --- |
| Deterministic finding | A parser/rule established a specific condition within its analysis scope | Complete program correctness |
| Heuristic finding | A pattern warrants inspection | A confirmed exploit |
| Retrieved trust score | A relevance score for a retrieved chunk | Truth of every generated statement |
| Unrated | No reranker score was produced | Low relevance or a successful check |
| Static fallback | Model work was unavailable; static findings remain | A completed deep model review |

If a measurement cannot be taken, it should be absent or marked unknown, not reported as zero or a pass. Patch verification similarly distinguishes verified, rejected, and unable to verify.

## Accounts, code, and credentials

- Supabase verifies identity; SavFlux does not store account passwords. Private API calls require a validated session.
- Verified accounts have scoped data directories and indexes. Legacy single-owner data is not automatically assigned to the first new account; re-index after sign-in.
- Saved provider keys are server-side with restrictive `0600` file permissions, **not encrypted at rest**. Protect the host, persistent volume, and backups. Keys are not returned in model-status responses or stored in browser configuration.
- Public share URLs are capability links created explicitly by the user; possessing the URL grants access to its shared content.
- New reviews do not reuse cached results. Existing legacy entries are separate from indexed code, operational settings, and explicitly saved items.
- Account creation is not consent to personalization or model training. Future personalization must be separately opt-in with clear retention, revocation, and deletion controls. Hosted providers have their own data policies; consult them before sending proprietary code.

## Costs and network boundaries

Local Ollama inference, local embeddings/reranking, deterministic analysis, and the offline benchmark need no paid model API. This is a statement about provider fees, **not a promise that hardware, hosting, or account services have no cost**. Sign-in and initial downloads still need network access; GitHub and OSV features call external services.

Cloud connections are optional. Free-tier quotas and account billing settings can change; legacy OpenAI/DeepSeek options are metered. SavFlux does not automatically switch a failed free provider to a paid cloud fallback. Follow [Model connections](MODEL_CONNECTIONS.md) for routing, consent, provider policies, and embedding caveats.

## Suggestions versus deterministic autofix

A generated suggestion is a preview that must be inspected and tested. Deterministic Python autofix covers a narrower set of unambiguous rule repairs and keeps an edit only when it parses, clears its target finding, and introduces no equally or more severe finding. SQL/shell injection, hardcoded secrets, and `eval()` are reported rather than guessed into a repair.

## Risk policy gate

With the risk gate enabled, each change SavFlux is asked to push is scored 0–10 — higher is riskier — and the score decides what happens to it. `POST /api/v1/policy/assess` returns the score, the signals that produced it, and the token to approve it; `POST /review/create-pr` runs the same assessment before it pushes anything.

What the score is made of:

| signal | points | fires when |
|---|---|---|
| `deterministic_findings` | 4 per critical, 2 per high (cap 5) | the change **introduces** a finding a rule proves |
| `verification_failed` | 3 | the verifier ran and the patch did not apply |
| `diff_security_flag` | 2 | added lines disable TLS, weaken a hash, hardcode a secret |
| `sensitive_path` | 2 | the change touches auth, crypto, CI, env, or lockfiles |
| `dependency_surface` | 2 | it adds or upgrades a dependency |
| `not_verified` | 2 | nobody has checked it yet — proposed, not proved |
| `unparsable_change` | 2 | the result is not valid syntax |
| blast radius | 1–3 | ≥1 / ≥3 / ≥10 files depend on what changed |
| `change_breadth` | 1 | more than 10 files or 400 lines |
| `stale_index` | 1 | the index predates the repo's head |

Findings are counted as a **delta**: a patch on a file that already had a hardcoded credential does not get charged for it again, and the pre-existing finding is reported in the signal's detail instead. On a change that adds an auth module with a hardcoded salt, a disabled-TLS call, and an `md5` digest, the score lands at 7 — high.

The gate has three answers, and one of them is not the score's to make:

- **allowed** — below the risk threshold; explicit exact-diff confirmation is still required before publishing.
- **approval required** — the response carries an `approval_token` bound to the change
  digest *and* the assessment digest, so any re-score invalidates it, plus a written
  reason of at least eight characters. Both go back on the retry.
- **blocked** — the score reaches the configured block threshold; publishing is refused.

Separately: **a patch the verifier proves does not apply is never pushed**, even with a perfect token. That is an integrity rule, not a risk judgement — SavFlux declines to push a change that does not do what it says it does, and hands back the `gh` command.

The verifier is a real `git apply` in a throwaway repo, and it answers in three states rather than two: applied (`verified: true`), rejected (`false` — the integrity rule above), or *cannot tell* (`null` — no git on the machine, or no current content to patch against). Where the patch does apply, the resulting files are parsed for the risk score — the same evidence standard the review itself is held to, rather than pattern-matching the diff text.

Every decision is recorded in a ledger (`GET /api/v1/policy/ledger`) holding digests, scores, signals and reasons — never the diff — so "why did this go out?" has an answer as well as "why was it refused?".

## Known limits

- Retrieval benchmarks measure retrieved evidence, not generated-answer faithfulness or universal model quality. The offline hashing embedder is a plumbing check, not a quality substitute for MiniLM.
- The benchmark's 44-query corpus targets this repository; changing backend source changes the corpus. See [Benchmarks](BENCHMARKS.md).
- Dense windowing is bounded; very long chunks may have unranked tails. The harness reports the limit rather than treating it as complete coverage.
- Deterministic autofix is Python-only. Other supported languages have analysis, not equivalent automatic repairs.
- Indexed/reconstructed source is not necessarily the current disk file. Re-index to refresh; an unchanged-file ingest may skip embedding when both content and pipeline match. Deleting the **review cache does not repair or delete the code index**.
- Static analysis and model review can miss defects. Neither passing checks nor approving a proposal makes it production-safe without your own review and tests.
- No implicit remote push: comparing, generating a suggestion, or marking a comment reviewed does not publish a change.

**Continue:** [Configuration](CONFIGURATION.md) · [GitHub workflow](GITHUB_WORKFLOW.md) · [Model privacy](MODEL_CONNECTIONS.md)
