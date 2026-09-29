# Free-tier model connections and local fallback

[Home](../README.md) / [Docs](README.md) / Free-tier model connections and local fallback

Researched 2026-09-29. Catalog availability, prices, regions and account quotas can change. Check the provider's own console before sending proprietary source. No provider is promised to be unlimited or suitable for all 288 files in one run.

## Why “model unavailable” appeared

An installed model is not a successful generation test. The screenshot alone cannot identify the machine's exception. Common causes are insufficient memory for loading the weights plus context/KV cache, Ollama not responding, a missing review model, or a generation timeout. The 8–18 GB download sizes shown in the screenshot are **not** total runtime RAM requirements.

There were also application issues:

- The header showed `chat_model`, even when review used `OLLAMA_REVIEW_MODEL`.
  Selecting a 14B chat model did not override a separately configured 33B review model.
  The picker now shows the cloud primary and effective review model; connection settings
  include an explicit **Local review fallback model** selector.
- The pinned `langchain_community.ChatOllama` adapter cannot bind tools. Agentic
  reviews now explicitly downgrade to deterministic tools plus one text-model review
  rather than failing before generation. The same applies if a fallback wrapper
  cannot bind tools. This is a capability downgrade, not a claim of tool support.
- Review exceptions were collapsed into “model call failed.” They now report safe
  categories for authentication, quota, timeout, request/model errors and runtime
  failures. Raw provider bodies, keys and source code are not used as diagnostics.
- Review circuits are scoped to the account/selection; one account's failed key
  must not disable another account's review.

The “unexpected indent” warning in the screenshot is a **separate source-analysis problem**. It does not prove an LLM outage. It may reflect the indexed/reconstructed snapshot; compare it with the real file and re-index. The expired email-link fragment in the URL is also separate: use a fresh sign-in link if authentication fails.

## Provider shortlist and tradeoffs

| Provider | Free-use scope | Decision |
| --- | --- | --- |
| Groq | Free-plan organization/model request and token limits; exact account limits in its console | Direct integration and live catalog. Start with GPT-OSS 20B, low effort, if returned for your account. |
| OpenRouter | Explicit `:free` endpoints, currently 50 requests/day without a credit top-up and 20/minute; availability rotates | Direct integration; filter for zero-priced text endpoints, add gateway price ceilings, and disable gateway provider fallback. Never upgrade to paid automatically. |
| Google Gemini | Eligible models have a free tier; quota is project/model-specific. Billing-enabled projects may charge. Free-tier data can be used to improve products. | Direct OpenAI-compatible integration. Conservative allowlist of documented free Flash/Flash-Lite text models, intersected with the live catalog. |
| Mistral | Free mode for evaluation/prototyping with organization request/token/month caps | Direct integration; live chat-capable catalog. Confirm Free mode and review data-training preferences. |
| Hugging Face Inference Providers | Free users currently receive $0.10 monthly credits, subject to change | Researched, not added as a direct connection: too little budget for routine large repository reviews. |
| GitHub Models | Free rate-limited API access for prototyping; limits vary by model/account and paid opt-in changes billing | Researched, not added in this delivery. GitHub repository authentication is not reused as a model credential. |
| Cerebras | Current docs describe a $5/30-day trial requiring a verified payment method, not a recurring free tier | Not recommended as a permanent free fallback; not integrated here. |
| OpenAI / DeepSeek direct | Metered accounts, not a general recurring free API allowance | Existing advanced provider settings retained, clearly separated from the new free-tier flow. Never chosen automatically. |

Sources:
- Groq limits: https://console.groq.com/docs/rate-limits
- Groq reasoning: https://console.groq.com/docs/reasoning
- OpenRouter free limits: https://openrouter.ai/pricing and https://openrouter.ai/blog/tutorials/how-to-get-the-lowest-cost-llm-inference-on-openrouter/
- OpenRouter reasoning metadata: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Gemini pricing and privacy distinction: https://ai.google.dev/gemini-api/docs/pricing
- Gemini compatibility and effort mappings: https://ai.google.dev/gemini-api/docs/openai
- Mistral free-mode quotas: https://help.mistral.ai/en/articles/698531-why-am-i-hitting-api-rate-limits-and-how-do-i-increase-them
- Hugging Face credits: https://huggingface.co/docs/inference-providers/pricing
- GitHub Models: https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-models
- Cerebras trial: https://inference-docs.cerebras.ai/support/rate-limits

