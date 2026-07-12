"""Buffered JSON usage logger for per-user API keys."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.file_lock import ProcessSafeFileLock

DEFAULT_USAGE_FILE = os.getenv("RAG_API_KEY_USAGE_FILE", "app/data/api_keys_usage.json")
DEFAULT_MAX_ENTRIES = 10_000


class ApiKeyLogger:
    """Thread-safe, buffered JSON logger for API key usage events."""

    _locks: dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Optional[str] = None, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.path = Path(path or DEFAULT_USAGE_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            self._lock = self._locks.setdefault(
                str(self.path.resolve()),
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )
        self._enabled = True
        self._max_entries = max_entries

    def log(
        self,
        *,
        user_id: str,
        key_name: str,
        endpoint: str,
        method: str,
        status_code: int,
        scope: str | None = None,
        scopes_used: list[str] | None = None,
        duration_ms: int | None = None,
        request_id: str = "",
        ip_address: str = "",
        workspace_id: str = "",
        api_key_id: str = "",
    ) -> None:
        if not self._enabled:
            return
        timestamp = _now()
        scopes = scopes_used if scopes_used is not None else _split_scope(scope)
        entry = {
            "id": uuid.uuid4().hex,
            "timestamp": timestamp,
            "date_bucket": timestamp[:10],
            "user_id": user_id,
            "api_key_id": api_key_id or key_name,
            "api_key_name": key_name,
            "key_name": key_name,
            "scopes_used": scopes,
            "scope": ",".join(scopes),
            "endpoint": endpoint,
            "method": method,
            "response_code": status_code,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "request_id": request_id,
            "ip_address": ip_address,
            "workspace_id": workspace_id,
        }
        with self._lock:
            data = self._load_data_unlocked()
            if not data.get("logging_enabled", True):
                return
            entries = data["log_entries"]
            entries.append(entry)
            if len(entries) > self._max_entries:
                data["log_entries"] = entries[-self._max_entries:]
            self._save_unlocked(data)

    def usage_stats(self) -> list[dict]:
        """Return aggregate usage statistics per user/key."""
        with self._lock:
            entries = self._load_data_unlocked()["log_entries"]
        stats: dict[str, dict] = {}
        for entry in entries:
            key_name = entry.get("api_key_name") or entry.get("key_name") or "unknown"
            key = f"{entry.get('user_id', 'unknown')}|{key_name}"
            bucket = stats.setdefault(key, {
                "user_id": entry.get("user_id"),
                "key_name": key_name,
                "requests": 0,
                "endpoints": {},
            })
            bucket["requests"] += 1
            endpoint = entry.get("endpoint", "unknown")
            bucket["endpoints"][endpoint] = bucket["endpoints"].get(endpoint, 0) + 1
        return list(stats.values())

    def recent_entries(self, limit: int = 20) -> list[dict]:
        """Return the newest usage log entries first."""
        if limit <= 0:
            return []
        with self._lock:
            entries = list(self._load_data_unlocked()["log_entries"])
        return list(reversed(entries[-limit:]))

    def file_exists(self) -> bool:
        return self.path.exists()

    def _load_data_unlocked(self) -> dict:
        if not self.path.exists():
            return {"logging_enabled": True, "log_entries": []}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"logging_enabled": True, "log_entries": []}
        if isinstance(data, list):
            return {"logging_enabled": True, "log_entries": data}
        if not isinstance(data, dict):
            return {"logging_enabled": True, "log_entries": []}
        entries = data.get("log_entries", [])
        return {
            "logging_enabled": bool(data.get("logging_enabled", True)),
            "log_entries": entries if isinstance(entries, list) else [],
        }

    def _save_unlocked(self, data: dict) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".api_usage.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _split_scope(scope: str | None) -> list[str]:
    if not scope:
        return []
    return [part.strip() for part in str(scope).split(",") if part.strip()]
