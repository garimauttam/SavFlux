# Getting started

[Home](../README.md) / [Docs](README.md) / Getting started

Run SavFlux locally with Ollama for inference and Supabase for account sign-in. No paid model API key is required for this path; you still need network access for sign-in and initial model downloads.

## Prerequisites

| Requirement | Recommended starting point |
| --- | --- |
| Python | 3.11 |
| Node.js | 22.12+; Node 22 LTS recommended |
| Git | Installed and available in your terminal |
| Local model runtime | [Ollama](https://ollama.com), running locally |
| Account service | A [Supabase](https://supabase.com) project with Email enabled; Google OAuth is optional |

Model download size is not its total RAM requirement. Start with the default 7B model and a small review before selecting larger models.

## 1. Clone and prepare

```bash
git clone https://github.com/garimauttam/SavFlux.git
cd SavFlux
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
ollama pull qwen2.5-coder:7b
```

Keep Ollama running. On an existing installation, edit your env files instead of copying over them.

## 2. Configure sign-in

Use the **same Supabase project** for both files. Its publishable/anon key is public; never use a service-role or secret key in a `VITE_*` variable.

`backend/.env`:

```dotenv
LLM_PROVIDER=ollama
OLLAMA_CHAT_MODEL=qwen2.5-coder:7b
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_ANON_KEY=YOUR_PUBLIC_PUBLISHABLE_OR_ANON_KEY
```

`frontend/.env.local`:

```dotenv
VITE_API_URL=
VITE_SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
VITE_SUPABASE_ANON_KEY=YOUR_PUBLIC_PUBLISHABLE_OR_ANON_KEY
```

In Supabase **Authentication**, enable Email and allow `http://localhost:5173` as a redirect URL. Use that as the Site URL for a local-only project; keep the production Site URL if the project already serves production. Google sign-in additionally needs a configured Google OAuth provider. Configure SMTP before inviting real users.

## 3. Start the backend

In **terminal A**, from the repository root:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. The initial embedding/reranker setup may download and cache model weights.

## 4. Start the frontend

In **terminal B**, from the repository root:

```bash
cd frontend
# Optional, if nvm is installed: nvm install && nvm use
npm ci
npm run dev
```

Open **http://localhost:5173**, create an account, and confirm your email if confirmation is enabled. A blank `VITE_API_URL` uses the development proxy to the backend.

## 5. Verify your first review

1. Check `http://localhost:8000/health`. An unavailable configured provider can make it return 503; that is a diagnostic, not proof the backend failed to start.
2. In **Repositories**, index a public GitHub URL or upload code. Public indexing does not need a GitHub token; private repositories need access.
3. Select the remote branch before indexing. In **Review**, start with 1–3 files.
4. Read the source-linked findings. Wait for the run to finish or press **Stop** before generating an individual fix.
5. Use **Changes → Branches** for a remote branch comparison, not the Indexed files view.

## Update an existing checkout

Keep uncommitted work safe. Pull the branch you intentionally use; do not discard local changes to force an update.

```bash
git status
git pull --ff-only
(cd frontend && npm ci)
```

Stop and restart both servers, then hard-refresh the browser. Restarting Vite alone does not install new packages, and frontend-only updates cannot fix an older backend. See [dependency troubleshooting](TROUBLESHOOTING.md#missing-frontend-package-or-font).

**Continue:** [Workspace guide](WORKSPACE.md) · [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md)
