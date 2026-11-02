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

| Variable | Value | Secret? |
|---|---|---|
| `GROQ_API_KEY` | your Groq API key | **yes** |
| `LLM_PROVIDER` | `groq` | no |
| `LLM_MODEL` | `qwen/qwen3.8-27b` | no |
| `APP_ENV` | `production` | no |
| `PYTHON_VERSION` | `3.12.8` | no |

Set these in the hosting dashboard, **never** in `.env.example` or in Git.
`GROQ_API_KEY` is what enables live LLM answers; without it the app still works
in extractive mode.

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

A direct Vercel deployment is **not supported** for this Streamlit app. The
practical options are:

1. **Frontend/backend split (the only real Vercel option).** Keep a Next.js or
   static frontend on Vercel and move the RAG pipeline to a long-lived API
   service (Render/Railway/Fly.io). The frontend calls the API over HTTPS. This
   requires a new API layer and changes to the UI.
2. **Vercel as a landing page only.** Put a static page on Vercel that links to
   the Streamlit app hosted elsewhere.
3. **Rewrite the UI.** Replace Streamlit with a Vercel-compatible Next.js UI and
   expose the Python pipeline as an HTTP API. This is a new deployment
   architecture, not a configuration change.

Do not add a `vercel.json` that points at `streamlit run`: Vercel will build it
but the deployed function cannot keep the Streamlit server and WebSocket alive.

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
