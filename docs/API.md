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
| `kb_manage` | knowledge-base catalog creation, update, and deletion |
| `agent_manage` | chat-agent create, update, and delete; read with `query` |

Each user key also has a `knowledge_base_ids` allowlist. The legacy
`RAG_API_KEY` environment key is default-only and has no `kb_manage` or
`agent_manage` scope.

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
| 404 | `knowledge_base_not_found` | Missing, foreign, or unauthorized KB |
| 409 | `knowledge_base_deleting` | KB is unavailable for new work |
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

Append `?knowledge_base_id=kb_...` for an authorized secondary KB. Omitting
the selector preserves the legacy default behavior.

## Knowledge bases

`GET/POST /api/v1/knowledge-bases` and
`GET/PATCH/DELETE /api/v1/knowledge-bases/{knowledge_base_id}` expose the
catalog. List/get accept `query`, `ingest`, or `kb_manage`; mutations require
`kb_manage`. DELETE returns a `delete_knowledge_base` job. An authorized
default target returns 409, while an unauthorized target (including default)
is hidden with 404. Poll deletion jobs through `/api/v1/jobs/{job_id}`; the
initiating API key can continue polling with `kb_manage` after target cleanup
removes that knowledge base from its allowlist. Other keys cannot poll that
deletion job, even when they have `kb_manage` in the same workspace.

Query operations use `knowledge_base_ids` in the JSON body to select up to
`RAG_MAX_QUERY_KNOWLEDGE_BASES` active KBs (default 5). Ingestion and
administrative operations remain single-KB and accept `knowledge_base_id`.
Conversation clear accepts repeated `knowledge_base_ids` query parameters.
Jobs freeze the target at creation.
For query operations, omitting the selector means `["default"]`; an explicitly
null or empty selector is invalid and returns 400. Malformed IDs return 400;
missing or unauthorized IDs return 404 without fallback.

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
  "knowledge_base_id": "default",
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

Requires an API key with `ingest` scope. Knowledge-base deletion jobs instead
require `kb_manage` and are visible only to the API key that initiated DELETE.

Ingest jobs are visible only to their workspace and current target allowlist.

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

Queries the RAG using documents from one or more knowledge bases.

The service validates the entire selection, embeds the question once, retrieves
from each collection concurrently, deduplicates identical chunks, and performs
one global ranking. A failure or unauthorized KB fails the whole request.

### Request JSON

| Field | Type | Required | Default | Constraints |
|---|---|---:|---|---|
| `query` | string | yes | | 3-2000 characters |
| `agent_id` | string | no | | ID of a chat agent; resolves provider, model, KBs, and prompt server-side |
| `provider` | string | no | runtime configuration | Must exist in registry; mutually exclusive with `agent_id` |
| `model` | string | no | default model | Must belong to provider; mutually exclusive with `agent_id` |
| `conversation_id` | string | no | stateless | 8-80 characters; if present enables conversational memory |
| `knowledge_base_ids` | string[] | no | `["default"]` | 1 to `RAG_MAX_QUERY_KNOWLEDGE_BASES` unique, authorized active KB IDs; mutually exclusive with `agent_id` |
| `system_prompt_id` | string | no | active/default prompt | ID of a saved system prompt; mutually exclusive with `agent_id` |
| `client_context` | object | no | none | Safe site/page metadata used only in the prompt |
| `response_language` | string | no | `auto` | `auto` answers in the question language; `it` forces Italian; `en` forces English |
| `stream` | boolean | no | `false` | `true` enables streaming |
| `stream_format` | string | no | `text` | `text` or `ndjson`; used only with `stream: true` |
| `temperature` | number | no | runtime configuration | 0.0-1.0 |
| `k` | integer | no | runtime configuration | 1-50 |
| `persist_history` | boolean | no | `true` | When `true` and `turn_id` is present, persists the turn to the durable per-workspace history store; when `false` the turn stays in warm memory only |
| `turn_id` | string | no | | Stable id for the turn; reuse it on retries to get replayed results without regeneration. Pattern `^[A-Za-z0-9][A-Za-z0-9_-]*$`, max 80 chars. Required for `persist_history` |
| `parent_turn_id` | string | no | | `turn_id` of the previous visible turn; enforces a linear chain. `null` for the first turn |

