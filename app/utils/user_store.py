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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

from utils.file_lock import ProcessSafeFileLock


USER_ROLES = {"admin", "user"}


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
        api_key_value: str | None = None,
        enabled: bool = True,
        description: str = "",
        expires_at: str | None = None,
    ) -> dict:
        """Create a new API key for a user. Returns the key with masked value."""
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        if not api_key_value:
            api_key_value = _generate_api_key()
        api_key_value = api_key_value.strip()

        if not self.get(user_id):
            raise ValueError("User not found")

        now = _now()
        new_key = {
            "id": uuid.uuid4().hex,
            "name": name,
            "key_hash": api_key_hash(api_key_value),
            "key_prefix": api_key_value[:8],
            "key_suffix": api_key_value[-4:],
            "scopes": self._normalize_api_scopes(scopes),
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
                existing = usr.get("api_keys") or []
                if any(k.get("name") == new_key["name"] for k in existing):
                    raise ValueError(f"API key name '{new_key['name']}' already exists for this user")
                usr["api_keys"] = existing + [new_key]
                usr["updated_at"] = now
                self._save_unlocked(users)
                break

        return {
            "name": new_key["name"],
            "key": api_key_value,
            "masked_key": _mask_api_key(api_key_value),
            "scopes": new_key["scopes"],
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
        """Hash API keys created by versions that stored raw values."""
        migrated = 0
        with self._lock:
            users = self._list_unlocked()
            for user in users:
                for key in user.get("api_keys") or []:
                    raw = str(key.pop("key", "") or "")
                    if not raw:
                        continue
                    key["key_hash"] = api_key_hash(raw)
                    key["key_prefix"] = raw[:8]
                    key["key_suffix"] = raw[-4:]
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
                        key["scopes"] = normalized
                        break
                else:
                    return None
                usr["updated_at"] = _now()
                self._save_unlocked(users)
                return _public_api_key(key, user_id=user_id)
            return None

    def _normalize_api_scopes(self, scopes: list[str]) -> list[str]:
        """Normalize scopes to known values (query, ingest, speech)."""
        valid: set[str] = {"query", "ingest", "speech"}
        result: list[str] = []
        for s in scopes:
            cleaned = str(s).strip().lower()
            if cleaned in valid and cleaned not in result:
                result.append(cleaned)
        if not result:
            result = ["query"]
        return result

    def _public_list_unlocked(self) -> list[dict]:
        return [_public_user(user) for user in self._list_unlocked()]

    def _list_unlocked(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        users = data.get("users") if isinstance(data, dict) else data
        return users if isinstance(users, list) else []

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
