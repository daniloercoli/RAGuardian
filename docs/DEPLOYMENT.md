# Deployment

## Development

```bash
python app/app.py
```

The development server is suitable for local testing only.

## Gunicorn

```bash
gunicorn -c gunicorn.conf.py wsgi:application
```

Default production posture is conservative:

```env
GUNICORN_WORKERS=1
GUNICORN_THREADS=16
```

Use multiple workers only after Redis-backed runtime state and queued jobs are configured.

## Redis Runtime

For shared runtime state:

```env
RAG_STATE_BACKEND=redis
RAG_QUEUE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
RAG_QUEUE_NAME=rag-default
```

Run a worker:

```bash
PYTHONPATH=app rq worker rag-default
```

Run the dedicated data-source poller as a separate process when periodic sync is enabled:

```bash
PYTHONPATH=app python -m utils.data_ingestion.poller
```

Set the poller tick interval if needed:

```env
RAG_DATA_SOURCE_POLLER_INTERVAL_SECONDS=60
```

Redis-backed mode is recommended before:

- multiple Gunicorn workers;
- long-running ingestion;
- parallel rebuilds;
- periodic data-source polling;
- production-style job monitoring.

## Reverse Proxy

Example Nginx block:

```nginx
server {
    listen 443 ssl;
    server_name rag.example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
    }
}
```

`proxy_buffering off` keeps streaming responses responsive.

## Health

Public health:

```text
GET /health
```

Workspace/API health:

```text
GET /api/v1/health
X-API-Key: <workspace-key>
```

The API health response reports queue, Redis, database, index, OCR, voice, and readiness fields for the workspace resolved by the API key.

## Production Checklist

- Set `FLASK_SECRET_KEY`.
- Set `RAG_SECRET_KEY`.
- Configure provider API keys.
- Create admin and normal users.
- Use workspace API keys for integrations.
- Enable HTTPS.
- Keep runtime JSON, uploads, ChromaDB, logs, and secrets out of git.
- Configure Redis before multi-worker deployment.
- Run `.venv/bin/pytest -q` before release.
