# RAGuardian: start here

This is the practical, copy-paste friendly guide for starting from zero and using the main RAGuardian features: persistent RAG, temporary chat attachments, Python code interpreter, Redis, Docker, API access, and WordPress.

## 1. Requirements

Minimum local setup:

- Python 3.11 or newer.
- Git.
- One LLM provider: Regolo, Mistral, or any local/custom OpenAI-compatible server.

Recommended for the full feature set:

- Docker Desktop, required for the Python code interpreter.
- Redis, required for shared runtime state, RQ workers, queued jobs, and multi-worker deployments.
- Node.js, only if you work on the bundled WordPress plugin.

## 2. Install from a clean checkout

From the project root:

```bash
cd /Users/daniloercoli/opt/Test-Rag-v1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Start the development server:

```bash
python app/app.py
```

Open:

- Chat: `http://127.0.0.1:5000/`
- Admin login: `http://127.0.0.1:5000/admin/login`

On a clean install, the first successful login bootstraps an admin user.

- Email: `admin@example.local`, or leave the email field empty.
- Password: `admin`.

Change credentials before using the system outside local development.

## 3. Configure models and providers

The UI can start without provider keys, but real answers require at least one LLM provider.

Open **Admin -> Configuration** and choose one path:

- Regolo: set `REGOLO_API_KEY` in `.env`.
- Mistral: set `MISTRAL_API_KEY` in `.env`.
- Local/custom: add an OpenAI-compatible endpoint in the UI.

Example `.env`:

```env
REGOLO_API_KEY=...
MISTRAL_API_KEY=...
```

Embeddings can be:

- local `sentence-transformers`, where supported;
- Regolo/custom embeddings, configured from **Admin -> Configuration**.

ReRanking is optional. When enabled, Chroma first retrieves candidate chunks,
then the reranker sorts those candidates and returns the final context chunks
for the LLM. The reranker can only rank what Chroma gives it.

If Chroma keeps returning many chunks from the same PDF while another relevant
book never appears in the candidates, set **Candidate Diversity** in
**Admin -> Configuration -> ReRanking**. It is OFF by default. **Source
Diversity** applies a simple per-source chunk cap; **MMR Diversity** uses
Maximal Marginal Relevance to select semantically diverse Chroma candidates
before the reranker. Only one mode can be active.

## 4. Add documents to the persistent knowledge base

For persistent RAG:

1. Open **Admin -> Files**.
2. Upload PDF, TXT, Markdown, or textual CSV files.
3. The file is parsed, chunked, embedded, and stored in Chroma.
4. Future questions can retrieve those chunks from the user's workspace.

These files become part of the user's persistent workspace until deleted.

## 5. Use the chat

The chat has three main behaviors.

### No attachment

Flow:

```text
question -> workspace Chroma/RAG -> LLM -> answer
```

Use this to ask questions about documents already indexed in the knowledge base.

### Attachment with Code Interpreter OFF

Flow:

```text
attachment -> temporary chunks + temporary embeddings in memory
question -> persistent Chroma/RAG + temporary attachment RAG -> LLM -> answer
```

The attached file:

- is used for the current question;
- is not added to Chroma;
- does not pollute the knowledge base;
- works best for PDF/TXT/MD/CSV files that are useful as text context.

This prevents the confusing behavior where a user attaches a file and the system silently ignores it.

### Attachment with Code Interpreter ON

Flow:

```text
attachment -> Docker /data/...
question -> persistent RAG as extra context -> LLM generates Python -> Docker executes -> output/charts
```

The attached file:

- is not indexed in Chroma;
- is mounted inside the container as `/data/<filename>`;
- is analyzed by Python;
- can produce text output and PNG charts.

Use this mode for CSV/XLSX/JSON datasets when you want calculations, statistics, aggregations, or charts.

## 6. Docker for the Python code interpreter

On macOS, start Docker Desktop:

```bash
open -a Docker
```

Wait until the Docker daemon is ready:

```bash
until docker info >/dev/null 2>&1; do sleep 2; done
```

By default, the app builds the code interpreter image automatically on first use
when Docker is available. You can also build it manually from the project root:

```bash
docker build -f Dockerfile.code-interpreter -t code-interpreter:latest .
```

Useful `.env` settings:

```env
CODE_INTERPRETER_ENABLED=1
CODE_INTERPRETER_AUTO_BUILD=1
CODE_INTERPRETER_TIMEOUT=60
CODE_INTERPRETER_MAX_FILE_MB=25
CODE_INTERPRETER_TTL_HOURS=24
CODE_INTERPRETER_DOCKER_MEMORY=512m
CODE_INTERPRETER_DOCKER_CPU_QUOTA=100000
```

Quick check:

```bash
docker image inspect code-interpreter:latest
```

Manual UI smoke test:

1. Start the app.
2. Enable **Code Interpreter** in the chat.
3. Attach a CSV.
4. Ask: `calculate the total and create a chart`.

