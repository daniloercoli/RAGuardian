# RAG Service API v1

Public documentation for integrating external applications with the RAG service.

## OpenAPI Contract

The OpenAPI 3.1 schema is available at:

```text
docs/openapi.yaml
```

You can import it into tools like Postman, Insomnia, Stoplight, or SDK generators.

## Base URL

```text
http://127.0.0.1:5000
```

In production, replace the host and schema with the domain exposed by your reverse proxy.

## Authentication

All public endpoints `/api/v1/*` require an application API key:

```http
X-API-Key: <api-key>
```

API keys are configured from **Admin -> API Keys** (`/admin/api-keys`). Each key resolves to one RAGuardian user/workspace; queries, uploads, deletes, jobs and conversations are scoped to that workspace. The legacy `RAG_API_KEY` environment key is still supported as an admin/global compatibility key, but new integrations should use per-user keys.

Supported scopes:

| Scope | Allows |
|---|---|
| `query` | health, models, RAG queries, OCR extraction, conversation cleanup |
| `ingest` | PDF upload, audio upload, file deletion |
| `speech` | text-to-speech synthesis |

The `RAG_API_KEY` environment key has all scopes.

In multi-user deployments, create a dedicated user for each external integration when that integration needs its own knowledge boundary. For example, a WordPress public site can use a `website@example.com` RAGuardian user and one API key from that user's workspace. Grant that key `query` for chat, add `ingest` for article import/sync or audio upload, and add `speech` only for text-to-speech.

## Versioning

The current public prefix is:

```text
/api/v1
```

Legacy endpoints like `/ask`, `/models`, and `/upload` exist for internal UI and backward compatibility, but are not the recommended contract for new integrations.

## Errors

JSON errors follow this general form:

```json
{
  "error": "Readable message",
  "status": "validation_error",
  "field": "query"
}
```

`field` is present only when the error relates to a specific field.

Common statuses:

| HTTP | status | Meaning |
|---:|---|---|
| 400 | `validation_error` | Invalid payload or field out of range |
| 401 | `unauthorized` | Missing or invalid API key |
| 429 | `rate_limited` | Too many requests in the configured window |
| 500 | `server_error` | Internal error or provider unavailable |
| 500 | `model_configuration_error` | Missing provider/models file or no models |

## Rate Limit

Rate limiting is per client IP and configurable with:

```env
RATE_LIMIT_REQUESTS=10
RATE_LIMIT_WINDOW=60
```

When the limit is exceeded:

```json
{
  "error": "Rate limit exceeded",
  "retry_after": 42,
  "status": "rate_limited"
}
```

## GET /api/v1/health

Returns the service status.

The response is scoped to the workspace resolved by the API key.

### Request

```bash
curl http://127.0.0.1:5000/api/v1/health \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "status": "healthy",
  "model_configuration_ready": true,
  "settings_ready": true,
  "embeddings_ready": true,
  "cache_enabled": true,
  "database_ready": true,
  "tracked_files_count": 3,
  "indexed_files_count": 3,
  "stale_index_files_count": 0,
  "needs_rebuild": false,
  "system_ready": true,
  "stt_ready": true,
  "tts_ready": true,
  "voice_provider": "openai-compatible",
  "ocr_ready": false,
  "ocr_provider": "",
  "state_backend": "memory",
  "queue_backend": "inline",
  "redis_ready": true,
  "queue_ready": true,
  "queue_depth": 0,
  "active_jobs_count": 0,
  "collection": "documents",
  "documents_count": 128
}
```

`status` can be:

| Value | Meaning |
|---|---|
| `healthy` | Configuration, database, and embeddings are ready |
| `degraded` | Service is running, but a component is not ready |
| `unhealthy` | Invalid base configuration |

`system_ready` is `true` when model configuration, settings, database, index, and at least one Chroma chunk are ready for production-style use.

`state_backend` and `queue_backend` report whether runtime state and long-running work are using local process memory/inline execution or Redis-backed shared infrastructure. Redis is optional for local development, but required before production multi-worker deployments.

When `queue_backend` is `redis`, run an RQ worker with:

```bash
PYTHONPATH=app rq worker rag-default
```

## GET /api/v1/jobs/{job_id}

Returns the status of an async ingest or rebuild job.

Requires an API key with `ingest` scope.

Jobs are visible only to the workspace that created them.

### Request

