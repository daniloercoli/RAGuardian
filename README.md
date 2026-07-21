# RAGuardian

Self-hosted, multi-user Retrieval-Augmented Generation service for personal and company knowledge bases.

A production-ready Retrieval-Augmented Generation (RAG) service for enterprise knowledge bases. Upload PDFs, index them with modern embeddings, and query them through a conversational interface powered by multiple LLM providers. Includes a full REST API for seamless integration with enterprise services, mobile apps, and third-party platforms.

RAGuardian lets one deployment serve many people: each local user gets an isolated RAG workspace with separate uploads, file index, data sources, conversations, API keys, and Chroma collection. Admins keep provider configuration, model policy, and user management centralized.

**Enterprise Compliance**: When using Regolo.ai as the LLM provider, inference runs on infrastructure located in the European Union with zero data retention. Customer prompts and outputs are handled in memory only and are not persisted. Regolo does not use customer request content to train or fine-tune shared models, providing a simpler, more auditable compliance story aligned with European data sovereignty requirements.

**Mistral AI Data Policy**: Data is stored in the European Union. Mistral AI does not use customer data to train its AI models except under specific conditions (free subscription, paid subs without opt-out, feedback provision, or content moderation). For complete details, see [Mistral AI Data Usage Policy](https://legal.mistral.ai/).

> Status: beta | Python: 3.11+ | License: MIT

## 🚀 Features

- **Multi-Provider LLM Support**: Regolo.ai and Mistral AI default providers, plus custom OpenAI-compatible providers
- **Multi-user** local login with `admin` and `user` roles.
- **Personal workspaces** under `app/data/workspaces/<workspace_id>/` and `app/uploads/workspaces/<workspace_id>/`.
- **Isolated Chroma collections** per workspace, for example `documents_<workspace_id>`.
- **Advanced Embeddings**: Local, Regolo cloud, or custom OpenAI-compatible embeddings
- **Smart Reranking**: BAAI local, Regolo remote, or custom OpenAI-compatible reranking
- **Conversational Memory**: Context-aware responses with automatic summary compression
- **Streaming Responses**: Real-time token streaming with NDJSON structured events
- **Admin Dashboard**: Web UI for document/audio management, configuration, and monitoring
- **Versioned REST API**: Production-ready APIs with authentication and rate limiting
- **External Client Ready**: Scoped API keys and safe source payloads for server-to-server clients
- **Audio Workflow**: Configurable OpenAI-compatible Voice providers for STT indexing, optional STT language control, and TTS
- **OCR Fallback**: Default Regolo OCR provider for scanned PDFs and image-to-text chat input
- **WordPress Plugin Ready**: Modular WordPress client with customizable chatbot, shortcode, WXR article import, live public-post sync, retry/backoff, health checks, and abuse controls
- **Enterprise Logging**: Rotating file logs with multiple severity levels
- **Health Monitoring**: Deep health checks with readiness indicators

## 📋 Requirements

- **Python**: 3.11 or higher
  - Windows, Linux, macOS (Apple Silicon): Latest supported version
  - macOS (Intel): Python 3.12 recommended (PyTorch wheel compatibility)
- **API Keys**: Not needed to start the UI. Add one LLM provider key, or a local/custom OpenAI-compatible provider, before asking real questions.
- **Redis**: Not required for local development. The default local runtime uses in-process memory and inline jobs.
- **Optional WordPress Client**: WordPress 6.x or compatible hosting for the bundled plugin
- **Memory**: Minimum 2GB RAM for local embeddings
- **Disk**: 500MB+ for dependencies and local ChromaDB index

## 5-Minute Local Start

From a fresh clone:

```bash
git clone <repo-url>
cd Test-Rag-v1
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python app/app.py
```

Open:

- Chat: `http://127.0.0.1:5000/`
- Login: `http://127.0.0.1:5000/admin/login`

On a clean install, the first successful login bootstraps an admin user. Use email `admin@example.local` (or leave the email field empty) and password `admin`. Set a real password before using the app beyond local testing.

The copied `.env.example` keeps the app local: no Redis, JSON-backed users/settings, local ChromaDB, inline jobs, and local sentence-transformers embeddings where supported.

## Provider Setup

The UI starts without provider keys, but real chat answers require a model provider:

- Hosted provider: uncomment `REGOLO_API_KEY` or `MISTRAL_API_KEY` in `.env`, restart the app, then select the provider in **Admin -> Configuration**.
- Local/custom provider: start any OpenAI-compatible server, then add it in **Admin -> Configuration -> Custom LLM Providers**. Disable "requires API key" if your local server does not need one.
- Embeddings: `.env.example` selects local embeddings. If your platform cannot install `sentence-transformers` (notably macOS Intel), switch embeddings to Regolo or another custom embedding provider in **Admin -> Configuration**.

## Production Configuration

For shared or production deployments, replace the dev secrets in `.env`:

```env
RAG_ENV=production
FLASK_SECRET_KEY=replace-with-a-long-random-secret
RAG_SECRET_KEY=replace-with-a-different-long-random-secret

# Preferred bootstrap admin credential: store a Werkzeug password hash.
# Generate with:
# python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass()))"
RAG_ADMIN_PASSWORD_HASH=replace-with-generated-password-hash

# Legacy/dev fallback only:
# RAG_ADMIN_PASSWORD=replace-me

# Provider keys, depending on what you enable.
REGOLO_API_KEY=...
MISTRAL_API_KEY=...
```

`RAG_SECRET_KEY` protects user connector secrets. In development the app can fall back to a temporary/dev key, but production deployments should always set it explicitly. Prefer `RAG_ADMIN_PASSWORD_HASH` over plaintext `RAG_ADMIN_PASSWORD` for bootstrap admin credentials.

Redis is only needed when you want shared runtime state, queued background jobs, multiple Gunicorn workers, or production-style job monitoring. Keep `RAG_STATE_BACKEND=memory` and `RAG_QUEUE_BACKEND=inline` for local development.

## First Setup Flow

1. Log in and create the initial admin.
2. Go to **Configuration** and choose LLM, embeddings, reranker, voice, and OCR providers.
3. Go to **Users** and create normal users.
4. Each user logs in and uploads files or configures personal **Data Sources**.
5. For API clients or WordPress, create one API key in the workspace that should answer those requests, granting only the scopes the integration needs.

## Production Run

Use Gunicorn with the tracked runtime configuration:

```bash
gunicorn -c gunicorn.conf.py wsgi:application
```

See [Deployment](docs/DEPLOYMENT.md) for Redis, reverse proxy, logging, and production checklist details.

## Documentation

- [Start Here: practical guide from zero](docs/START_HERE.md)
- [Setup](docs/SETUP.md)
- [Multi-User Architecture](docs/MULTI_USER.md)
- [Data Ingestion Plugins](docs/INGESTION.md)
- [Deployment](docs/DEPLOYMENT.md)
- [API Reference](docs/API.md)
- [OpenAPI Schema](docs/openapi.yaml)
- [WordPress Plugin](integrations/wordpress/rag-client/README.md)
- [Public Repository Snapshot Workflow](docs/PUBLICATION.md)

## Repository Map

| Path | Purpose |
|------|---------|
| `app/app.py` | Flask app factory, core API routes, and job orchestration |
| `app/routes/` | Focused route registrars for auth, accounts, backups, and prompts |
| `app/utils/http_security.py` | CSRF, CORS, request timeout, and production config checks |
| `app/utils/rate_limiter.py` | In-memory and Redis-backed request rate limiting |
| `app/utils/user_store.py` | JSON-backed local user store |
| `app/utils/workspace.py` | Workspace path and collection resolver |
| `app/utils/secret_store.py` | Encrypted connector credential store |
| `app/utils/data_ingestion/` | Plugin contract, registry, Email IMAP, Microsoft Drive |
| `app/utils/document_indexer.py` | Shared indexing pipeline for uploads and connectors |
| `app/utils/rag_engine.py` | Retrieval, reranking, generation, conversation memory |
| `integrations/` | External platform integrations (WordPress, etc.) |
| `integrations/README.md` | Integration architecture, roadmap, and how to add new |
| `integrations/wordpress/rag-client/` | WordPress plugin with modular PHP architecture |
| `public-site/` | Static public website |
| `tests/` | Regression and architecture tests |

## API Example

Create an API key from the user workspace you want to query, then:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{
    "query": "Summarize my indexed documents",
    "response_language": "auto"
  }'
```

The API key resolves to one user/workspace. Queries, uploads, delete operations, jobs, and conversations stay inside that boundary.

## WordPress

The bundled WordPress plugin is a server-side client. Store a RAGuardian API key in WordPress settings; browser JavaScript never sees it.

Recommended pattern:

1. Create a dedicated RAGuardian user, for example `website@example.com`.
2. Index the website knowledge base in that user's workspace.
3. Create one API key for that workspace. Use `query` for chat, add `ingest` for WXR import/live article sync, and add `speech` only when TTS is enabled.
4. Configure `Settings -> Raguardian` in WordPress.

The plugin uses the standard WordPress settings UI, AJAX actions, WP-Cron, and post hooks. It does not expose the API key to JavaScript and does not read WordPress content directly from the database.

## Testing

```bash
pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Current multi-user regression coverage includes auth, workspace isolation, API key ownership, data sources, secret storage, jobs, upload/query/delete/rebuild, Chroma collection routing, and legacy API compatibility.

## Reset to Zero

To completely wipe all data and start fresh:

```powershell
Remove-Item -Recurse -Force "app\data", "app\chroma_db", "app\uploads" -ErrorAction SilentlyContinue
```

This removes users, workspaces, ChromaDB indexes, prompts, API keys, and all uploads. After running this, restart the app and the first login will bootstrap a new admin.

## Security Notes

- Do not commit `.env`, `app/data/*.json`, workspace data, uploads, ChromaDB, logs, or secrets.
- Use strong `FLASK_SECRET_KEY` and `RAG_SECRET_KEY` values in production.
- Use per-user API keys with the narrowest useful scopes.
- Store connector passwords/tokens as user secrets or environment references, never as plaintext in workspace settings.
- Use HTTPS in front of any public deployment.

## License

MIT. See [LICENSE](LICENSE).