## 7. Redis quick commands

Redis is optional for simple local development. The default local mode is:

```env
RAG_STATE_BACKEND=memory
RAG_QUEUE_BACKEND=inline
```

Use Redis when you need shared state, RQ workers, background jobs, data-source polling, or multi-worker deployments.

### macOS/Homebrew Redis

Start Redis:

```bash
brew services start redis
```

Stop Redis:

```bash
brew services stop redis
```

Restart Redis:

```bash
brew services restart redis
```

Check Redis:

```bash
redis-cli ping
```

Expected response:

```text
PONG
```

### Redis with Docker

If you do not use Homebrew Redis:

```bash
docker run --name raguardian-redis -p 6379:6379 -d redis:7
```

## 8. Redis-backed app runbook

Set these values in `.env`:

```env
RAG_STATE_BACKEND=redis
RAG_QUEUE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
RAG_QUEUE_NAME=rag-default
```

Then use separate terminals from the project root.

### Terminal 1: RQ worker

```bash
PYTHONPATH=app .venv/bin/rq worker rag-default
```

Equivalent if your virtualenv is already active:

```bash
PYTHONPATH=app rq worker rag-default
```

### Terminal 2: web app

Development server:

```bash
python app/app.py
```

Or run through the WSGI entry point:

```bash
.venv/bin/python wsgi.py
```

For production-style serving:

```bash
gunicorn -c gunicorn.conf.py wsgi:application
```

## 9. Data-source sync with poller

For scheduled data-source synchronization, run the worker and the poller as separate processes.

Terminal 1:

```bash
PYTHONPATH=app rq worker rag-default
```

Terminal 2:

```bash
PYTHONPATH=app python -m utils.data_ingestion.poller
```

Terminal 3:

```bash
python app/app.py
```

Required `.env`:

```env
RAG_STATE_BACKEND=redis
RAG_QUEUE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
RAG_QUEUE_NAME=rag-default
```

Optional poller interval:

```env
RAG_DATA_SOURCE_POLLER_INTERVAL_SECONDS=60
```

## 10. External data sources

Each user has isolated data sources in their own workspace.

From **Data Sources**, a user can configure:

- local/server folders;
- Email IMAP;
- Microsoft Drive.

Synchronized content is indexed into that user's persistent knowledge base.

## 11. API and WordPress

For API or WordPress usage:

1. Create a dedicated user, for example `website@example.com`.
2. Load or sync that user's knowledge base.
3. Create an API key for that user.
4. Grant only the scopes needed:
   - `query` for chat/query;
   - `ingest` for import/sync;
   - `speech` only for STT/TTS.

API example:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{"query": "Summarize the indexed documents"}'
```

For WordPress, see:

```text
integrations/wordpress/rag-client/README.md
```

## 12. Production notes

Before exposing the service:

- set `FLASK_SECRET_KEY`;
- set `RAG_SECRET_KEY`;
- prefer `RAG_ADMIN_PASSWORD_HASH` over plaintext admin passwords;
- use HTTPS behind a reverse proxy;
- configure Redis before running multiple workers;
- do not commit `.env`, uploads, ChromaDB, `app/data`, logs, or secrets.

Example secrets:

```env
FLASK_SECRET_KEY=replace-with-long-random-secret
RAG_SECRET_KEY=replace-with-another-long-random-secret
RAG_ADMIN_PASSWORD_HASH=replace-with-generated-password-hash
```

Generate an admin password hash:

```bash
python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass()))"
```

## 13. Tests and checks

Install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

Useful checks:

```bash
redis-cli ping
docker info
docker image inspect code-interpreter:latest
curl http://127.0.0.1:5000/health
```

## 14. Common issues

### Docker is not reachable

Symptom:

```text
permission denied while trying to connect to the docker API
```

Check Docker Desktop:

```bash
open -a Docker
docker info
```

### Code interpreter image build fails

Symptom:

```text
Immagine Docker non trovata e auto-build non disponibile
```

Fix or rebuild manually:

```bash
docker build -f Dockerfile.code-interpreter -t code-interpreter:latest .
```

### Redis is not responding

Check:

```bash
redis-cli ping
```

If it does not return `PONG`, start or restart Redis:

```bash
brew services start redis
brew services restart redis
```

### Chat says there are no documents

Upload at least one persistent document in **Admin -> Files**, or attach a file in chat with Code Interpreter OFF to use it as temporary RAG context.

### CSV/XLSX answers are weak with Code Interpreter OFF

Use Code Interpreter ON for calculations, averages, charts, trends, and aggregations. Code Interpreter OFF treats attachments as temporary text/RAG context, not as full dataframes.

### Redis is not needed for basic local use

For a simple local run:

```env
RAG_STATE_BACKEND=memory
RAG_QUEUE_BACKEND=inline
```

Switch to Redis when you need workers, long jobs, polling, or production-style deployment.