When `agent_id` is present, the server resolves the agent's `provider_id`,
`model_id`, `knowledge_base_ids`, and `prompt_ref`. Sending any explicit
`provider`, `model`, `knowledge_base_ids`, `knowledge_base_id`,
`system_prompt_id`, or `system_prompt_scope` together with `agent_id` returns
`400` with `status: "agent_conflicting_params"`. The API key must have `query`
scope and its KB allowlist must cover every knowledge base linked to the agent;
otherwise the response is `400` with `status: "chat_agent_not_found"`. The
response includes `agent_id` and `agent_name` when an agent was used.

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
    "knowledge_base_ids": [
      "default",
      "kb_11111111111111111111111111111111"
    ],
    "system_prompt_id": "support",
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
  "agent_id": "agent_abcdef1234567890abcdef1234567890",
  "agent_name": "Support Agent",
  "conversation_id": "chat-20260616-demo",
  "knowledge_base_ids": [
    "default",
    "kb_11111111111111111111111111111111"
  ],
  "response_language": "auto",
  "context": [
    {
      "text": "Chunk retrieved from document...",
      "metadata": {
        "source": "app/uploads/workspaces/user_123/demo.pdf",
        "chunk_id": 0,
        "knowledge_base_id": "default",
        "knowledge_base_name": "General",
        "knowledge_base_origins": [
          {
            "knowledge_base_id": "default",
            "knowledge_base_name": "General",
            "source": "app/uploads/workspaces/user_123/demo.pdf"
          }
        ],
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
  "usage": null,
  "history_status": "saved",
  "history_saved": true
}
```

`conversation_id` is returned only when present in the request. `sources` is the safe field for external clients: it never exposes local paths or admin download URLs. `usage` is reserved for future token/cost metrics when the provider exposes them uniformly.

When `persist_history` is `true` (the default), the response carries two extra fields describing the durable history outcome:

| Field | Type | Values | Meaning |
|---|---|---|---|
| `history_status` | string | `saved`, `not_requested`, `disabled`, `client_turn_id_required`, `error` | Outcome of the durable turn. `saved` = persisted; `not_requested` = `persist_history` was false; `disabled` = history feature flag off; `client_turn_id_required` = `turn_id` missing, so memory was updated but nothing persisted; `error` = persistence failed |
| `history_saved` | boolean | `true`/`false` | Convenience boolean: `true` only when `history_status` is `saved` |

A retry that sends the same `turn_id` with an identical request fingerprint returns the previously persisted result with `replayed: true` (NDJSON `meta`/`done` events) without calling the provider again. A retry with the same `turn_id` but a different fingerprint returns `409 turn_id_conflict`.

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
{"type":"meta","model":"mistral-medium","provider":"mistral","provider_name":"Mistral AI","response_language":"auto","conversation_id":"chat-20260616-demo","knowledge_base_ids":["default"],"knowledge_base_id":"default"}
{"type":"token","text":"Progressive "}
{"type":"token","text":"response..."}
{"type":"done","answer":"Progressive response...","model":"mistral-medium","provider":"mistral","provider_name":"Mistral AI","response_language":"auto","conversation_id":"chat-20260616-demo","knowledge_base_ids":["default"],"knowledge_base_id":"default","context":[],"sources":[],"usage":null}
```

In case of error during streaming, the server sends an event:

```json
{"type":"error","error":"Error message","status":"server_error"}
```

## DELETE /api/v1/conversations/{conversation_id}

Deletes the conversational memory associated with a `conversation_id`.
Repeat `knowledge_base_ids` once for every KB in the plural selection. A
missing or unauthorized KB returns 404; a KB being deleted or in
`delete_failed` returns 409.

```bash
curl -X DELETE 'http://127.0.0.1:5000/api/v1/conversations/chat-20260616-demo?knowledge_base_ids=default&knowledge_base_ids=kb_11111111111111111111111111111111' \
  -H "X-API-Key: $RAG_API_KEY"
```

Response:

```json
{
  "conversation_id": "chat-20260616-demo",
  "cleared": true,
  "knowledge_base_ids": [
    "default",
    "kb_11111111111111111111111111111111"
  ]
}
```

## Persistent Conversation History (session-auth)

When `RAG_CONVERSATION_HISTORY_ENABLED=1`, completed turns are persisted to a
per-workspace SQLite database (`<WORKSPACE_DATA_DIR>/<workspace_id>/conversations.db`)
so they survive restarts and warm-memory TTL expiry. The history is isolated
per workspace and never crosses workspace boundaries.

The management endpoints below are **session-authenticated** (browser login
cookie/CSRF), not API-key authenticated, and back the in-app history drawer.
They live under `/api/conversations` (no `/v1` prefix). Enable the feature with
`RAG_CONVERSATION_HISTORY_ENABLED=1`; the `.env.example` documents retention,
quota, lease, and pending-turn tuning knobs.

### GET /api/conversations

Lists conversations for the current workspace with pagination. Conversations
with zero messages are hidden.

| Query param | Type | Default | Constraints |
|---|---|---:|---|
| `page` | integer | `1` | >= 1 |
| `per_page` | integer | `20` | 1-100 |
| `status` | string | (all) | `active` or `archived` |
| `archived` | string | (all) | `true`/`false`; sets `status` when `status` is omitted |

Response:

```json
{
  "conversations": [
    {
      "id": "a1b2c3d4-...",
      "client_conversation_id": "chat-20260616-demo",
      "scope_kind": "default",
      "title": "Summarize my indexed documents",
      "status": "active",
      "agent_id": "",
      "agent_name": "",
      "provider_id": "mistral",
      "model_id": "mistral-medium",
      "message_count": 4,
      "payload_bytes": 12345,
      "has_incomplete_turn": false,
      "created_at": 1754294400.0,
      "updated_at": 1754294500.0,
      "archived_at": null
    }
  ],
  "pagination": {
    "page": 1,
    "per_page": 20,
    "total": 1,
    "page_count": 1,
    "has_prev": false,
    "has_next": false
  }
}
```

When history is disabled, `conversations` is always empty and `total` is `0`.

### GET /api/conversations/{history_id}

Returns the full conversation record by its server-side UUID `history_id`.
Returns `404` when the id is not a UUID, history is disabled, or the
conversation belongs to another workspace.

### GET /api/conversations/{history_id}/messages

Returns messages for a conversation with backward cursor pagination. Messages
are returned oldest-first within the requested window.

| Query param | Type | Default | Constraints |
|---|---|---:|---|
| `before_sequence` | integer | (latest) | >= 0; fetch messages with `sequence <` this value |
| `limit` | integer | `50` | 1-200 |

Response:

```json
{
  "messages": [
    {
      "id": 1,
      "conversation_id": "a1b2c3d4-...",
      "turn_id": "turn_001",
      "role": "user",
      "message_type": "text",
      "content": "Summarize my indexed documents",
      "sequence": 1,
      "sources": [],
      "metadata": {},
      "payload_bytes": 42,
      "created_at": 1754294400.0
    }
  ],
  "next_cursor": null,
  "limit": 50
}
```

`next_cursor` is the `sequence` of the oldest message in the current window, to
pass back as `before_sequence` for the previous page. It is `null` when there
are no older messages.

### PATCH /api/conversations/{history_id}

Renames and/or archives/unarchives a conversation. Send one or both fields.

```json
{
  "title": "New title (max 120 chars)",
  "archived": true
}
```

- `title`: non-empty string, max 120 characters.
- `archived`: boolean; `true` archives, `false` unarchives.

Returns the updated conversation record, or `404` if not found. `400` on
validation errors.

### DELETE /api/conversations/{history_id}

Hard-deletes a conversation and all its messages and turn records. Returns
`{"deleted": true}` on success, `404` if the conversation does not exist. When
history is disabled, returns `{"deleted": false}`.

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
| `knowledge_base_id` | string | no | Omit for default; otherwise an authorized `kb_...` ID |
| `relative_path` | string | no | Safe workspace-relative source path, useful for folders and integrations |

Maximum size is configured with `MAX_UPLOAD_SIZE_MB`.

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/files \
  -H "X-API-Key: $RAG_API_KEY" \
  -F "file=@documento.pdf" \
  -F "relative_path=manuali/documento.pdf" \
  -F "knowledge_base_id=kb_11111111111111111111111111111111"
```

### Response 200

```json
{
  "message": "documento.pdf uploaded and indexed",
  "filename": "documento.pdf",
  "source_type": "pdf",
  "chunks": 18,
  "knowledge_base_id": "kb_11111111111111111111111111111111"
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
curl -X DELETE 'http://127.0.0.1:5000/api/v1/files/manuali/documento.pdf?knowledge_base_id=kb_11111111111111111111111111111111' \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "message": "documento.pdf removed from knowledge base",
  "filename": "documento.pdf",
  "source": "app/uploads/workspaces/user_123/documento.pdf",
  "chunks_deleted": 18,
  "file_deleted": true,
  "knowledge_base_id": "kb_11111111111111111111111111111111"
}
```

If the file is not registered in `files.json`, the response is `404` with `status: "not_found"`.

## Chat Agents

Chat agents are named presets that bundle a provider, model, knowledge bases,
and an optional prompt reference. External clients can query by `agent_id`
instead of specifying each parameter individually.

### Scopes

| Operation | Required scope |
|---|---|
| List, get, options | `query` or `agent_manage` |
| Create, update, delete | `agent_manage` |

### KB-grant filtering

List, get, update, delete, and query-by-`agent_id` enforce the API key's
`knowledge_base_ids` allowlist: only agents whose `knowledge_base_ids` are a
subset of the key's allowed KBs are visible. An agent referencing a KB outside
the key's allowlist is hidden with `404`.

## GET /api/v1/agents

Lists agents visible to the API key.

### Request

```bash
curl http://127.0.0.1:5000/api/v1/agents \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "agents": [
    {
      "id": "agent_abcdef1234567890abcdef1234567890",
      "name": "Support Agent",
      "description": "Customer support",
      "provider_id": "regolo",
      "model_id": "gpt-oss-120b",
      "knowledge_base_ids": ["default"],
      "prompt_ref": {
        "id": "prompt-uuid",
        "scope": "shared"
      },
      "created_at": "2026-08-01T10:00:00+00:00",
      "updated_at": "2026-08-01T10:00:00+00:00",
      "available": true,
      "issues": []
    }
  ],
  "limits": {
    "max_chat_agents": 20,
    "max_query_knowledge_bases": 5
  },
  "capabilities": {
    "can_manage": false
  }
}
```

Each agent carries an `available` flag and an `issues` array describing why
it cannot currently run (missing model, inactive/missing KB, a selected prompt
that is missing or inactive, or KB count above the limit).

## GET /api/v1/agents/{agent_id}

Returns a single agent by ID.

### Response 200

Same shape as one element of the `agents` array above.

### Errors

| HTTP | status | Meaning |
|---:|---|---|
| 404 | `chat_agent_not_found` | Agent does not exist or KB grant mismatch |

## GET /api/v1/agents/options

Returns the models, active knowledge bases, personal/shared prompt metadata,
capabilities, and limits available to
the API key. Useful for building agent creation/edit forms in external clients.

### Request

```bash
curl http://127.0.0.1:5000/api/v1/agents/options \
  -H "X-API-Key: $RAG_API_KEY"
