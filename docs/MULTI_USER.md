# Multi-User Architecture

RAGuardian is designed for personal RAG in a shared deployment. A company can run one application instance while each user gets an isolated knowledge base.

## Auth Model

Local users live in `app/data/users.json` through `UserStore`.

User fields:

- `id`
- `email`
- `display_name`
- `password_hash`
- `role`: `admin` or `user`
- `enabled`
- `created_at`
- `updated_at`

Flask sessions store `session["user_id"]`. API requests use `X-API-Key`; the key resolves to one user and therefore one workspace.

## Workspaces

Each user has:

```text
workspace_id = user_id
```

Workspace paths:

```text
app/data/workspaces/<workspace_id>/settings.json
app/data/workspaces/<workspace_id>/files.json
app/uploads/workspaces/<workspace_id>/
```

Global `app/data/settings.json` remains for provider/model/admin policy. Workspace settings hold per-user API keys and data sources.

| Area | Storage | Who Manages It |
|---|---|---|
| LLM, embeddings, reranker, voice, OCR providers | Global settings | Admins |
| Default model and indexing policy | Global settings copied into new workspaces | Admins |
| API keys | Workspace settings | The current workspace owner |
| Uploaded files, FileIndex, data sources, conversations | Workspace paths/settings | The current workspace owner |

## RAG Isolation

Every RAG operation receives or resolves a `WorkspaceContext`:

- `SETTINGS_FILE`
- `FILE_INDEX`
- `UPLOAD_FOLDER`
- `CHROMA_COLLECTION`
- `USER_ID`
- `WORKSPACE_ID`

Chroma collection naming:

```text
documents_<workspace_id>
```

This avoids relying on metadata filters for security boundaries.

## API Key Ownership

API keys are stored in workspace settings. A key can have scopes:

- `query`
- `ingest`
- `speech`

When a request uses `X-API-Key`, RAGuardian resolves the key to its owner and routes query/upload/delete/job operations to that workspace only.

`RAG_API_KEY` is still supported as a legacy/admin environment key with all scopes. Prefer workspace keys for integrations.

## Conversation Memory

Conversation IDs are namespaced internally:

```text
<workspace_id>:<conversation_id>
```

The public API still returns the original `conversation_id` supplied by the client.

## Jobs

Upload, rebuild, and data source sync jobs store:

- `user_id`
- `workspace_id`
- job type
- progress
- errors
- result

Status endpoints hide jobs from other workspaces.

## Admin vs User UI

Admins can:

- configure global providers and policies;
- create, disable, update users;
- inspect their own workspace from normal user pages.

Users can:

- use Chat;
- upload and manage their own RAG files;
- configure and sync their own data sources;
- use their own API keys.
