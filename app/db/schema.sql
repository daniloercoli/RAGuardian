-- SQLite schema for the RAGuardian user store.
--
-- This file is executed once by db/schema.initialize_schema() for an empty
-- local database. There is intentionally no previous-version upgrade path.
--
-- Two tables:
--   users    - local accounts (email + password login, or admin bootstrap)
--   api_keys - personal API tokens issued by each user
--
-- Notes:
--   * enabled is INTEGER (0 or 1) because SQLite has no native BOOL type.
--   * scopes / knowledge_base_ids are stored as JSON strings for flexibility.
--   * ON DELETE CASCADE on api_keys.user_id means deleting a user also
--     removes all their API keys automatically.

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deletion_status TEXT NOT NULL DEFAULT '',
    deletion_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    key_prefix TEXT NOT NULL DEFAULT '',
    key_suffix TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    knowledge_base_ids TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used TEXT NOT NULL DEFAULT '',
    usage_count INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    UNIQUE(user_id, name)
);

-- Indexes for the most common lookup patterns.
CREATE INDEX idx_api_keys_user_id ON api_keys(user_id);
CREATE INDEX idx_api_keys_key_hash ON api_keys(key_hash);
CREATE INDEX idx_users_email ON users(email);