```

### Response 200

```json
{
  "models": [
    {
      "id": "gpt-oss-120b",
      "name": "gpt-oss-120b (Regolo)",
      "provider": "regolo",
      "provider_name": "Regolo",
      "value": "regolo:gpt-oss-120b",
      "is_default": true
    }
  ],
  "default_provider": "regolo",
  "default_model": "gpt-oss-120b",
  "knowledge_bases": [
    {
      "id": "default",
      "name": "General",
      "description": "",
      "is_default": true,
      "status": "active",
      "created_at": "2026-08-01T10:00:00+00:00",
      "updated_at": "2026-08-01T10:00:00+00:00",
      "stats": {
        "tracked_files": 0,
        "indexed_files": 0,
        "chunks": 0,
        "data_sources": 0
      }
    }
  ],
  "prompts": [
    {
      "id": "prompt-uuid",
      "name": "Agent system prompt",
      "scope": "shared",
      "is_active": true
    }
  ],
  "capabilities": {
    "can_manage": false
  },
  "limits": {
    "max_chat_agents": 20,
    "max_query_knowledge_bases": 5
  }
}
```

## POST /api/v1/agents

Creates a new agent. Requires `agent_manage` scope.

### Request JSON

| Field | Type | Required | Constraints |
|---|---|---:|---|
| `name` | string | yes | 1-120 characters |
| `description` | string | no | Max 500 characters |
| `provider_id` | string | yes | Must exist in registry |
| `model_id` | string | yes | Must belong to provider |
| `knowledge_base_ids` | string[] | yes | 1 to `max_query_knowledge_bases` authorized active KB IDs |
| `prompt_ref` | object or null | no | `{"id": "prompt-uuid", "scope": "shared"}`; omit, use `null`, or `{}` for no prompt |

When no prompt is selected, the response always normalizes `prompt_ref` to an
empty object.

### Request

```bash
curl -X POST http://127.0.0.1:5000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RAG_API_KEY" \
  -d '{
    "name": "Support Agent",
    "description": "Customer support",
    "provider_id": "regolo",
    "model_id": "gpt-oss-120b",
    "knowledge_base_ids": ["default"],
    "prompt_ref": {
      "id": "prompt-uuid",
      "scope": "shared"
    }
  }'
