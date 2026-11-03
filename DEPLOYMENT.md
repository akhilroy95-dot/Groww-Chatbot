# Deployment

## TL;DR

- **Streamlit cannot run on Vercel serverless functions.** The app needs a
  long-lived process and a WebSocket connection between the browser and the
  Streamlit server. Vercel Functions are short-lived HTTP handlers and do not
  support that model.
- Use **Render** or **Streamlit Community Cloud** for this app. Both run the
  Streamlit server process and support WebSockets.
- The prebuilt index is committed on purpose: `data/01_raw`, `data/02_chunks`,
  `data/03_embeddings`, and `data/04_vector_db`. Request traces, answer traces,
  logs, and `.env` stay ignored.

## Required environment variables

Pick **one** live-LLM provider; the app falls back to extractive answers when
the provider is missing or fails.

| Variable | Value | Secret? |
|---|---|---|
| `GROQ_API_KEY` | your Groq API key (free at <https://console.groq.com/keys>) | **yes** |
| `LLM_PROVIDER` | `groq` | no |
| `LLM_MODEL` | `qwen/qwen3.8-27b` | no |
| `APP_ENV` | `production` | no |
| `PYTHON_VERSION` | `3.12.8` | no |

Alternative provider (wired in `app/llm/client.py`):

| Variable | Value | Secret? |
|---|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic API key (must have credit) | **yes** |
| `LLM_PROVIDER` | `anthropic` | no |
| `LLM_MODEL` | `claude-sonnet-4-5` | no |

Set these in the hosting dashboard, **never** in `.env.example` or in Git.
Without a working key the app still answers from the index in extractive mode;
the answer status then shows `llm_unavailable` or `llm_error` with the reason.

## Option A — Render (recommended)

1. Push the repository to GitHub.
2. In Render, choose **New → Blueprint** and select the repository. Render reads
   `render.yaml`.
3. If you prefer a manual web service instead of the Blueprint:
   - Environment: **Python**
   - Build command: `pip install -r requirements.txt`
   - Start command:
     ```text
     streamlit run app/ui/streamlit_app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --browser.gatherUsageStats false
     ```
   - Health check path: `/_stcore/health`
   - Python version: `3.12.8`
4. Add `GROQ_API_KEY` in **Environment → Secret File / Environment Variables**.
5. Deploy. First request downloads the MiniLM model, so expect a slower cold
   start. The free 512 MB plan may be too small for torch + MiniLM; the
   Blueprint uses `starter` (2 GB). Switch to `free` only if you accept possible
   OOM restarts.

## Option B — Streamlit Community Cloud

1. Push the repository to GitHub (public or private).
2. Go to <https://share.streamlit.io>, choose **New app**, and select the repo.
3. Main file path: `app/ui/streamlit_app.py`.
4. Python version: `3.12`.
5. Open **Advanced settings → Secrets** and add:
   ```toml
   GROQ_API_KEY = "your-key"
   LLM_PROVIDER = "groq"
   LLM_MODEL = "qwen/qwen3.8-27b"
   ```
   Streamlit secrets become environment variables available to the app.
6. Deploy. The app serves at `https://<app-name>.streamlit.app`.

The inspector is available at:

```text
https://<app-name>.streamlit.app/2_Pipeline_Inspector?debug=1
```

## Option C — Vercel

A direct Vercel deployment of `streamlit run` is **not possible**. Streamlit is
a stateful Tornado server: the browser opens a WebSocket to a process that must
stay alive for the whole session. Vercel Functions are request-scoped HTTP
handlers with a size limit and no persistent WebSocket server, so the app cannot
run there. There is no `vercel.json` that changes this.

If Vercel is a hard requirement, use the split architecture below. It needs a
small API layer that does not exist yet; the Streamlit app cannot be reused
as-is.

### C1. API host (Render/Railway/Fly.io) — parameters

| Setting | Value |
|---|---|
| Root directory | repository root |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn api.main:app --host 0.0.0.0 --port $PORT` (after adding `api/main.py`) |
| Health check | `/health` |
| Python | `3.12.8` |
| Env | `GROQ_API_KEY` (secret), `LLM_PROVIDER=groq`, `LLM_MODEL=qwen/qwen3.8-27b` |
| Env | `APP_ENV=production`, `ALLOWED_ORIGIN=https://<vercel-app>.vercel.app` |

The API should expose `POST /ask` with `{"query": "..."}` and return the same
`AnswerResult` shape (`status`, `body`, `citation_url`, `citation_label`,
`last_updated_from_sources`, `mode`, `reason`). Call
`AnswerService.answer_with_trace()` from `app/pipeline/answer.py`; do not
expose the pipeline inspector on the public API.

### C2. Vercel project — parameters

| Setting | Value |
|---|---|
| Framework preset | Next.js |
| Root directory | `web/` (new frontend) |
| Build command | `npm run build` (default) |
| Output directory | `.next` (default) |
| Install command | `npm install` (default) |
| Node version | `20.x` or `22.x` |

Environment variables:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_API_URL` | `https://<api-host>.onrender.com` |
| `API_SHARED_SECRET` | random string, also set on the API host (optional) |

The Vercel function only proxies to the API. If the key must stay server-side,
add `app/api/ask/route.ts` that forwards the request with the secret; never put
provider keys in `NEXT_PUBLIC_*`.

### C3. Vercel CLI steps

```powershell
npm i -g vercel
vercel login
vercel link
vercel env add NEXT_PUBLIC_API_URL production
vercel --prod
```

### C4. What not to do

- Do not commit `vercel.json` with `"builds": [{"src": "app/ui/streamlit_app.py", "use": "@vercel/python"}]`:
  it builds, but the function dies at the first WebSocket upgrade.
- Do not put `GROQ_API_KEY` / `ANTHROPIC_API_KEY` in `NEXT_PUBLIC_*`; those
  values are shipped to the browser.

The simplest path that keeps this app unchanged is **Render** (Option A) or
**Streamlit Community Cloud** (Option B).

## Local run

```powershell
# 1. Add the key only to .env (never .env.example, never Git)
#    GROQ_API_KEY=...

# 2. Run the app
.\.venv\Scripts\python.exe -m streamlit run app/ui/streamlit_app.py

# 3. Inspector
#    http://localhost:8501/2_Pipeline_Inspector?debug=1
```

Verify the key is loaded without printing it:

```powershell
.\.venv\Scripts\python.exe -c "import os; from app.config import load_settings; s=load_settings(None); print('llm_ready:', bool(os.environ.get(s.api_key_env)))"
```

## Git push

```powershell
git init -b main
git add .
git commit -m "Groww MF facts assistant with retrieval, UI, and deployment config"
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```

If Git asks for credentials, use a GitHub personal access token as the password
(or sign in with Git Credential Manager). A URL with the token embedded works
but leaves the secret in shell history; prefer the credential manager.

Before committing, confirm the secret is not staged:

```powershell
git status --short
git check-ignore -v .env
```

`git check-ignore` must report `.env` as ignored.
