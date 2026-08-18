from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from db.connection import get_connection
from db.schema import initialize_schema
from utils.settings_store import API_SCOPES, API_SCOPES_REQUIRING_KB


USER_ROLES = {"admin", "user"}
USER_DELETION_STATUSES = {"deleting", "delete_failed"}


class UserDeletionPreflightError(RuntimeError):
    """Raised when deletion is rejected before any user data is removed."""


class UserStore:
    """SQLite-backed local user store for personal RAG accounts.

    Each UserStore instance points to a single ``.db`` file. The schema is
    created automatically on first use from the current clean schema.

    The store manages two kinds of records:
      * **Users**   - email/password accounts with a role (admin/user).
      * **API keys** - per-user tokens with scopes and knowledge-base access.

    Connections are short-lived: every method opens a connection, does its
    work, commits, and closes. This keeps the code simple and avoids locking
    issues in a multi-threaded web server (gunicorn).
    """

    def __init__(self, path: Optional[str] = None):
        configured = path or os.getenv("RAG_USERS_DB", "app/data/users.db")
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initialize_schema(self.path)

    @contextmanager
    def _connect(self):
        """Open a connection, yield it, and always close it when done."""
        conn = get_connection(self.path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def list(self) -> list[dict]:
        """Return all users (without password hashes), newest last."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
            result = []
            for row in rows:
                user = _row_to_user(row)
                user["api_keys"] = self._load_api_keys(conn, user["id"])
                result.append(_public_user(user))
            return result

    def get(self, user_id: str) -> Optional[dict]:
        """Return a single user by id, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            user = _row_to_user(row)
            user["api_keys"] = self._load_api_keys(conn, user_id)
            return _public_user(user)

    def get_by_email(self, email: str) -> Optional[dict]:
        normalized = normalize_email(email)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (normalized,)
            ).fetchone()
            if row is None:
                return None
            user = _row_to_user(row)
            user["api_keys"] = self._load_api_keys(conn, user["id"])
            return _public_user(user)

    def has_users(self) -> bool:
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return count > 0

    def create_user(
        self,
        *,
        email: str,
        password: str,
        display_name: str = "",
        role: str = "user",
        enabled: bool = True,
    ) -> dict:
        email = normalize_email(email)
        role = role if role in USER_ROLES else "user"
        if not email:
            raise ValueError("email is required")
        if not password:
            raise ValueError("password is required")
        user = _user_record(
            email=email,
            password=password,
            display_name=display_name,
            role=role,
            enabled=enabled,
        )
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users
                        (id, email, display_name, password_hash, role,
                         enabled, created_at, updated_at, deletion_status,
                         deletion_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')
                    """,
                    (
                        user["id"],
                        user["email"],
                        user["display_name"],
                        user["password_hash"],
                        user["role"],
                        1 if user["enabled"] else 0,
                        user["created_at"],
                        user["updated_at"],
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                if "email" in str(exc).lower():
                    raise ValueError("email already exists") from exc
                raise
        return _public_user(user)

    def update_user(self, user_id: str, **patch) -> Optional[dict]:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            user = _row_to_user(row)

            sets: list[str] = []
            params: list = []

            if "display_name" in patch:
                user["display_name"] = str(
                    patch["display_name"] or user.get("email") or ""
                ).strip()
                sets.append("display_name = ?")
                params.append(user["display_name"])

            if "role" in patch and patch["role"] in USER_ROLES:
                user["role"] = patch["role"]
                sets.append("role = ?")
                params.append(user["role"])

            if "enabled" in patch:
                if patch["enabled"] and user.get("deletion_status") in USER_DELETION_STATUSES:
                    raise ValueError(
                        "A user with an incomplete deletion cannot be re-enabled; "
                        "retry the deletion"
                    )
                user["enabled"] = bool(patch["enabled"])
                sets.append("enabled = ?")
                params.append(1 if user["enabled"] else 0)

            if patch.get("password"):
                new_hash = generate_password_hash(str(patch["password"]))
                user["password_hash"] = new_hash
                sets.append("password_hash = ?")
                params.append(new_hash)

            sets.append("updated_at = ?")
            params.append(now)
            params.append(user_id)

            conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params
            )
            conn.commit()

            user["updated_at"] = now
            user["api_keys"] = self._load_api_keys(conn, user_id)
            return _public_user(user)

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        """Return the user if email+password match and the account is enabled."""
        email = normalize_email(email)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? AND enabled = 1",
                (email,),
            ).fetchone()
            if row is None:
                return None
            if not check_password_hash(row["password_hash"], password):
                return None
            user = _row_to_user(row)
            user["api_keys"] = self._load_api_keys(conn, user["id"])
            return _public_user(user)

    def bootstrap_admin_if_empty(self, *, email: str, password: str) -> dict | None:
        """Create the first admin user if the database is empty.

        Returns the new admin user dict, or None if users already exist.
        Uses BEGIN IMMEDIATE so two concurrent startups cannot create two admins.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count > 0:
                conn.rollback()
                return None
            user = _user_record(
                email=normalize_email(email or "admin@example.local"),
                password=password,
                display_name="Admin",
                role="admin",
                enabled=True,
            )
            conn.execute(
                """
                INSERT INTO users
                    (id, email, display_name, password_hash, role,
                     enabled, created_at, updated_at, deletion_status,
                     deletion_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')
                """,
                (
                    user["id"],
                    user["email"],
                    user["display_name"],
                    user["password_hash"],
                    user["role"],
                    1,
                    user["created_at"],
                    user["updated_at"],
                ),
            )
            conn.commit()
            return _public_user(user)

    # ------------------------------------------------------------------
    # API key CRUD
    #
    # API keys are stored as SHA-256 hashes (never plaintext).
    # The raw key is returned to the caller only once, at creation time.
    # ------------------------------------------------------------------

    def get_api_keys(self, user_id: str, *, include_raw: bool = False) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at",
                (user_id,),
            ).fetchall()
            return [
                _public_api_key(_row_to_api_key(row), user_id=user_id, include_raw=include_raw)
                for row in rows
            ]

    def get_api_key(self, user_id: str, key_name: str, *, include_raw: bool = False) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            return _public_api_key(_row_to_api_key(row), user_id=user_id, include_raw=include_raw)

    def update_api_key_usage(self, user_id: str, key_name: str) -> None:
        """Atomically record successful use of an enabled API key."""

        now = _now()
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE api_keys
                SET last_used = ?, usage_count = usage_count + 1
                WHERE user_id = ? AND name = ? AND enabled = 1
                """,
                (now, user_id, key_name),
            )
            if updated.rowcount == 0:
                return
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()

    def create_api_key(
        self,
        *,
        user_id: str,
        name: str,
        scopes: list[str],
        knowledge_base_ids: list[str] | None = None,
        api_key_value: str | None = None,
        enabled: bool = True,
        description: str = "",
        expires_at: str | None = None,
        validate: Callable[[], None] | None = None,
    ) -> dict:
        """Create a new API key for a user.

        The raw key value is generated automatically (or uses the provided
        one). It is returned in the result dict under ``"key"`` so the caller
        can show it once to the user. Only the hash is stored in the database.
        """
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if not api_key_value:
            api_key_value = _generate_api_key()
        api_key_value = api_key_value.strip()

        now = _now()
        normalized_scopes = self._normalize_api_scopes(scopes)
        normalized_knowledge_base_ids = self._normalize_knowledge_base_ids(
            knowledge_base_ids
        )
        if (
            API_SCOPES_REQUIRING_KB & set(normalized_scopes)
            and not normalized_knowledge_base_ids
        ):
            raise ValueError(
                "At least one knowledge base is required for query, ingest, agent_manage, history_read, or history_manage"
            )
        new_key = {
            "id": uuid.uuid4().hex,
            "name": name,
            "key_hash": api_key_hash(api_key_value),
            "key_prefix": api_key_value[:8],
            "key_suffix": api_key_value[-4:],
            "scopes": normalized_scopes,
            "knowledge_base_ids": normalized_knowledge_base_ids,
            "enabled": bool(enabled),
            "created_at": now,
            "last_used": "",
            "usage_count": 0,
            "description": (description or "").strip(),
            "expires_at": expires_at,
        }

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user_row = conn.execute(
                "SELECT id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user_row is None:
                raise ValueError("User not found")
            if validate is not None:
                validate()
            try:
                conn.execute(
                    """
                    INSERT INTO api_keys
                        (id, user_id, name, key_hash, key_prefix, key_suffix,
                         scopes, knowledge_base_ids, enabled, created_at,
                         last_used, usage_count, description, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_key["id"],
                        user_id,
                        new_key["name"],
                        new_key["key_hash"],
                        new_key["key_prefix"],
                        new_key["key_suffix"],
                        json.dumps(new_key["scopes"]),
                        json.dumps(new_key["knowledge_base_ids"]),
                        1 if new_key["enabled"] else 0,
                        new_key["created_at"],
                        new_key["last_used"],
                        new_key["usage_count"],
                        new_key["description"],
                        new_key["expires_at"],
                    ),
                )
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE id = ?",
                    (now, user_id),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(
                    f"API key name '{new_key['name']}' already exists for this user"
                )

        return {
            "name": new_key["name"],
            "key": api_key_value,
            "masked_key": _mask_api_key(api_key_value),
            "scopes": new_key["scopes"],
            "knowledge_base_ids": new_key["knowledge_base_ids"],
            "enabled": new_key["enabled"],
            "created_at": new_key["created_at"],
            "description": new_key["description"],
            "expires_at": new_key["expires_at"],
            "id": new_key["id"],
        }

    def toggle_api_key_enabled(self, *, user_id: str, key_name: str, enabled: bool | None = None) -> dict | None:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            if enabled is None:
                new_enabled = not bool(row["enabled"])
            else:
                new_enabled = bool(enabled)
            conn.execute(
                "UPDATE api_keys SET enabled = ? WHERE id = ?",
                (1 if new_enabled else 0, row["id"]),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["enabled"] = new_enabled
            return _public_api_key(key, user_id=user_id)

    def delete_api_key(self, *, user_id: str, key_name: str) -> bool:
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM api_keys WHERE id = ?", (row["id"],))
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            return True

    def delete_user(
        self,
        user_id: str,
        *,
        before_delete: Callable[[dict], None] | None = None,
    ) -> bool:
        """Delete a user and (optionally) run a preflight callback first.

        If before_delete is None, the user is deleted immediately.

        If before_delete is provided, the deletion happens in 3 phases:
          1. Mark the user as "deleting" (disabled) in the DB.
          2. Run the before_delete callback (no DB connection held).
             - If it raises UserDeletionPreflightError, restore the user.
             - If it raises any other Exception, mark as "delete_failed".
          3. Actually delete the user row (cascade removes API keys).

        This phased approach lets the caller clean up external resources
        (ChromaDB collections, workspace files, etc.) before the user row
        is removed, while still being safe if the callback fails.
        """
        if before_delete is None:
            with self._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM users WHERE id = ?", (user_id,)
                )
                conn.commit()
                return cur.rowcount > 0

        # Phase 1: mark as deleting
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                return False
            original = _row_to_user(row)
            original["api_keys"] = self._load_api_keys(conn, user_id)
            now = _now()
            conn.execute(
                """
                UPDATE users SET enabled = 0, deletion_status = 'deleting',
                                 deletion_error = '', updated_at = ?
                WHERE id = ?
                """,
                (now, user_id),
            )
            conn.commit()
            target = deepcopy(original)
            target["enabled"] = False
            target["deletion_status"] = "deleting"
            target["deletion_error"] = ""

        # Phase 2: run preflight callback (no connection held)
        try:
            before_delete(_public_user(target))
        except UserDeletionPreflightError:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE users SET enabled = ?, deletion_status = ?,
                                     deletion_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        1 if original["enabled"] else 0,
                        original.get("deletion_status", ""),
                        original.get("deletion_error", ""),
                        _now(),
                        user_id,
                    ),
                )
                conn.commit()
            raise
        except Exception as exc:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE users SET enabled = 0,
                                 deletion_status = 'delete_failed',
                                 deletion_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), _now(), user_id),
                )
                conn.commit()
            raise

        # Phase 3: actually delete
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM users WHERE id = ?", (user_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def rotate_api_key(self, *, user_id: str, key_name: str) -> dict | None:
        new_key = _generate_api_key()
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE api_keys SET key_hash = ?, key_prefix = ?, key_suffix = ?
                WHERE id = ?
                """,
                (api_key_hash(new_key), new_key[:8], new_key[-4:], row["id"]),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["key_hash"] = api_key_hash(new_key)
            key["key_prefix"] = new_key[:8]
            key["key_suffix"] = new_key[-4:]
            result = _public_api_key(key, user_id=user_id)
            result["key"] = new_key
            return result

    def update_api_key_name(self, *, user_id: str, key_name: str, new_name: str) -> dict | None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new_name is required")
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            dup = conn.execute(
                "SELECT id FROM api_keys WHERE user_id = ? AND name = ? AND id != ?",
                (user_id, new_name, row["id"]),
            ).fetchone()
            if dup is not None:
                raise ValueError(
                    f"API key name '{new_name}' already exists for this user"
                )
            conn.execute(
                "UPDATE api_keys SET name = ? WHERE id = ?",
                (new_name, row["id"]),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["name"] = new_name
            return _public_api_key(key, user_id=user_id)

    def update_api_key_scopes(self, *, user_id: str, key_name: str, scopes: list[str]) -> dict | None:
        normalized = self._normalize_api_scopes(scopes)
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            knowledge_base_ids = self._normalize_knowledge_base_ids(
                json.loads(row["knowledge_base_ids"] or "[]")
            )
            if (
                API_SCOPES_REQUIRING_KB & set(normalized)
                and not knowledge_base_ids
            ):
                raise ValueError(
                    "At least one knowledge base is required for query, ingest, agent_manage, history_read, or history_manage"
                )
            conn.execute(
                "UPDATE api_keys SET scopes = ? WHERE id = ?",
                (json.dumps(normalized), row["id"]),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["scopes"] = normalized
            return _public_api_key(key, user_id=user_id)

    def update_api_key_knowledge_bases(
        self,
        *,
        user_id: str,
        key_name: str,
        knowledge_base_ids: list[str],
    ) -> dict | None:
        normalized = self._normalize_knowledge_base_ids(knowledge_base_ids)
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            scopes = set(json.loads(row["scopes"] or "[]")) or {"query"}
            if API_SCOPES_REQUIRING_KB & scopes and not normalized:
                raise ValueError(
                    "At least one knowledge base is required for query, ingest, agent_manage, history_read, or history_manage"
                )
            conn.execute(
                "UPDATE api_keys SET knowledge_base_ids = ? WHERE id = ?",
                (json.dumps(normalized), row["id"]),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["knowledge_base_ids"] = normalized
            return _public_api_key(key, user_id=user_id)

    def update_api_key_access(
        self,
        *,
        user_id: str,
        key_name: str,
        scopes: list[str],
        knowledge_base_ids: list[str],
        validate: Callable[[], None] | None = None,
    ) -> dict | None:
        normalized_scopes = self._normalize_api_scopes(scopes)
        normalized_knowledge_base_ids = self._normalize_knowledge_base_ids(
            knowledge_base_ids
        )
        if (
            API_SCOPES_REQUIRING_KB & set(normalized_scopes)
            and not normalized_knowledge_base_ids
        ):
            raise ValueError(
                "At least one knowledge base is required for query, ingest, agent_manage, history_read, or history_manage"
            )
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                (user_id, key_name),
            ).fetchone()
            if row is None:
                return None
            if validate is not None:
                validate()
            conn.execute(
                """
                UPDATE api_keys SET scopes = ?, knowledge_base_ids = ?
                WHERE id = ?
                """,
                (
                    json.dumps(normalized_scopes),
                    json.dumps(normalized_knowledge_base_ids),
                    row["id"],
                ),
            )
            conn.execute(
                "UPDATE users SET updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            conn.commit()
            key = _row_to_api_key(row)
            key["scopes"] = normalized_scopes
            key["knowledge_base_ids"] = normalized_knowledge_base_ids
            return _public_api_key(key, user_id=user_id)

    def add_knowledge_base_to_api_key(
        self,
        *,
        user_id: str,
        key_name: str,
        knowledge_base_id: str,
        key_id: str | None = None,
        required_scope: str | None = None,
        validate: Callable[[], None] | None = None,
    ) -> dict | None:
        normalized_id = self._normalize_knowledge_base_ids(
            [knowledge_base_id]
        )[0]
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if key_id:
                row = conn.execute(
                    "SELECT * FROM api_keys WHERE user_id = ? AND id = ?",
                    (user_id, str(key_id)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM api_keys WHERE user_id = ? AND name = ?",
                    (user_id, key_name),
                ).fetchone()
            if row is None:
                return None
            if not row["enabled"]:
                return None
            scopes = set(json.loads(row["scopes"] or "[]"))
            if required_scope and required_scope not in scopes:
                return None
            if validate is not None:
                validate()
            allowed = self._normalize_knowledge_base_ids(
                json.loads(row["knowledge_base_ids"] or "[]")
            )
            if normalized_id not in allowed:
                allowed.append(normalized_id)
                conn.execute(
                    "UPDATE api_keys SET knowledge_base_ids = ? WHERE id = ?",
                    (json.dumps(allowed), row["id"]),
                )
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE id = ?",
                    (now, user_id),
                )
                conn.commit()
            key = _row_to_api_key(row)
            key["knowledge_base_ids"] = allowed
            return _public_api_key(key, user_id=user_id)

    def remove_knowledge_base_from_api_keys(
        self,
        *,
        user_id: str,
        knowledge_base_id: str,
        finalize: Callable[[], None] | None = None,
        lease_check: Callable[[], None] | None = None,
    ) -> dict:
        """Remove a knowledge base from all of a user's API keys.

        If removing it would leave a key with no knowledge bases and a scope
        that requires one (query/ingest/agent_manage), that key is disabled.

        Two-phase commit with an optional finalize callback:
          Phase 1: update the DB under a transaction.
          Phase 2: call finalize() (no DB connection held).
        If finalize raises, the changes are rolled back.
        """
        from utils.index_lock import DistributedLockLeaseLostError

        updated = 0
        disabled = 0
        now = _now()

        # Phase 1: read and modify under transaction
        originals: list[dict] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            user_row = conn.execute(
                "SELECT id FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user_row is None:
                raise ValueError("User not found")
            key_rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            for row in key_rows:
                allowed = self._normalize_knowledge_base_ids(
                    json.loads(row["knowledge_base_ids"] or "[]")
                )
                if knowledge_base_id not in allowed:
                    continue
                originals.append(_row_to_api_key(row))
                new_allowed = [item for item in allowed if item != knowledge_base_id]
                updated += 1
                scopes = set(json.loads(row["scopes"] or "[]")) or {"query"}
                new_enabled = bool(row["enabled"])
                if (
                    not new_allowed
                    and (
                        bool(API_SCOPES_REQUIRING_KB & scopes)
                        or "kb_manage" not in scopes
                    )
                ):
                    new_enabled = False
                    disabled += 1
                conn.execute(
                    """
                    UPDATE api_keys SET knowledge_base_ids = ?, enabled = ?
                    WHERE id = ?
                    """,
                    (json.dumps(new_allowed), 1 if new_enabled else 0, row["id"]),
                )
            if updated:
                if lease_check is not None:
                    lease_check()
                conn.execute(
                    "UPDATE users SET updated_at = ? WHERE id = ?",
                    (now, user_id),
                )
                conn.commit()
            else:
                conn.rollback()

        # Phase 2: finalize callback (no connection held)
        try:
            if lease_check is not None:
                lease_check()
            if finalize is not None:
                finalize()
        except DistributedLockLeaseLostError:
            raise
        except Exception:
            if updated:
                with self._connect() as conn:
                    for orig in originals:
                        conn.execute(
                            """
                            UPDATE api_keys SET knowledge_base_ids = ?,
                                                 enabled = ?
                            WHERE id = ?
                            """,
                            (
                                json.dumps(orig["knowledge_base_ids"]),
                                1 if orig["enabled"] else 0,
                                orig["id"],
                            ),
                        )
                    conn.execute(
                        "UPDATE users SET updated_at = ? WHERE id = ?",
                        (now, user_id),
                    )
                    conn.commit()
            raise

        return {
            "updated": updated,
            "disabled": disabled,
        }

    # ------------------------------------------------------------------
    # API key lookup by raw value
    #
    # This is the authentication path: given a raw API key from the
    # X-API-Key header, find the matching (enabled) key+user in the DB.
    # The lookup is by SHA-256 hash, never by plaintext comparison.
    # ------------------------------------------------------------------

    def find_api_key_by_value(self, value: str) -> Optional[dict]:
        """Find an enabled API key by its raw value across all enabled users.

        Returns None if the key is not found, disabled, expired, or belongs
        to a disabled user. The returned dict includes ``user_id`` and
        ``api_key_id`` so the caller can attribute the request.
        """
        if not value:
            return None
        target_hash = api_key_hash(value)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ak.*, u.id AS u_id, u.enabled AS u_enabled
                FROM api_keys ak
                JOIN users u ON ak.user_id = u.id
                WHERE ak.key_hash = ? AND ak.enabled = 1 AND u.enabled = 1
                """,
                (target_hash,),
            ).fetchone()
            if row is None:
                return None
            expires_at = row["expires_at"]
            if expires_at and _is_expired(expires_at):
                return None
            scopes = json.loads(row["scopes"] or "[]")
            knowledge_base_ids = json.loads(row["knowledge_base_ids"] or "[]")
            return {
                "name": row["name"],
                "key": value,
                "enabled": True,
                "scopes": scopes,
                "knowledge_base_ids": knowledge_base_ids,
                "can_upload": "ingest" in scopes,
                "user_id": row["u_id"],
                "api_key_id": row["id"] or row["name"],
                "_user_key_name": row["name"],
                "_user_id_for_logging": row["u_id"],
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_api_keys(self, conn: sqlite3.Connection, user_id: str) -> list[dict]:
        """Load all API keys for a user (internal, uses an existing connection)."""
        rows = conn.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [_row_to_api_key(row) for row in rows]

    def _normalize_api_scopes(self, scopes: list[str]) -> list[str]:
        """Filter scopes to the valid set, lowercase, deduplicate.

        If nothing valid remains, default to ["query"].
        """
        valid: set[str] = set(API_SCOPES)
        result: list[str] = []
        for s in scopes:
            cleaned = str(s).strip().lower()
            if cleaned in valid and cleaned not in result:
                result.append(cleaned)
        if not result:
            result = ["query"]
        return result

    def _normalize_knowledge_base_ids(self, values) -> list[str]:
        """Normalize a list/string of knowledge-base IDs.

        Accepts None (defaults to ["default"]), a comma-separated string,
        or a list. Each ID is validated and deduplicated.
        """
        from utils.knowledge_base_store import validate_knowledge_base_id

        if values is None:
            values = ["default"]
        if isinstance(values, str):
            values = values.replace(",", "\n").splitlines()
        result: list[str] = []
        for value in values:
            knowledge_base_id = validate_knowledge_base_id(value)
            if knowledge_base_id not in result:
                result.append(knowledge_base_id)
        return result


# ----------------------------------------------------------------------
# Module-level helpers
#
# These are pure functions that do not need a database connection.
# They convert sqlite3.Row objects into plain dicts and generate IDs/timestamps.
# ----------------------------------------------------------------------

def normalize_email(value: str) -> str:
    """Lowercase and trim an email address."""
    return str(value or "").strip().lower()


def _row_to_user(row: sqlite3.Row) -> dict:
    """Convert a users-table row into a dict (including password_hash)."""
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deletion_status": row["deletion_status"] or "",
        "deletion_error": row["deletion_error"] or "",
    }