```bash
curl http://127.0.0.1:5000/api/v1/jobs/$JOB_ID \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "id": "9cf3e6b9d7b34d3ebf4d6b2eaf3d6b5a",
  "type": "file_upload",
  "status": "completed",
  "message": "documento.pdf caricato e indicizzato",
  "processed": 1,
  "total": 1,
  "current_file": "",
  "filename": "documento.pdf",
  "errors": [],
  "result": {
    "message": "documento.pdf caricato e indicizzato",
    "filename": "documento.pdf",
    "source_type": "pdf",
    "chunks": 18
  },
  "started_at": 1782135600.0,
  "finished_at": 1782135603.4
}
```

`status` is typically `queued`, `running`, `completed`, or `failed`. When a job fails, `errors` and `result.error` contain the validation or processing error.

## GET /api/v1/models

Lists available models for built-in and custom enabled providers.

### Request

```bash
curl http://127.0.0.1:5000/api/v1/models \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "default_provider": "mistral",
  "default_model": "mistral-medium",
  "default_value": "mistral:mistral-medium",
  "models": [
    {
      "id": "mistral-medium",
      "name": "mistral-medium (Mistral AI)",
      "provider": "mistral",
      "provider_name": "Mistral AI",
      "value": "mistral:mistral-medium",
      "is_default": true
    }
  ]
}
```

Use `provider` and `id` in queries. `value` is a compact form useful for UI selects. `default_*` and `is_default` indicate the model configured as runtime default.

## POST /api/v1/query

Queries the RAG using indexed documents.

The query runs only against the Chroma collection and FileIndex of the workspace resolved by `X-API-Key`.

### Request JSON

| Field | Type | Required | Default | Constraints |
|---|---|---:|---|---|
| `query` | string | yes | | 3-2000 characters |
| `provider` | string | no | runtime configuration | Must exist in registry |
| `model` | string | no | default model | Must belong to provider |
| `conversation_id` | string | no | stateless | 8-80 characters; if present enables conversational memory |
| `client_context` | object | no | none | Safe site/page metadata used only in the prompt |
| `response_language` | string | no | `auto` | `auto` answers in the question language; `it` forces Italian; `en` forces English |
| `stream` | boolean | no | `false` | `true` enables streaming |
| `stream_format` | string | no | `text` | `text` or `ndjson`; used only with `stream: true` |
| `temperature` | number | no | runtime configuration | 0.0-1.0 |
| `k` | integer | no | runtime configuration | 1-50 |

To maintain a conversation, reuse the same `conversation_id` in subsequent requests. The history is used both to contextualize retrieval and in the final prompt; when it exceeds a fixed server-side threshold, older turns are compressed into a summary and the latest exchanges remain explicit. If `conversation_id` is not present, the request remains stateless.

Internally, conversation memory is namespaced by workspace. External clients keep using their own plain `conversation_id`; the API does not expose the internal namespace.

External clients can pass `client_context` to give the model non-secret page metadata. It is added to the prompt as client context only; it is not indexed and is not returned in `sources`.

`response_language` controls only the RAG answer prompt. Omit it or set `auto` to answer in the same language as the question, use `it` to force Italian, or `en` to force English. Audio transcription language is configured separately through the Voice provider and `/api/v1/audio` `language` field.

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{
    "query": "What are the main points of the document?",
    "provider": "mistral",
    "model": "mistral-medium",
    "conversation_id": "chat-20260616-demo",
    "response_language": "auto",
    "client_context": {
      "site_name": "Example Site",
      "page_title": "Pricing",
      "page_url": "https://example.com/pricing",
      "post_type": "page",
      "locale": "it_IT",
      "instructions": "The visitor is reading the pricing page."
    },
    "temperature": 0.3,
    "k": 5
  }'
```

### Response 200

```json
{
  "answer": "Response generated by the model.",
  "model": "mistral-medium",
  "provider": "mistral",
  "provider_name": "Mistral AI",
  "conversation_id": "chat-20260616-demo",
  "response_language": "auto",
  "context": [
    {
      "text": "Chunk retrieved from document...",
      "metadata": {
        "source": "app/uploads/workspaces/user_123/demo.pdf",
        "chunk_id": 0,
        "chunk_length": 924
      }
    }
  ],
  "sources": [
    {
      "filename": "demo.pdf",
      "source_type": "pdf",
      "chunk_id": 0,
      "snippet": "Chunk retrieved from document..."
    }
  ],
  "usage": null
}
```

`conversation_id` is returned only when present in the request. `sources` is the safe field for external clients: it never exposes local paths or admin download URLs. `usage` is reserved for future token/cost metrics when the provider exposes them uniformly.

### Streaming

For streaming compatible with existing clients, set only `stream: true`.

```bash
curl -N -X POST http://127.0.0.1:5000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{
    "query": "Summarize the document",
    "stream": true
  }'