## Step-by-step setup

1. Stop an active run. Pull this Arena branch and restart backend/frontend (see
   `REVIEW_WORKSPACE.md`). Hard-refresh the browser.
2. Open **Profile → Model connections**, or the model menu's **Manage API keys,
   models & routing** button.
3. Choose Groq, OpenRouter, Gemini or Mistral. Open the provider's **Get API key**
   link. Paste the key into the password field **in the app, never into chat**.
4. Click **Load / refresh models**. This contacts that provider's model-list
   endpoint only. It does not generate text or store the pasted key.
5. Choose a model and a supported reasoning effort. Unknown capabilities show
   **Provider default**, not a guessed low/medium/high selector. Gemini 3.5
   Flash-Lite currently uses default effort here pending a verified mapping.
6. Read and explicitly accept server-storage/cloud-context consent. For providers
   other than OpenRouter, confirm that the account/project uses the free tier,
   not paid billing. **SavFlux cannot enforce the billing plan on a remote account.**
7. **Save connection**. Repeat for additional providers. One key and preferred
   model/effort are stored per provider; saving does not activate it or erase other keys.
8. Under **Task routing**, choose:
   - **One model for all tasks**, with a default connected provider or Ollama; or
   - **Assign providers by task**: chat/Q&A, code generation (Write), code review
     and fixes, repository reasoning (review summaries).
   Example: Groq for chat, Mistral for writing, Gemini for reviews, OpenRouter free
   for summaries. This is explicit routing, not a claim that one model is universally
   best. Each provider's saved model/effort is shared by the tasks assigned to it.
9. **Save routing**. This disables any previously configured parallel MoA summary
   set to avoid surprise fan-out. You can deliberately configure that separate local
   feature again later. Only subsequent model calls use changed settings.
10. Choose a smaller **Local review fallback model** if necessary, then use
    **Test local review model** or **Test saved model**. Tests send only “Reply with
    OK,” consume a small quota, never repository code, and do not use fallback.
11. Start with a 1–3 file review and low/default reasoning. Increase scope gradually.
    Inspect the last-model-call diagnostic to see whether cloud or Ollama actually
    completed. A green installed-model indicator alone is not an inference check.

## Cost, fallback, and data guarantees

- The new connection flow never offers paid OpenRouter endpoints. Calls include
  zero prompt/completion price ceilings and no gateway fallback. Other direct
  providers require a confirmed free account; their `/models` APIs cannot verify
  your billing plan or remaining allowance.
- There is **no automatic cloud-to-cloud routing, key rotation, credit purchase,
  or paid upgrade**. A selected cloud model can fall back only to the configured
  local Ollama model. 401/402/403/429 failures cool down that account/provider for
  60 seconds to avoid retry storms. A 35-second hosted network timeout limits stalled calls; the review's outer
  deadline still bounds the full operation. Cold local models can
  still exceed it; failures remain explicit.
- Once a stream has emitted content/tool deltas, it is not spliced with another
  model's answer. Cancellation does not start fallback. An empty/tiny-budget test
  result is not described as a successful full review.
- Reasoning can consume output budget and leave less visible answer. Higher effort
  is not guaranteed to improve a task, and can hit free-tier limits sooner.
- Keys live in the existing per-user server data directory, in a separate atomic
  `0600` credentials file. APIs return configured/not-configured, never the secret.
  **SavFlux does not encrypt these files at rest.** Server administrators/storage
  backups may access them; secure the host and storage. Deleting a connection
  removes its app-stored key and moves affected routes to Ollama. Environment keys
  are explicitly unaffected.
- Consent is required to save new cloud connections and send future task context.
  Keys are not placed in browser storage. No review cache, prompt retention or
  personalization store was introduced. Recent safe runtime status is in memory,
  bounded and per account, expiring from display after an hour.
