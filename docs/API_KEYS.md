# API Key Management

API keys provide per-user authentication for API access to the RAG service via the `X-API-Key` header. Keys support expiration, rotation, scope-based access, and usage tracking.

## Quick Reference

| Feature | Description |
|---|---|
| **Creation** | Via admin UI at `/admin/api-keys` |
| **Expiration** | Set TTL as `Nd`, `Nh`, or `Nm` (e.g. `7d`, `48h`, `30m`) with second-level UTC precision |
| **Rotation** | Replace key value while preserving name, scopes, and expiration |
| **Renaming** | Update key label without affecting value or scopes |
| **Scopes** | Restrict to `query`, `ingest`, `speech` (any combination) |
| **Usage** | Logged to `app/data/api_keys_usage.json` with retention |

## Creating a Key

Navigate to **Admin API Keys** → **Create API Key** form:

| Field | Required | Format | Description |
|---|---|---|---|
| User | Yes | User ID | The owner of the key |
| Name | Yes | Arbitrary string | Label for the key (e.g. `production`, `testing`) |
| Description | No | Free text | Human-readable purpose |
| Expires In | No | `N[dhm]` | TTL (e.g. `7d`, `24h`, `60m`). Leave blank for never |
| Scopes | Yes | Checkboxes | `query`, `ingest`, `speech` |
| Status | Yes | Select | `Enabled` or `Disabled` |

The raw value is displayed once, immediately after creation. Copy it at that point: the server stores only a SHA-256 hash plus the masked prefix/suffix and cannot reveal the key again.

## Expiration

Keys can be set to expire after a duration. The expiration timestamp is stored as an ISO-8601 UTC timestamp.

- **Setting expiration**: Enter `7d`, `24h`, or `30m` in the Expires In field.
- **No expiration**: Leave the field blank.
- **Expiration behavior**: Once the key's expiration timestamp passes, all requests using that key are rejected immediately.
- **Viewing expiration**: The "Expires" column shows the expiration timestamp or "never".

## Rotating a Key

Rotation replaces the raw key value while preserving its name, scopes, expiration, and owner. The old value is invalidated immediately.

```python
from utils.user_store import UserStore

store = UserStore("app/data/users.json")
new_key = store.rotate_api_key(user_id="user-123", key_name="production")
# new_key contains the new raw value
```

**Important**: Update any integration or client that uses the old key value with the new value after rotation.

## Renaming a Key

Rename updates the label without affecting the raw key value or scopes:

```python
store.update_api_key_name(user_id="user-123", key_name="old_name", new_name="new_name")
```

## Updating Scopes

Change which endpoints a key can access:

```python
store.update_api_key_scopes(user_id="user-123", key_name="production", scopes=["query", "ingest"])
```

Valid scopes:

| Scope | Grants access to |
|---|---|
| `query` | `/ask`, `/api/v1/query`, and query endpoints |
| `ingest` | `/upload`, `/api/v1/upload`, `/api/v1/files`, file ingestion |
| `speech` | `POST /api/v1/tts` text-to-speech synthesis |

An integration normally needs one key with the required combination of scopes. For example, the WordPress plugin can run chat with `query`, article import/sync and audio upload with `ingest`, and the optional TTS button with `speech`.

## Disabling and Deleting

### Disable

Disabled keys are rejected for all requests but retained for auditing. Enable a key again from the admin UI via the Toggle action.

### Delete

Delete permanently removes the key. A confirmation dialog prevents accidental deletion. After deletion the key is immediately rejected for all requests.

## Usage Logging

Every request using a valid API key is logged to `app/data/api_keys_usage.json`:

```json
{
  "logging_enabled": true,
  "log_entries": [
    {
      "id": "b3c7f6f9b12847ad8f1b8c6c96e9c111",
      "timestamp": "2025-01-15T10:30:00+00:00",
      "date_bucket": "2025-01-15",
      "user_id": "user-123",
      "api_key_id": "key-id",
      "api_key_name": "production",
      "key_name": "production",
      "scopes_used": ["query"],
      "endpoint": "/api/v1/query",
      "method": "POST",
      "response_code": 200,
      "status_code": 200,
      "duration_ms": 42,
      "request_id": "request-id",
      "ip_address": "127.0.0.1",
      "workspace_id": "workspace-user-123"
    }
  ]
}
```

The usage log is:

- **Process-safe on Unix**: Uses a shared file lock per log path for concurrent worker writes.
- **Capped**: The default maximum is 10,000 entries. Older entries are rotated out to prevent unbounded growth.
- **Visible**: The "Usage Statistics" table in the admin UI shows the latest 20 log entries and links to the full JSON usage file.
- **Runtime-only**: `app/data/api_keys_usage.json` is ignored by git; use `app/data/api_keys_usage.example.json` as the committed schema sample.

## Security Notes

- The raw key value is revealed only in the current admin response and is never stored in the Flask session cookie.
- Existing plaintext keys are migrated to hashes automatically at application startup.
- Keys are masked in the UI as `sk-abc...xyz`.
- Disabled keys remain in the store for audit trails.
- Expiration validation runs on every request before the key is accepted for authentication.
