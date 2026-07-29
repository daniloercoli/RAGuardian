# Multi-User Architecture

RAGuardian is designed for personal RAG in a shared deployment. A company can run one application instance while each user gets an isolated workspace containing multiple knowledge bases.

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
app/data/workspaces/<workspace_id>/knowledge_bases.json
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
- `kb_manage`

When a request uses `X-API-Key`, RAGuardian resolves the key to its owner and routes query/upload/delete/job operations to that workspace only.

`RAG_API_KEY` remains a default-only compatibility key without `kb_manage`. Prefer user keys for integrations.

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

## Multiple Knowledge Bases

Each workspace starts with a `default` KB. It is renameable but cannot be
deleted. Up to `RAG_MAX_KNOWLEDGE_BASES` additional KBs may be created
(default: 20). The default retains the legacy file index, upload root, and
`documents_<workspace_id>` collection without reindexing.

Additional KBs use isolated `knowledge_bases/<kb_id>/files.json`,
`__knowledge_bases__/<kb_id>/` upload roots, and deterministic hashed Chroma
collections. `KnowledgeBaseContext` includes `KNOWLEDGE_BASE_ID`,
`KNOWLEDGE_BASE_NAME`, and `WORKSPACE_UPLOAD_FOLDER` alongside the existing
runtime fields.

API keys contain a `knowledge_base_ids` allowlist. Existing keys migrate to
`["default"]`; newly created KBs are not added to unrelated keys. A well-formed
but unauthorized KB returns 404.

Default conversation memory remains
`<workspace_id>:<conversation_id>`. Secondary KBs use
`<workspace_id>:kb:<knowledge_base_id>:<conversation_id>`. Jobs and locks also
carry the KB ID. Secondary deletion is an asynchronous cascade with
`deleting` and retryable `delete_failed` states.

## Migration and rollout

Existing workspaces keep their legacy paths and Chroma collections. Run a
verified full backup first, then inspect the idempotent migration report:

```bash
python3 scripts/migrate_knowledge_bases.py --dry-run
python3 scripts/migrate_knowledge_bases.py --apply
```

Use `--workspace-root`, `--users-file`, and `--max-additional` when the
deployment uses non-default paths. The script creates only the `default`
catalog record, marks legacy data sources and API-key allowlists as default,
and never moves files, copies collections, or regenerates embeddings. A
corrupt catalog is reported and left untouched.

Backups include the KB catalog schema, per-workspace KB inventory, storage
presence, collection presence, and document/chunk counts. Restore validates
the catalog and KB storage after the atomic swap and rolls all components back
if validation fails. Live KB deletion is permanent; recovery requires a full
backup restore.
