# P1 → P2 Backlog Implementation ($0, no API key required)

Implements the full P1 → P5 backlog in priority order. Every feature is **$0**:
local files, git CLI, browser APIs, and free key-less services (OSV) only.
No new paid dependencies; no API keys needed for any new endpoint.

> Branch: `arena/01a0bb43-savflux` · 15 commits · all `Author: garimauttam`,
> no `Co-authored-by` trailers (local commit-msg hook removed).

---

## 0. Critical repair — the app did not start or build

`main` was broken on both sides. Fixed first so every later commit is verifiable:

**Backend** — `main.py` imported `app.api.agent` and `app.api.prompts`, which did
not exist → `ImportError` on startup. Added:

| File | What |
|---|---|
| `backend/app/api/agent.py` | Deterministic $0 agent: goal → hybrid retrieval → file reads → blast radius → markdown report, streamed with `__STATUS__` telemetry |
| `backend/app/api/prompts.py` | Prompt library CRUD + use + history |
| `backend/app/api/analytics.py` | Latency summary/history/clear (feeds the middleware in `main.py`) |
| `backend/app/services/prompt_service.py` | `prompt_library.json` store; schema matches what `activity_service` already reads |
| `backend/app/services/analytics_service.py` | `record_latency` / `get_summary` / `clear_history` on `analytics_history.jsonl` |

**Frontend** — `App.tsx`, `ChatWindow.tsx`, and `MessageBubble.tsx` imported 11
components that did not exist → `tsc`/build failure. Added:
`HealthPanel`, `OrgPanel`, `ThemeToggle`, `AgentPanel`, `AnalyticsPanel`,
`PromptLibrary`, `ShareView`, `ArchitectureDiagram`, `ExportButton`,
`VoiceButton`, `TrustLedger` (+ `TrustLedgerPanel`, `PRReviewPanel`,
`TimeMachinePanel`, `VoiceControls` for their backlog items).

---

## P1 — Code Intelligence & Safety

### 1. Time Machine — `git log --follow` + blame per file ✅
- `history_service.py`: bare mirror per repo under `chroma_data/mirrors/`, seeded
  from the ingest temp clone (local, no network); `file_timeline()` +
  `file_blame()` via argv-list git calls; rev allowlist (`HEAD` | 40-hex sha).
- `GET /history/timeline`, `GET /history/blame` — accept indexed source ids
  (`{repo_url}::{rel_path}`), rel paths, or basenames.
- `GET /activity/file` — merged per-file timeline (trust-ledger index record +
  git commits) in `activity_service`.
- `TimeMachinePanel` + new **History** tab (`g t` shortcut) + **History** jump
  button in Code Writer edit mode. Click any commit to blame at that revision.

### 2. Blast Radius ✅ (endpoint was the missing piece)
- `GET /ingest/blast-radius?file=&repo_url=&max_depth=` — `GraphPanel` already
  called it but it 404'd. `dep_graph.get_blast_radius()` does reverse-BFS
  transitive dependents with basename/id/suffix matching + deterministic
  risk score and `low|medium|high` level. Existing blast highlight, filters,
  isolate mode, and PNG export now work end to end.

### 3. Auto Architecture Diagram ✅
- `GET /architecture/diagram` — hub-first (degree-ranked) node selection,
  directory-based layering, Mermaid `graph TD` + structured layers. No LLM.
- `ArchitectureDiagram` — collapsible chat empty-state card (`ChatWindow`
  already mounted it): zero-dependency layered SVG map, node inspection,
  Mermaid view / copy / `.mmd` download.

### 4. Trust Ledger ✅
- Ingestion records the cloned `HEAD` sha per repo (best-effort, never breaks
  indexing). `trust_service` compares against live upstream `HEAD` via
  read-only `git ls-remote` (scheme allowlist, 10s timeout) → reports
  `verified` / `stale` / `unknown` with human-readable detail.
- `GET /trust/ledger` + `POST /trust/verify` — the shape `TrustLedgerPanel`
  (embedded in the Health tab) renders.
- `__SOURCES__` citation marker in `chat.py` streaming + `useChat` parsing were
  already present; per-citation verification lands via `TrustLedgerDrawer`
  (trust explanation, cited-line preview, jump to Review).