def _row_to_api_key(row: sqlite3.Row) -> dict:
    """Convert an api_keys-table row into a dict."""
    return {
        "id": row["id"],
        "name": row["name"],
        "key_hash": row["key_hash"],
        "key_prefix": row["key_prefix"],
        "key_suffix": row["key_suffix"],
        "scopes": json.loads(row["scopes"] or "[]"),
        "knowledge_base_ids": json.loads(row["knowledge_base_ids"] or "[]"),
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_used": row["last_used"] or "",
        "usage_count": row["usage_count"],
        "description": row["description"] or "",
        "expires_at": row["expires_at"],
    }


def _public_user(user: dict) -> dict:
    """Strip the password_hash before returning a user to the caller."""
    result = {key: value for key, value in user.items() if key != "password_hash"}
    if "api_keys" in result:
        result["api_keys"] = [
            _public_api_key(key, user_id=str(user.get("id") or ""))
            for key in (user.get("api_keys") or [])
        ]
    return result


def _user_record(
    *,
    email: str,
    password: str,
    display_name: str = "",
    role: str = "user",
    enabled: bool = True,
) -> dict:
    """Build a new user dict with a fresh id and hashed password."""
    now = _now()
    return {
        "id": _user_id(email),
        "email": email,
        "display_name": display_name.strip() or email,
        "password_hash": generate_password_hash(password),
        "role": role,
        "enabled": bool(enabled),
        "created_at": now,
        "updated_at": now,
    }