```

The response is `text/plain` and sends progressive text chunks.

For structured streaming, useful for UIs that need to display the response token-by-token and sources at the end of generation, also use `stream_format: "ndjson"`.

```bash
curl -N -X POST http://127.0.0.1:5000/api/v1/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{
    "query": "Summarize the document",
    "stream": true,
    "stream_format": "ndjson"
  }'
```

The response is `application/x-ndjson`: one JSON line per event.

```json
{"type":"meta","model":"mistral-medium","provider":"mistral","provider_name":"Mistral AI","response_language":"auto","conversation_id":"chat-20260616-demo"}
{"type":"token","text":"Progressive "}
{"type":"token","text":"response..."}
{"type":"done","answer":"Progressive response...","model":"mistral-medium","provider":"mistral","provider_name":"Mistral AI","response_language":"auto","conversation_id":"chat-20260616-demo","context":[],"sources":[],"usage":null}
```

In case of error during streaming, the server sends an event:

```json
{"type":"error","error":"Error message","status":"server_error"}
```

## DELETE /api/v1/conversations/{conversation_id}

Deletes the conversational memory associated with a `conversation_id`.

```bash
curl -X DELETE http://127.0.0.1:5000/api/v1/conversations/chat-20260616-demo \
  -H "X-API-Key: $RAG_API_KEY"
```

Response:

```json
{
  "conversation_id": "chat-20260616-demo",
  "cleared": true
}
```

## POST /api/v1/files

Uploads and indexes a PDF, TXT, or Markdown document in the RAG knowledge base. By default the endpoint is synchronous for backward compatibility. Add `?async=true` to return immediately with a job id.

Requires an API key with `ingest` scope.

The uploaded file is stored under the API key owner's workspace upload directory and indexed into that workspace collection only.

When OCR is ready, scanned PDFs or PDFs whose parser returns no chunks are
processed through the configured OCR provider and then indexed as normal chunks.
Regolo OCR with `deepseek-ocr-2` is the default OCR provider and requires
`REGOLO_API_KEY`.

The ingestion fallback policy lives in `app/utils/ocr_policy.py` so deployments
can customize when OCR is attempted without changing the route handler.

If a document indexed with the same `source` already exists, previous chunks are deleted from ChromaDB before the new insertion.

Internal ChromaDB chunk IDs are deterministic and include both the `source` and content hash, in the form:

```text
<source_hash>:<document_hash>:chunk:<n>
```

This avoids collisions when the same content is uploaded with different names or paths.

### Request multipart/form-data

| Field | Type | Required | Notes |
|---|---|---:|---|
| `file` | file | yes | Supported extensions: `pdf`, `txt`, `md` |

Maximum size is configured with `MAX_UPLOAD_SIZE_MB`.

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/files \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@documento.pdf"
```

### Response 200

```json
{
  "message": "documento.pdf uploaded and indexed",
  "filename": "documento.pdf",
  "source_type": "pdf",
  "chunks": 18
}
```

### Async request

```bash
curl -X POST "http://127.0.0.1:5000/api/v1/files?async=true" \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@documento.pdf"
```

### Response 202

```json
{
  "job_id": "9cf3e6b9d7b34d3ebf4d6b2eaf3d6b5a",
  "id": "9cf3e6b9d7b34d3ebf4d6b2eaf3d6b5a",
  "type": "file_upload",
  "status": "queued",
  "message": "documento.pdf upload in elaborazione in coda",
  "processed": 0,
  "total": 1,
  "current_file": "documento.pdf",
  "filename": "documento.pdf",
  "errors": [],
  "result": null
}
```

After each successful upload, the retrieval cache is automatically cleared.

## POST /api/v1/audio

Uploads an audio file, transcribes it through the configured OpenAI-compatible STT provider, saves a transcript sidecar file, and indexes the transcript in the RAG knowledge base. The endpoint is synchronous by default for backward compatibility; add `?async=true` to process it as a job. STT language is empty by default for provider autodetection; it can be forced per request with multipart `language` or in Admin with an ISO language code such as `it`.

Requires an API key with `ingest` scope.

Supported extensions: `mp3`, `wav`, `m4a`, `webm`, `ogg`, `flac`.

### Request multipart/form-data

| Field | Type | Required | Notes |
|---|---|---:|---|
| `file` | file | yes | Audio file |
| `language` | string | no | Per-request STT language hint. If omitted, uses Admin `STT Language`; if both are empty, provider autodetects. |