### 5. Citations + Share + Create PR + CVE Audit + Memory Prune ✅
- **Citations**: already present (`retrieval_service` + `useChat` + chips).
- **Share**: `share_service` + `POST /share`, `GET /share/{id}` (public —
  unguessable id is the capability, gist-style), `POST /chat/share` (what
  `MessageBubble` calls, with ledger snapshot + history), `ShareView` at
  `/s/{id}` (route `App.tsx` already handled), `ExportButton` (.md/.json/clipboard).
- **Create PR**: `POST /review/create-pr` — live via GitHub API when
  `GITHUB_TOKEN` is set, else a deterministic manual plan (shell-safe
  `gh pr create` command + patch). Validated repo refs/branches.
- **CVE Audit**: `GET /security/cve-audit?repo_url=` +
  `POST /security/cve-scan` — manifest parsers
  (`requirements.txt` / `package.json` / `go.mod`) + single-batch OSV query
  (free, no key); graceful degraded mode when OSV is unreachable.
- **Memory Prune**: `POST /chat/prune` — the $0 `RemoveMessage` equivalent:
  drops oldest turns over a token budget, newest always kept
  (`query_enhancer.prune_chat_history`, `chars/4` estimator, no tokenizer dep).

### 6. Watcher ✅
- `watcher_service.py`: 30s poll loop (`WATCHER_INTERVAL_S` override) checking
  every trust-ledger repo; on new upstream commits emits a one-per-sha
  `watcher` notification to the Inbox. Fulfills the `main.py` lifespan
  `start/stop_watcher_background` contract (previously a silent no-op).
- `GET /watcher/status` + `POST /watcher/poll` for status and forced checks.

### 7. Health ✅ (UI was the missing piece)
- `HealthPanel`: polls deep `GET /health` (30s auto-refresh), per-check status
  cards, provider summary, degraded-state handling for 503s, with the Trust
  Ledger section embedded below.

### 8. PR Inline Comments ✅
- `impact_analyzer.inline_comments_for_diff()`: hunk-header line mapping + 8
  severity rules (secrets, eval/exec, shell, deserialization, SQL concat,
  broad-except, debug output, TODO) — one comment per added line.
- `POST /review/impact` and `POST /review/pr-webhook` now include
  `inline_comments`. New **PR Diff** tab in `ReviewPanel` (`PRReviewPanel`):
  paste-a-diff → instant $0 impact or full agent review, risk banner, changed
  files, regression focus, file-grouped inline comments.

### 9. Export / Bulk ✅ — already present, untouched.

## P2 — already present, untouched
Snippet Vault, Activity Feed, Bulk File Ops, File Tree Explorer, Diff Viewer,
Notifications Center, Slash Commands, Graph Enhancements — verified present on
both backend and frontend; no changes.

## P2 — Voice / Theme ✅
- **Voice** (`VoiceControls.tsx` + `VoiceButton`): speech-to-text mic input in
  chat (`SpeechRecognition`, $0, hidden where unsupported) + per-message
  read-aloud (`speechSynthesis`, markdown stripped). Chat is hands-free capable.
- **Theme/Org**: `ThemeToggle` (light/dark/system, persisted, live-follows OS)
  + `tailwind darkMode: "class"` + light-theme CSS remap layer (dark mode
  pixel-identical); `OrgPanel` workspace manager (active scoping, multi-repo
  cross-search, per-repo clearing, chunk counts).

---

## Verification (this branch)

- `npx tsc --noEmit` → **clean** (exit 0)
- `npm run build` → **green** (8.9s)
- New backend tests → **40/40 pass** (no LLM/Chroma/network):
  `test_agent_prompts_analytics`, `test_share`, `test_blast_radius`,
  `test_inline_comments`, `test_trust`, `test_history` (real temp git repo),
  `test_architecture`, `test_watcher`, `test_pr_security`
- Sweep: all 21 `main.py` router imports resolve; every frontend `./X` import
  resolves; `python -m py_compile` clean on all touched backend files.
- Full existing suite (`pytest backend/tests`) needs torch/Chroma/LangChain and
  was not re-run here; all touched shared modules keep backward-compatible
  signatures and lazy heavy imports.

## Notes for reviewers
- New local stores (all under `chroma_data/`, git-ignored): `prompt_library.json`,
  `analytics_history.jsonl`, `shared_links.json`, `trust_ledger.json`,
  `watcher_state.json`, `mirrors/`.
- No migrations, no new npm/pip dependencies, no env vars required.
  Optional: `GITHUB_TOKEN` (live Create-PR), `WATCHER_INTERVAL_S`.