```

### Response 201

```json
{
  "id": "agent_abcdef1234567890abcdef1234567890",
  "name": "Support Agent",
  "description": "Customer support",
  "provider_id": "regolo",
  "model_id": "gpt-oss-120b",
  "knowledge_base_ids": ["default"],
  "prompt_ref": {
    "id": "prompt-uuid",
    "scope": "shared"
  },
  "created_at": "2026-08-01T10:00:00+00:00",
  "updated_at": "2026-08-01T10:00:00+00:00",
  "available": true,
  "issues": []
}
```

### Errors

| HTTP | status | Meaning |
|---:|---|---|
| 400 | `invalid_prompt_ref` | `prompt_ref` was provided but is malformed or incomplete |
| 400 | `invalid_chat_agent_name` | Name empty or too long |
| 400 | `model_unavailable` / `knowledge_base_missing` / `knowledge_base_inactive` / `prompt_missing` / `prompt_inactive` | Referenced resource no longer usable |
| 403 | `forbidden` | Key lacks `agent_manage` scope |
| 404 | `knowledge_base_not_found` | Referenced KB not in key allowlist |
| 409 | `knowledge_base_limit_exceeded` | Too many KBs |
| 409 | `chat_agent_limit_reached` | Workspace reached `max_chat_agents` |
| 409 | `duplicate_chat_agent_name` | Another agent already uses that name |

## PATCH /api/v1/agents/{agent_id}

Updates an agent. All fields are optional; only provided fields are changed.
Requires `agent_manage` scope.

### Request JSON

| Field | Type | Notes |
|---|---|---:|
| `name` | string | 1-120 characters |
| `description` | string | Max 500 characters |
| `provider_id` | string | Must exist in registry |
| `model_id` | string | Must belong to provider |
| `knowledge_base_ids` | string[] | 1 to `max_query_knowledge_bases` authorized active KB IDs |
| `prompt_ref` | object or null | `{"id": "prompt-uuid", "scope": "shared"}`; send `null` or `{}` to clear it |

### Response 200

Same shape as the create response.

### Errors

| HTTP | status | Meaning |
|---:|---|---|
| 400 | `model_unavailable` / `knowledge_base_missing` / `knowledge_base_inactive` / `prompt_missing` / `prompt_inactive` | Updated references no longer usable |
| 403 | `forbidden` | Key lacks `agent_manage` scope |
| 404 | `chat_agent_not_found` | Agent does not exist or KB grant mismatch |
| 404 | `knowledge_base_not_found` | Referenced KB not in key allowlist |
| 409 | `knowledge_base_limit_exceeded` | Too many KBs |
| 409 | `duplicate_chat_agent_name` | Another agent already uses that name |

## DELETE /api/v1/agents/{agent_id}

Deletes an agent. Requires `agent_manage` scope.

### Response 200

```json
{
  "ok": true
}
```

### Errors

| HTTP | status | Meaning |
|---:|---|---|
| 404 | `chat_agent_not_found` | Agent does not exist or KB grant mismatch |
| 403 | `forbidden` | Key lacks `agent_manage` scope |

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
7. Optionally create chat agents with `POST /api/v1/agents` and query by `agent_id`.
