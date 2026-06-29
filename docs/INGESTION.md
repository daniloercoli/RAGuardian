# Data Ingestion Plugins

RAGuardian uses an in-repo allowlist plugin system for external data sources. V1 does not load arbitrary code from untrusted directories.

## Plugin Contract

An ingester exposes:

- `plugin_id`
- `display_name`
- `validate_config(config)`
- `sync(context, source_config, cursor) -> SyncResult`

Each produced item can include:

- `content`
- `content_bytes`
- `filename`
- `extension`
- `remote_id`
- `updated_at`
- `metadata`
- `attachments`

## Available Plugins

| Plugin | ID | Notes |
|---|---|---|
| Email IMAP | `email_imap` | Standard-library IMAP and email parsing |
| Microsoft Drive | `microsoft_drive` | Microsoft Graph for OneDrive/SharePoint files |
| Folder Watch | `folder_watch` | Local/UNC folder file monitoring |

## Storage Model

External data is materialized as snapshots inside the owning user's workspace:

```text
app/uploads/workspaces/<workspace_id>/external/<data_source_id>/
```

RAGuardian indexes those snapshots through the same document indexer used by manual uploads.

Stable filenames use:

```text
<source_id>__<remote_hash>__<slug>.<ext>
```

This keeps `FileIndex` compatible while avoiding collisions.

## Metadata

Common metadata includes:

- `source_type`
- `ingestion_plugin`
- `data_source_id`
- `remote_id`
- `remote_url`
- `remote_updated_at`
- email: `subject`, `sender`, `recipients`, `thread_id`, `message_id`, `attachment_id`
- Drive: `drive_id`, `item_id`, `parent_id`, `etag`, `mime_type`, `size`

## Secrets

Workspace settings never store plaintext connector passwords or tokens.

Supported modes:

- `user_secret`: encrypted in `SecretStore`;
- `env_ref`: resolved from an admin-managed environment variable.

Set a stable production key:

```env
RAG_SECRET_KEY=replace-with-a-long-random-secret
```

## Email IMAP

Configuration fields:

- `host`
- `port`
- `use_ssl`
- `username`
- `password` as encrypted user secret or `password_env`
- `folder`
- `from_contains`
- `subject_contains`
- `since`
- `max_messages`
- `include_body`
- `include_attachments`

Each email body becomes a Markdown snapshot. Supported attachments are indexed as separate documents linked by metadata.

## Microsoft Drive

Configuration fields:

- `token` as encrypted user secret or `token_env`
- `drive_id`
- `folder_path` or `item_id`
- `recursive`
- `include_extensions`
- `max_files`
- `max_file_size_mb`

Supported file types in the MVP: PDF, TXT, MD, CSV.

## Folder Watch

Monitor a local or UNC folder for files to index. Files are copied to the workspace and indexed on sync.

Configuration fields:

- `folder_path` - Absolute path to the folder (required)
- `recursive` - Scan subfolders (default: false)
- `include_extensions` - Comma-separated list of extensions (default: `pdf,txt,md,csv`)
- `exclude_patterns` - Comma-separated glob patterns to skip (e.g., `*.tmp,*~`)
- `max_files` - Maximum files to process per sync (1-500, default: 100)

Supported file types: PDF, TXT, MD, CSV. Text files are read directly; binary files (PDF) are copied and processed by the document indexer.

Example configuration:
```
Folder Path: C:\Documents\Contracts
Extensions: pdf,txt,md
Exclude: *.tmp,*~,draft*
Max Files: 200
Recursive: ✓
```

**Note:** The folder must exist and be accessible by the application. Paths must be absolute (no relative paths).

## Triggering Sync

Manual sync is always available:

- UI: **Data Sources -> Sync now**
- Route: `POST /admin/data-sources/<id>/sync`

When Redis queueing is enabled, sync runs as an RQ job. Otherwise it runs in the existing inline/thread fallback used by uploads and rebuilds.

## Periodic Polling

Periodic sync is handled by a dedicated poller process. The web app does not run an internal scheduler, so Gunicorn workers do not duplicate scheduled work.

Enable periodic sync on a data source with:

```json
{
  "sync_enabled": true,
  "sync_interval_seconds": 900
}
```

The admin UI exposes the same fields as **Periodic sync** and **Sync interval minutes**.

Run Redis-backed queueing plus one RQ worker:

```bash
PYTHONPATH=app rq worker rag-default
```

Run the poller:

```bash
PYTHONPATH=app python -m utils.data_ingestion.poller
```

Useful options:

```bash
PYTHONPATH=app python -m utils.data_ingestion.poller --once
PYTHONPATH=app python -m utils.data_ingestion.poller --interval-seconds 60
```

The poller only enqueues sources that are due. A per-workspace/per-source active job lock prevents duplicate syncs if the poller overlaps with a manual **Sync now** or another poller replica.
