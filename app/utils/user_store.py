from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from utils.file_lock import ProcessSafeFileLock


USER_ROLES = {"admin", "user"}
USER_DELETION_STATUSES = {"deleting", "delete_failed"}


class UserDeletionPreflightError(RuntimeError):
    """Raised when deletion is rejected before any user data is removed."""


class UserStore:
    """JSON-backed local user store for personal RAG accounts."""

    _locks: dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Optional[str] = None):
        configured = path or os.getenv("RAG_USERS_FILE", "app/data/users.json")
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            lock_key = str(self.path.resolve())
            self._lock = self._locks.setdefault(
                lock_key,
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )

    def list(self) -> list[dict]:
        with self._lock:
            return self._public_list_unlocked()

    def get(self, user_id: str) -> Optional[dict]:
        for user in self.list():
            if user.get("id") == user_id:
                return user
        return None

    def get_by_email(self, email: str) -> Optional[dict]:
        normalized = normalize_email(email)
        for user in self.list():
            if user.get("email") == normalized:
                return user
        return None

    def has_users(self) -> bool:
        return bool(self.list())

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
        with self._lock:
            users = self._list_unlocked()
            if any(user.get("email") == email for user in users):
                raise ValueError("email already exists")
            user = _user_record(
                email=email,
                password=password,
                display_name=display_name,
                role=role,
                enabled=enabled,
            )
            users.append(user)
            self._save_unlocked(users)
            return _public_user(user)

    def update_user(self, user_id: str, **patch) -> Optional[dict]:
        with self._lock:
            users = self._list_unlocked()
            changed = None
            for index, user in enumerate(users):
                if user.get("id") != user_id:
                    continue
                if "display_name" in patch:
                    user["display_name"] = str(patch["display_name"] or user.get("email") or "").strip()
                if "role" in patch and patch["role"] in USER_ROLES:
                    user["role"] = patch["role"]
                if "enabled" in patch:
                    if (
                        patch["enabled"]
                        and user.get("deletion_status")
                        in USER_DELETION_STATUSES
                    ):
                        raise ValueError(
                            "Un utente con cancellazione incompleta non può "
                            "essere riabilitato; ripeti la cancellazione"
                        )
                    user["enabled"] = bool(patch["enabled"])
                if patch.get("password"):
                    user["password_hash"] = generate_password_hash(str(patch["password"]))
                user["updated_at"] = _now()
                users[index] = user
                changed = _public_user(user)
                break
            if changed:
                self._save_unlocked(users)
            return changed

    def authenticate(self, email: str, password: str) -> Optional[dict]:
        email = normalize_email(email)
        with self._lock:
            for user in self._list_unlocked():
                if user.get("email") != email or not user.get("enabled", True):
                    continue
                if check_password_hash(user.get("password_hash", ""), password):
                    return _public_user(user)
        return None

    def bootstrap_admin_if_empty(self, *, email: str, password: str) -> dict | None:
        with self._lock:
            if self._list_unlocked():
                return None
            user = _user_record(
                email=normalize_email(email or "admin@example.local"),
                password=password,
                display_name="Admin",
                role="admin",
                enabled=True,
            )
            self._save_unlocked([user])
            return _public_user(user)

    def get_api_keys(self, user_id: str, *, include_raw: bool = False) -> list[dict]:
        """Return API keys for a user with raw values hidden by default."""
        with self._lock:
            for user in self._list_unlocked():
                if user.get("id") != user_id:
                    continue
                return [
                    _public_api_key(key, user_id=user_id, include_raw=include_raw)
                    for key in (user.get("api_keys") or [])
                ]
        return []

    def get_api_key(self, user_id: str, key_name: str, *, include_raw: bool = False) -> dict | None:
        """Return one API key by name, hiding the raw value unless requested."""
        with self._lock:
            for user in self._list_unlocked():
                if user.get("id") != user_id:
                    continue
                for key in (user.get("api_keys") or []):
                    if key.get("name") == key_name:
                        return _public_api_key(key, user_id=user_id, include_raw=include_raw)
                return None
        return None

    def update_api_key_usage(self, user_id: str, key_name: str, *, extra: dict | None = None) -> None:
        """Update last_used and usage_count for a named API key."""
        if not extra:
            extra = {}
        with self._lock:
            users = self._list_unlocked()
            for user in users:
                if user.get("id") != user_id:
                    continue
                for key in (user.get("api_keys") or []):
                    if key.get("name") == key_name and key.get("enabled", True):
                        key["last_used"] = _now()
                        key["usage_count"] = key.get("usage_count", 0) + 1
                        key.update(extra)
                        user["updated_at"] = _now()
                        break
                else:
                    continue
                self._save_unlocked(users)
                return
            # Key not found -- no-op

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
        """Create a new API key for a user. Returns the key with masked value."""
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
            {"query", "ingest", "agent_manage"} & set(normalized_scopes)
            and not normalized_knowledge_base_ids
        ):
            raise ValueError(
                "At least one knowledge base is required for query, ingest, or agent_manage"
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

        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                if validate is not None:
                    validate()
                existing = usr.get("api_keys") or []
                if any(k.get("name") == new_key["name"] for k in existing):
                    raise ValueError(f"API key name '{new_key['name']}' already exists for this user")
                usr["api_keys"] = existing + [new_key]
                usr["updated_at"] = now
                self._save_unlocked(users)
                break
            else:
                raise ValueError("User not found")

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
        """Toggle enabled state for an API key. Returns updated key or None."""
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key.get("name") == key_name:
                        if enabled is None:
                            key["enabled"] = not key.get("enabled", True)
                        else:
                            key["enabled"] = bool(enabled)
                        break
                else:
                    return None
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return _public_api_key(key, user_id=user_id)
            return None

    def delete_api_key(self, *, user_id: str, key_name: str) -> bool:
        """Delete an API key for a user. Returns True if found and deleted."""
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                original = usr.get("api_keys") or []
                usr["api_keys"] = [k for k in original if k.get("name") != key_name]
                if len(usr["api_keys"]) == len(original):
                    return False
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return True
            return False

    def delete_user(
        self,
        user_id: str,
        *,
        before_delete: Callable[[dict], None] | None = None,
    ) -> bool:
        """Delete a user after an optional cleanup succeeds."""
        if before_delete is None:
            with self._lock:
                users = self._list_unlocked()
                if not any(user.get("id") == user_id for user in users):
                    return False
                users = [
                    user for user in users if user.get("id") != user_id
                ]
                self._save_unlocked(users)
                return True

        with self._lock:
            users = self._list_unlocked()
            target = next((user for user in users if user.get("id") == user_id), None)
            if target is None:
                return False
            original_target = deepcopy(target)
            target["enabled"] = False
            target["deletion_status"] = "deleting"
            target["deletion_error"] = ""
            target["updated_at"] = _now()
            self._save_unlocked(users)

        try:
            before_delete(_public_user(target))
        except UserDeletionPreflightError:
            with self._lock:
                users = self._list_unlocked()
                for index, current in enumerate(users):
                    if current.get("id") == user_id:
                        users[index] = original_target
                        self._save_unlocked(users)
                        break
            raise
        except Exception as exc:
            with self._lock:
                users = self._list_unlocked()
                current = next(
                    (user for user in users if user.get("id") == user_id),
                    None,
                )
                if current is not None:
                    current["enabled"] = False
                    current["deletion_status"] = "delete_failed"
                    current["deletion_error"] = str(exc)
                    current["updated_at"] = _now()
                    self._save_unlocked(users)
            raise

        with self._lock:
            users = self._list_unlocked()
            if not any(user.get("id") == user_id for user in users):
                return False
            users = [user for user in users if user.get("id") != user_id]
            self._save_unlocked(users)
            return True

    def rotate_api_key(self, *, user_id: str, key_name: str) -> dict | None:
        """Generate a new raw key value. Returns updated key or None."""
        new_key = _generate_api_key()
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key.get("name") == key_name:
                        key.pop("key", None)
                        key["key_hash"] = api_key_hash(new_key)
                        key["key_prefix"] = new_key[:8]
                        key["key_suffix"] = new_key[-4:]
                        break
                else:
                    return None
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return {**_public_api_key(key, user_id=user_id), "key": new_key}
            return None

    def migrate_legacy_api_keys(self) -> int:
        """Hash legacy keys and make their default-only access explicit."""
        migrated = 0
        with self._lock:
            users = self._list_unlocked()
            for user in users:
                for key in user.get("api_keys") or []:
                    changed = False
                    if not key.get("id"):
                        key["id"] = uuid.uuid4().hex
                        changed = True
                    raw = str(key.pop("key", "") or "")
                    if raw:
                        key["key_hash"] = api_key_hash(raw)
                        key["key_prefix"] = raw[:8]
                        key["key_suffix"] = raw[-4:]
                        changed = True
                    if "knowledge_base_ids" not in key:
                        key["knowledge_base_ids"] = ["default"]
                        changed = True
                    if changed:
                        migrated += 1
            if migrated:
                self._save_unlocked(users)
        return migrated

    def update_api_key_name(self, *, user_id: str, key_name: str, new_name: str) -> dict | None:
        """Rename an API key. Returns updated key or None."""
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("new_name is required")
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                if any(k.get("name") == new_name and k.get("name") != key_name for k in (usr.get("api_keys") or [])):
                    raise ValueError(f"API key name '{new_name}' already exists for this user")
                for key in (usr.get("api_keys") or []):
                    if key.get("name") == key_name:
                        key["name"] = new_name
                        break
                else:
                    return None
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return _public_api_key(key, user_id=user_id)
            return None

    def update_api_key_scopes(self, *, user_id: str, key_name: str, scopes: list[str]) -> dict | None:
        """Update scopes for an API key. Returns updated key or None."""
        normalized = self._normalize_api_scopes(scopes)
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key.get("name") == key_name:
                        knowledge_base_ids = self._normalize_knowledge_base_ids(
                            key.get("knowledge_base_ids")
                        )
                        if (
                            {"query", "ingest", "agent_manage"} & set(normalized)
                            and not knowledge_base_ids
                        ):
                            raise ValueError(
                                "At least one knowledge base is required for query, ingest, or agent_manage"
                            )
                        key["scopes"] = normalized
                        break
                else:
                    return None
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return _public_api_key(key, user_id=user_id)
            return None

    def update_api_key_knowledge_bases(
        self,
        *,
        user_id: str,
        key_name: str,
        knowledge_base_ids: list[str],
    ) -> dict | None:
        normalized = self._normalize_knowledge_base_ids(knowledge_base_ids)
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key.get("name") != key_name:
                        continue
                    scopes = set(key.get("scopes") or ["query"])
                    if {"query", "ingest", "agent_manage"} & scopes and not normalized:
                        raise ValueError(
                            "At least one knowledge base is required for query, ingest, or agent_manage"
                        )
                    key["knowledge_base_ids"] = normalized
                    usr["updated_at"] = _now()
                    self._save_unlocked(users)
                    return _public_api_key(key, user_id=user_id)
                return None
            return None

    def update_api_key_access(
        self,
        *,
        user_id: str,
        key_name: str,
        scopes: list[str],
        knowledge_base_ids: list[str],
        validate: Callable[[], None] | None = None,
    ) -> dict | None:
        """Atomically update scopes and KB grants after in-lock validation."""

        normalized_scopes = self._normalize_api_scopes(scopes)
        normalized_knowledge_base_ids = self._normalize_knowledge_base_ids(
            knowledge_base_ids
        )
        if (
            {"query", "ingest", "agent_manage"} & set(normalized_scopes)
            and not normalized_knowledge_base_ids
        ):
            raise ValueError(
                "At least one knowledge base is required for query, ingest, or agent_manage"
            )
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key.get("name") != key_name:
                        continue
                    if validate is not None:
                        validate()
                    key["scopes"] = normalized_scopes
                    key["knowledge_base_ids"] = normalized_knowledge_base_ids
                    usr["updated_at"] = _now()
                    self._save_unlocked(users)
                    return _public_api_key(key, user_id=user_id)
                return None
            return None

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
        """Grant one KB without replacing grants added by concurrent writers."""
        normalized_id = self._normalize_knowledge_base_ids(
            [knowledge_base_id]
        )[0]
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                for key in (usr.get("api_keys") or []):
                    if key_id:
                        if str(key.get("id") or "") != str(key_id):
                            continue
                    elif key.get("name") != key_name:
                        continue
                    if not key.get("enabled", True):
                        return None
                    if (
                        required_scope
                        and required_scope not in set(key.get("scopes") or [])
                    ):
                        return None
                    if validate is not None:
                        validate()
                    allowed = self._normalize_knowledge_base_ids(
                        key.get("knowledge_base_ids")
                    )
                    if normalized_id not in allowed:
                        allowed.append(normalized_id)
                        key["knowledge_base_ids"] = allowed
                        usr["updated_at"] = _now()
                        self._save_unlocked(users)
                    return _public_api_key(key, user_id=user_id)
                return None
            return None

    def ensure_api_key_knowledge_base_ids(
        self,
        *,
        default_knowledge_base_id: str = "default",
        user_ids: set[str] | None = None,
    ) -> int:
        """Make legacy API-key KB grants explicit under the users-file lock."""
        normalized_default = self._normalize_knowledge_base_ids(
            [default_knowledge_base_id]
        )
        updated = 0
        with self._lock:
            users = self._list_unlocked()
            for usr in users:
                if user_ids is not None and str(usr.get("id") or "") not in user_ids:
                    continue
                user_changed = False
                for key in (usr.get("api_keys") or []):
                    if "knowledge_base_ids" in key:
                        continue
                    key["knowledge_base_ids"] = list(normalized_default)
                    updated += 1
                    user_changed = True
                if user_changed:
                    usr["updated_at"] = _now()
            if updated:
                self._save_unlocked(users)
        return updated

    def remove_knowledge_base_from_api_keys(
        self,
        *,
        user_id: str,
        knowledge_base_id: str,
        finalize: Callable[[], None] | None = None,
        lease_check: Callable[[], None] | None = None,
    ) -> dict:
        from utils.index_lock import DistributedLockLeaseLostError

        updated = 0
        disabled = 0
        with self._lock:
            users = self._list_unlocked()
            original_users = deepcopy(users)
            owner_found = False
            for usr in users:
                if usr.get("id") != user_id:
                    continue
                owner_found = True
                for key in (usr.get("api_keys") or []):
                    allowed = self._normalize_knowledge_base_ids(
                        key.get("knowledge_base_ids")
                    )
                    if knowledge_base_id not in allowed:
                        continue
                    key["knowledge_base_ids"] = [
                        item for item in allowed if item != knowledge_base_id
                    ]
                    updated += 1
                    scopes = set(key.get("scopes") or ["query"])
                    if (
                        not key["knowledge_base_ids"]
                        and (
                            bool({"query", "ingest", "agent_manage"} & scopes)
                            or "kb_manage" not in scopes
                        )
                    ):
                        key["enabled"] = False
                        disabled += 1
                if updated:
                    usr["updated_at"] = _now()
                    if lease_check is not None:
                        lease_check()
                    self._save_unlocked(users)
                break
            if not owner_found:
                raise ValueError("User not found")
            try:
                if lease_check is not None:
                    lease_check()
                if finalize is not None:
                    finalize()
            except DistributedLockLeaseLostError:
                # A rollback is itself a live write. Once the lease is known
                # to be lost, leave recovery to the next protected run.
                raise
            except Exception:
                if updated:
                    try:
                        self._save_unlocked(original_users)
                    except Exception as rollback_exc:
                        raise RuntimeError(
                            "API key cleanup failed and rollback was not completed"
                        ) from rollback_exc
                raise
        return {
            "updated": updated,
            "disabled": disabled,
        }

    def _normalize_api_scopes(self, scopes: list[str]) -> list[str]:
        """Normalize scopes to known values."""
        valid: set[str] = {"query", "ingest", "speech", "kb_manage", "agent_manage"}
        result: list[str] = []
        for s in scopes:
            cleaned = str(s).strip().lower()
            if cleaned in valid and cleaned not in result:
                result.append(cleaned)
        if not result:
            result = ["query"]
        return result

    def _normalize_knowledge_base_ids(self, values) -> list[str]:
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

    def _public_list_unlocked(self) -> list[dict]:
        return [_public_user(user) for user in self._list_unlocked()]

    def _list_unlocked(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid user store: {self.path}") from exc
        users = data.get("users") if isinstance(data, dict) else data
        if not isinstance(users, list):
            raise ValueError(f"Invalid user store: {self.path}")
        return users

    def _save_unlocked(self, users: list[dict]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".users.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"users": users}, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def _public_user(user: dict) -> dict:
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
    slug = re.sub(r"[^a-z0-9_.-]+", "-", email.lower()).strip("-._")
    slug = slug[:48] or "user"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _generate_api_key() -> str:
    return f"rag_{secrets.token_urlsafe(32)}"


def api_key_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def api_key_matches(record: dict, candidate: str) -> bool:
    stored_hash = str(record.get("key_hash") or "")
    if stored_hash:
        return hmac.compare_digest(stored_hash, api_key_hash(candidate))
    legacy = str(record.get("key") or "")
    return bool(legacy) and hmac.compare_digest(legacy, str(candidate or ""))


def _mask_api_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "*" * len(key) if key else ""
    return f"{key[:8]}...{key[-4:]}"