def _public_api_key(key: dict, *, user_id: str, include_raw: bool = False) -> dict:
    """Strip secret fields (hash, prefix, suffix) from an API key dict.

    If include_raw is True and the dict has a "key" field, include it
    (used only at creation/rotation time, when the user sees the key once).
    """
    result = {
        name: value
        for name, value in key.items()
        if name not in {"key", "key_hash", "key_prefix", "key_suffix"}
    }
    raw = str(key.get("key", "") or "")
    prefix = str(key.get("key_prefix") or raw[:8])
    suffix = str(key.get("key_suffix") or raw[-4:])
    result["masked_key"] = f"{prefix}...{suffix}" if prefix or suffix else ""
    knowledge_base_ids = (
        key.get("knowledge_base_ids")
        if "knowledge_base_ids" in key
        else ["default"]
    )
    result["knowledge_base_ids"] = list(knowledge_base_ids or [])
    result["user_id"] = user_id
    if include_raw and raw:
        result["key"] = raw
    return result


def _user_id(email: str) -> str:
    """Generate a stable-ish slug from an email plus a random suffix."""
    slug = re.sub(r"[^a-z0-9_.-]+", "-", email.lower()).strip("-._")
    slug = slug[:48] or "user"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    """Current UTC time as an ISO string (for storage in the DB)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _generate_api_key() -> str:
    """Generate a random API key string (rag_ prefix + 32 URL-safe chars)."""
    return f"rag_{secrets.token_urlsafe(32)}"


def api_key_hash(value: str) -> str:
    """SHA-256 hash of an API key value (for storage and lookup)."""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _mask_api_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "*" * len(key) if key else ""
    return f"{key[:8]}...{key[-4:]}"


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    parsed = _parse_expiration(expires_at)
    if parsed is None:
        return False
    return parsed <= datetime.now(timezone.utc)


def _parse_expiration(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            day = datetime.fromisoformat(raw).date()
            return datetime.combine(day, time.max, tzinfo=timezone.utc)
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
