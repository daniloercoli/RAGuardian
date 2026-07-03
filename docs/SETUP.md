# Setup

This guide covers a clean local setup of RAGuardian.

For a step-by-step operational guide that also covers Redis, Docker, chat attachments,
code interpreter mode, API usage, and common troubleshooting, see
[Start Here](START_HERE.md).

## Requirements

- Python 3.11 or newer.
- No Redis for a local single-process run.
- One LLM provider key or a local OpenAI-compatible provider when you want real chat answers.
- 2GB+ RAM for local embeddings.
- Optional Redis only for shared runtime state, queued jobs, or multi-worker production.

## 5-Minute Local Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python app/app.py
```

Open `http://127.0.0.1:5000/admin/login`.

On a clean install, the first successful login bootstraps an admin account. Use email `admin@example.local` (or leave the email field empty) and password `admin`.

The default local `.env.example` uses:

- `RAG_STATE_BACKEND=memory`
- `RAG_QUEUE_BACKEND=inline`
- local ChromaDB under `app/chroma_db`
- JSON runtime files under `app/data`
- local sentence-transformers embeddings where supported

## Provider Configuration

The app starts without provider keys. To ask questions against your documents, choose one path:

- Hosted: set `REGOLO_API_KEY` or `MISTRAL_API_KEY` in `.env`, restart, then select the provider in **Admin -> Configuration**.
- Local/custom: run an OpenAI-compatible model server and add it from **Admin -> Configuration -> Custom LLM Providers**. Disable "requires API key" if your local server does not need one.
- Embeddings: keep local embeddings on supported platforms, or switch to Regolo/custom embeddings from **Admin -> Configuration**.

Voice and OCR are optional; configure them only for audio or scanned PDF workflows.

## ReRanking and Source Diversity

With ReRanking enabled, retrieval has four steps:

```text
question -> Chroma candidates -> optional Source Diversity -> reranker -> final k chunks -> LLM
```

`Documents for ReRanking (top_n)` controls how many chunks the reranker receives.
Without Source Diversity, those chunks are the top `top_n` results returned by
Chroma. If one large or very repetitive document dominates vector similarity,
many candidates can come from that same source, and other relevant books or PDFs
may never reach the reranker.

**Source Diversity** is an optional pre-reranker diversification step in
**Admin -> Configuration -> ReRanking**. It is OFF by default. When enabled, the
app retrieves a wider Chroma pool, then limits how many chunks from the same
source document are sent to the reranker. This gives more documents a chance to
compete before the reranker chooses the final `Retrieval Results (k)` chunks.

This is not full MMR. It is a simpler per-source chunk cap before reranking:

- use it when the same document monopolizes Chroma candidates;
- leave it off when you intentionally want pure vector similarity and repeated
  chunks from one source are acceptable;
- inspect the `rag_service.chroma` logs to see which source filenames and chunk
  IDs are passed to the reranker.

## Production Environment

Set these values for any non-trivial deployment:

```env
FLASK_SECRET_KEY=replace-with-a-long-random-secret
RAG_SECRET_KEY=replace-with-a-different-long-random-secret

# Preferred: hashed bootstrap admin credential.
RAG_ADMIN_PASSWORD_HASH=replace-with-generated-password-hash

# Legacy/dev fallback only:
# RAG_ADMIN_PASSWORD=replace-me
```

Provider examples:

```env
REGOLO_API_KEY=...
MISTRAL_API_KEY=...
```

Optional runtime state:

```env
RAG_STATE_BACKEND=redis
RAG_QUEUE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
RAG_QUEUE_NAME=rag-default
```

## First Admin Tasks

1. Open **Configuration**.
2. Confirm the default LLM provider and model.
3. Configure embeddings and reranking.
4. Configure Voice/OCR only if audio or scanned PDF workflows are needed.
5. Open **Users** and create normal users.

## First User Tasks

1. Log in with the user account.
2. Open **RAG Files** and upload PDF, TXT, Markdown, or audio files.
3. Open **Data Sources** to configure personal Email IMAP or Microsoft Drive sources.
4. Use **Sync now** on a data source when ready.
5. Ask questions from the Chat page.

## Runtime Files

Runtime files are intentionally ignored by git:

- `app/data/settings.json`
- `app/data/users.json`
- `app/data/secrets.json`
- `app/data/workspaces/`
- `app/uploads/`
- `app/chroma_db/`
- `app/logs/`