Maximum size is configured with `MAX_AUDIO_UPLOAD_SIZE_MB` and defaults to `50`.

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/audio \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@meeting.mp3"
```

### Response 200

```json
{
  "message": "meeting.mp3 transcribed and indexed",
  "filename": "meeting.mp3",
  "source_type": "audio",
  "chunks": 4,
  "language_hint": "it",
  "transcript": "Transcript text..."
}
```

### Async request

```bash
curl -X POST "http://127.0.0.1:5000/api/v1/audio?async=true" \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@meeting.mp3" \
  -F "language=it"
```

### Response 202

```json
{
  "job_id": "5f42c5c36c154137ba3a1cb6b9b22f0b",
  "id": "5f42c5c36c154137ba3a1cb6b9b22f0b",
  "type": "audio_upload",
  "status": "queued",
  "message": "meeting.mp3 audio upload in elaborazione in coda",
  "processed": 0,
  "total": 1,
  "current_file": "meeting.mp3",
  "filename": "meeting.mp3",
  "errors": [],
  "result": null
}
```

## POST /api/v1/ocr

Extracts text from an uploaded image or PDF without indexing it.

Requires an API key with `query` scope. For PDFs, the service tries the normal PDF text parser first and uses the configured OCR provider only when no text is available. Image files always use OCR.

Supported extensions: `pdf`, `png`, `jpg`, `jpeg`, `webp`, `gif`, `bmp`, `tif`, `tiff`.

### Request multipart/form-data

| Field | Type | Required | Notes |
|---|---|---:|---|
| `file` | file | yes | Image or PDF file |

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ocr \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@scan.pdf"
```

### Response 200

```json
{
  "filename": "scan.pdf",
  "text": "Extracted text...",
  "method": "ocr",
  "ocr_used": true
}
```

`method` is `parsed` when a PDF already contains extractable text and `ocr` when the configured OCR provider was used.

## POST /api/v1/tts

Synthesizes speech from text through the configured OpenAI-compatible TTS provider.

Requires an API key with `speech` scope.

### Request JSON

| Field | Type | Required | Default | Constraints |
|---|---|---:|---|---|
| `text` | string | yes | | 1-4000 characters |
| `voice` | string | no | Admin default | Provider-specific voice ID |
| `format` | string | no | Admin default | `mp3`, `wav`, `opus`, `aac`, `flac` |

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/tts \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{"text": "Hello from the knowledge base."}' \
  --output answer.mp3
```

The response body is binary audio with a matching `Content-Type`.

## DELETE /api/v1/files/{filename}

Removes a file from the RAG knowledge base.

Deletion removes:

- ChromaDB chunks associated with the file's `source`
- Metadata row in `app/data/files.json`
- Uploaded file in `app/uploads`, if still present and safe to remove
- In-memory retrieval cache

Requires an API key with `ingest` scope.

### Request

```bash
curl -X DELETE http://127.0.0.1:5000/api/v1/files/documento.pdf \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "message": "documento.pdf removed from knowledge base",
  "filename": "documento.pdf",
  "source": "app/uploads/workspaces/user_123/documento.pdf",
  "chunks_deleted": 18,
  "file_deleted": true
}
```

If the file is not registered in `files.json`, the response is `404` with `status: "not_found"`.

## Provider Configuration

Providers/models distributed with the project are defined in:

```text
app/default_providers.json
```

The file is the source of truth for built-in provider IDs, model lists, endpoint
URLs, and API key environment variables. OpenAI-compatible providers can be added
there without Python code changes; restart the server after editing it.

The distributed file includes Regolo and Mistral examples, which require their
own API keys:

```env
MISTRAL_API_KEY=...
REGOLO_API_KEY=...
```

Runtime/custom OpenAI-compatible LLM, Embedding, ReRanking, Voice, and OCR
providers are configured from `/admin/config`. Regolo OCR with `deepseek-ocr-2`
is distributed as the default OCR provider; add custom OCR providers in Admin
when you need alternatives.

## Integration Checklist

1. Create an application API key from `/admin/api-keys`.
2. Verify `GET /api/v1/health`.
3. Read models with `GET /api/v1/models`.
4. Upload at least one PDF via `/admin/files` or `POST /api/v1/files`; upload audio via `POST /api/v1/audio` when STT is configured.
5. Set `REGOLO_API_KEY` or configure another OCR provider when scanned PDFs or image-to-text chat input are required.
6. Query `POST /api/v1/query`.
