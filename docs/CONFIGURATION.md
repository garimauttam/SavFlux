# Configuration

[Home](../README.md) / [Docs](README.md) / Configuration

Use [Getting started](GETTING_STARTED.md) for the minimum working setup. This page covers changes after that first run.

## Configuration sources

| Location | Purpose |
| --- | --- |
| [`backend/.env`](../backend/.env.example) | Backend runtime, auth, provider, review, and policy options; link opens the template |
| [`frontend/.env.local`](../frontend/.env.example) | Public browser/build configuration; link opens the template |
| [`configs.json`](../configs.json) | Shipped defaults; environment settings override these |
| **Profile → Model connections** | Explicit saved provider keys, model choices, task routes, local fallback |

Restart the relevant process after changing environment files. Frontend `VITE_*` values are included in the browser build: never put private provider keys or Supabase service-role keys there.

## Local-first models

```dotenv
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_CHAT_MODEL=qwen2.5-coder:7b
OLLAMA_REVIEW_MODEL=
```

An empty review-model setting uses the chat model. A separately selected Review model can differ from the header's chat selection. Installation/reachability does not prove that inference will complete; context length and concurrent requests also consume memory.

For multiple provider keys, supported reasoning levels, free-tier eligibility, and task-specific routing, use [Model connections](MODEL_CONNECTIONS.md). Automatic fallback is local Ollama, not another paid cloud provider. Provider quotas, billing-plan status, and source-code data policies still apply.

Changing chat models is different from changing the embedding pipeline. Embedding-provider/model changes require re-indexing; the index records pipeline identity to avoid silently mixing incompatible vectors. Existing legacy embedding configuration may remain pinned when adding cloud routes; follow the migration guidance in the model guide.

## Review throughput

These are example settings, not a guarantee of speed:

```dotenv
REVIEW_MODE=fast
REVIEW_LLM_BUDGET=12
REVIEW_CONCURRENCY=2
REVIEW_MAX_FILES_PER_REQUEST=2000
```

`fast` runs deterministic analysis and plans bounded model work. Eligible files receive individual calls up to the configured budget; other model work can be batched. `agentic` uses the deeper tool loop where the configured adapter supports it. Larger models and greater concurrency are not automatically faster on a memory-limited machine.

`REVIEW_MAX_FILES_PER_REQUEST` limits request size, not analysis quality. The UI reads `/api/v1/review/limits` before submission. Start small and inspect static-only/fallback labels before increasing the selection.

**Every review runs fresh.** `REVIEW_CACHE_ENABLED` is retired and ignored. Legacy cache entries can be inspected/deleted, but are not reused or repurposed for personalization. Fresh reviews do not fetch newer Git commits: re-index first when source has changed.

## Risk policy

```dotenv
RISK_GATE_ENABLED=true
RISK_APPROVAL_THRESHOLD=6
RISK_BLOCK_THRESHOLD=10
```

Risk is scored from 0–10, with higher values indicating greater risk. These settings do not replace exact-diff confirmation or patch-integrity checks. See [the policy gate](SECURITY.md#risk-policy-gate) for signals and approval behavior.

## Docker and deployment

The repository includes a [Compose file](../docker-compose.yml), a [backend Dockerfile](../backend/Dockerfile), and a [frontend Dockerfile](../frontend/Dockerfile).

```bash
docker compose up --build
```

Before running this, supply the Supabase values, review provider configuration, allowed frontend origins, and persistent storage. The supplied Compose file does **not** start Ollama; configure a backend-reachable Ollama address through your deployment environment/Compose override. Inside a container, `localhost` refers to that container, not your host machine.

The supplied frontend API build URL targets localhost for same-machine use. A remote deployment needs a browser-reachable API URL or same-origin reverse proxy, HTTPS, and matching Supabase redirect URLs. Configure `VITE_ALLOWED_HOSTS` for development previews rather than disabling host protection globally. Treat Compose as a starting point, not a production-hardening checklist.

**Continue:** [Model connections](MODEL_CONNECTIONS.md) · [Security](SECURITY.md) · [Troubleshooting](TROUBLESHOOTING.md)