- Embeddings are pinned to the pre-routing provider to avoid invalidating the
  existing index. If your old index uses OpenAI embeddings, that route remains
  metered; switching chat models does not make embeddings free. Moving that index
  to local embeddings requires explicitly selecting Ollama in the legacy provider
  settings and re-indexing, before enabling the new cloud task routing again.
- Only fixed known HTTPS provider hosts are supported. Arbitrary custom endpoints
  are intentionally not accepted (SSRF/key-exfiltration risk).

## Local troubleshooting without sharing secrets

```bash
ollama list
ollama ps
curl http://localhost:11434/api/tags
ollama run qwen2.5-coder:7b 'Reply with OK'
```

If the smaller model is not installed, explicitly pull it first: `ollama pull qwen2.5-coder:7b`. Select it for the local review fallback in the app. Check the actual review model, not just the chat model. Check available RAM and Ollama logs if generation returns a server error. Do not post API keys or full request headers when sharing a sanitized error report.

## Validation for this change

- 272 frontend tests and 1,150 backend tests passed.
- Production build and existing bundle budget passed: 317.3 kB gzip first paint
  against 320 kB. The provider settings UI is loaded on demand.
- Browser fixture: consent-gated key save, model/effort selection, task routing,
  dark/light themes and a 390px viewport without horizontal overflow.
- Mock transport verified that the pinned OpenAI SDK places reasoning and
  OpenRouter price controls correctly in the actual outgoing JSON body.
- Tests cover tenant key isolation, safe diagnostics, auth, live-catalog validation,
  unsupported reasoning rejection, quota cooldown/local fallback, cancellation,
  no midstream model splicing, and the old Ollama tool-binding failure.
- No live cloud keys or local Ollama model were available in the sandbox; provider
  calls and browser catalogs were mocked. Account-specific limits and generation
  quality must be checked using the explicit in-app tests.
- Offline RAG corpus baseline regenerated for the new Python modules/tests;
  retrieval implementation and acceptance thresholds were not changed.

## Fix timeouts during a repository review

A finding can appear before a multi-file review finishes. Previously, its **Generate code fix** action could start a second review-model request during that batch. The review workspace now disables generation until the batch ends or is stopped. Starting a new batch also cancels an in-flight suggestion in that workspace; existing suggestions remain viewable. This is a UI coordination measure, not a server-wide queue: requests in other tabs, chat, and other clients can still contend for the same model.

The suggestion deadline remains 90 seconds, with provider cancellation on expiry/disconnect. A timeout makes no file changes and does not automatically retry. Wait for completion or press **Stop**, then try one fix. If it still fails, select a smaller installed **Review** model (the header's chat model is not necessarily the review model), or explicitly configure a free-tier review provider. Start with 1–3 files rather than the full repository. Large findings now show a warning: a hundreds-of-lines refactor can exceed time/output limits even without competing requests.

Suggestion prompts no longer ask the model to echo the entire original range. The backend constructs `original` from the hash-checked indexed snapshot and validates that the returned range covers the finding and stays within supplied context. If a model still returns `original`, a mismatched echo is rejected. The client rechecks the returned original/hash. This reduces output work; it is not a syntax or behavioral-correctness guarantee. Nothing is auto-applied.

Code rows, suggestion diffs and the source viewer use self-hosted Geist Mono (13px / 22px), without a third-party font request. Review prose explicitly resets preformatted whitespace to prevent clipped instructions. This is an Arena-like monospace treatment, not a verified copy of Arena's exact typography.

Validation: regression tests cover active-batch blocking, post-stop enabling, cancellation/late results, compact proposals and actionable timeout errors. Browser checks use a fixture, not the user's repository or live model: desktop light/dark and 390px mobile typography, font loading, wrapping and generation blocking. Local Ollama timing/resource use still requires a test on the user's machine; these changes do not establish the runtime cause of the screenshot.

**Continue:** [Documentation index](README.md) · [Troubleshooting](TROUBLESHOOTING.md)
