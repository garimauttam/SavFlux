# Troubleshooting

[Home](../README.md) / [Docs](README.md) / Troubleshooting

Start with the first actual error, not the number of Network requests. A successful asset request does not prove that the app initialized; an installed model does not prove that generation completed.

## Missing frontend package or font

Symptoms include `Failed to resolve import`, `@supabase/supabase-js` missing, or `ENOENT ... @fontsource/geist-mono/latin-400.css`.

Stop Vite with Ctrl+C. From the repository root:

```bash
cd frontend
node --version
npm ci
npm ls @fontsource/geist-mono @supabase/supabase-js vite
npm run dev
```

Node must be at least 22.12.0. If you use nvm, run `nvm install && nvm use` from `frontend/`. Install here, not at the repository root; keep the committed lockfile. Do not disable Vite's overlay or remove imports to hide missing dependencies. Restart and hard-refresh after installation.

## Blank page or occupied port

1. Use the URL printed by the current Vite process. Port 5173 is strict; do not accidentally keep viewing an older server.
2. Stop old SavFlux servers from their terminals. On macOS, `lsof -nP -iTCP:5173 -sTCP:LISTEN` helps identify the listener; stop only the process you recognize.
3. Open DevTools → Console, refresh, and capture the **first red error**. Redact keys and tokens before sharing it.
4. After environment changes, restart both frontend and backend, then use Cmd+Shift+R (macOS) or Ctrl+Shift+R.

## Supabase sign-in is not configured

A backend `Application startup complete` message means it started. An auth-configuration warning means protected routes remain blocked; do not bypass authentication.

Use the same project URL and public publishable/anon key in `backend/.env` and `frontend/.env.local`, with the appropriate `VITE_` prefixes in the latter. Keep `VITE_API_URL` blank for the local proxy. Follow [sign-in setup](GETTING_STARTED.md#2-configure-sign-in), including Email, redirect URLs, and optional Google OAuth. Never use a service-role key in browser variables.

If an email-link URL says the OTP expired, request a fresh link. That is separate from a model timeout or source parse failure.

## Model unavailable or fix generation timed out

```bash
ollama list
ollama ps
curl http://localhost:11434/api/tags
ollama run qwen2.5-coder:7b 'Reply with OK'
```

Pull the model first if it is absent. Check the **Review** model in Profile; the chat model can differ. Download size is not runtime memory usage. Try 1–3 review files, let the review finish (or press Stop), then request one fix. A large refactor may exceed time or output limits even without concurrent work.

The suggestion deadline is bounded; increasing it indefinitely does not resolve unavailable memory or invalid credentials. Use a smaller installed local Review model or explicitly configured free-tier provider. [Model diagnostics](MODEL_CONNECTIONS.md) explains routing, safe error categories, fallback, and limits.

## Compare is disabled or GitHub refuses a comparison

| State | Next step |
| --- | --- |
| Branches loading | Wait for discovery to finish; retry lookup if it fails |
| Same base and compare ref | Select two different branches |
| Public repository, no GitHub connection | Current versions permit read-only comparison; update and restart **both** backend and frontend |
| Private repository or missing ref | Check the ref names and connect a GitHub account with read access |
| API rate limit | Wait for reset; a GitHub connection can provide higher public-read limits |
| Indexed-file mode | Choose two distinct indexed files; use Branches for a Git branch comparison |

[Changes workspace](CHANGES_WORKSPACE.md) explains these independent data sources. This comparison does not depend on the selected language model.

## Source does not match or looks stale

Indexed source can be reconstructed from chunks and is not a live disk view. Compare it with the actual file, re-index the intended branch, and run a new review. Hash mismatches disable anchored fixes; parser failures should remain unrated, not be interpreted as a clean bill of health. If malformed reconstruction persists, report the file path and a small redacted reproduction rather than applying a generated patch blindly.

## Tests or benchmark appear stuck

Install the development dependencies first. Backend tests mock external services and configure a non-downloading test provider in `conftest.py`; an explicit environment override may change that. Use `--embedder offline --no-rerank` for the network-free retrieval regression gate. See [Development](DEVELOPMENT.md) for commands and [Benchmarks](BENCHMARKS.md) for what the numbers mean.

**Continue:** [Getting started](GETTING_STARTED.md) · [Configuration](CONFIGURATION.md)
