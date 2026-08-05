"""Lease-scoped pending turn result store.

When a RAG turn finishes streaming but before it is durably committed to the
:class:`ConversationHistoryStore`, the full result payload is staged here so a
client retry that arrives before the commit can replay the answer without
re-running generation. Entries are keyed by ``(scope_key, turn_id)`` and
guarded by the lease token issued by ``ConversationHistoryStore.begin_turn``.

Two backends are provided:

* :class:`PendingTurnResultStore` – thread-safe in-process memory (default).
* :class:`RedisPendingTurnResultStore` – Redis-backed for multi-worker deploys.

The application picks a backend via :func:`configured_state_backend` and the
singleton is exposed through :func:`get_pending_turn_store`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from utils.state_backend import (
    configured_state_backend,
    redis_connection,
    redis_scan_delete,
    state_key_prefix,
)

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class PendingTurnResult:
    """Immutable view of a staged turn result."""

    scope_key: str
    turn_id: str
    lease_token: str
    result_digest: str
    result: Any
    created_at: float


@dataclass
class _Entry:
    lease_token: str
    result_digest: str
    result: Any
    created_at: float
    expires_at: float


class PendingTurnResultStore:
    """Thread-safe in-process store for pending turn results."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    @property
    def backend(self) -> str:
        return "memory"

    def put(
        self,
        scope_key: str,
        turn_id: str,
        *,
        lease_token: str,
        result_digest: str,
        result: Any,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        if not scope_key or not turn_id or not lease_token:
            return False
        now = time.time()
        ttl = int(ttl_seconds or self.ttl_seconds)
        entry = _Entry(
            lease_token=lease_token,
            result_digest=result_digest,
            result=result,
            created_at=now,
            expires_at=now + ttl,
        )
        with self._lock:
            self._purge_expired_locked(now)
            self._entries[self._key(scope_key, turn_id)] = entry
        return True

    def get(self, scope_key: str, turn_id: str) -> Optional[PendingTurnResult]:
        if not scope_key or not turn_id:
            return None
        key = self._key(scope_key, turn_id)
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.lease_token and entry.expires_at < now:
                self._entries.pop(key, None)
                return None
            return PendingTurnResult(
                scope_key=scope_key,
                turn_id=turn_id,
                lease_token=entry.lease_token,
                result_digest=entry.result_digest,
                result=entry.result,
                created_at=entry.created_at,
            )

    def delete(self, scope_key: str, turn_id: str) -> bool:
        if not scope_key or not turn_id:
            return False
        key = self._key(scope_key, turn_id)
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear_by_prefix(self, prefix: str) -> int:
        if not prefix:
            return 0
        now = time.time()
        needle = f"{prefix}:"
        with self._lock:
            self._purge_expired_locked(now)
            to_remove = [key for key in self._entries if needle in key]
            for key in to_remove:
                self._entries.pop(key, None)
            return len(to_remove)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()

    def _key(self, scope_key: str, turn_id: str) -> str:
        return f"{scope_key}\x1f{turn_id}"

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)


class RedisPendingTurnResultStore(PendingTurnResultStore):
    """Redis-backed pending turn result store for multi-worker deployments."""

    def __init__(
        self,
        *,
        redis_client=None,
        key_prefix: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        super().__init__(ttl_seconds=ttl_seconds)
        self._redis = redis_client or redis_connection()
        self._key_prefix = key_prefix or f"{state_key_prefix()}:pending-turn"

    @property
    def backend(self) -> str:
        return "redis"

    def put(
        self,
        scope_key: str,
        turn_id: str,
        *,
        lease_token: str,
        result_digest: str,
        result: Any,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        if not scope_key or not turn_id or not lease_token:
            return False
        now = time.time()
        ttl = int(ttl_seconds or self.ttl_seconds)
        payload = {
            "lease_token": lease_token,
            "result_digest": result_digest,
            "result": result,
            "created_at": now,
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            log.warning("Unable to serialize pending turn result for %s", turn_id)
            return False
        try:
            self._redis.setex(self._redis_key(scope_key, turn_id), ttl, encoded)
        except Exception as exc:
            log.warning("Redis pending turn put failed: %s", exc)
            return False
        return True

    def get(self, scope_key: str, turn_id: str) -> Optional[PendingTurnResult]:
        if not scope_key or not turn_id:
            return None
        try:
            raw = self._redis.get(self._redis_key(scope_key, turn_id))
        except Exception as exc:
            log.warning("Redis pending turn get failed: %s", exc)
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            return PendingTurnResult(
                scope_key=scope_key,
                turn_id=turn_id,
                lease_token=str(payload.get("lease_token") or ""),
                result_digest=str(payload.get("result_digest") or ""),
                result=payload.get("result"),
                created_at=float(payload.get("created_at") or time.time()),
            )
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            try:
                self._redis.delete(self._redis_key(scope_key, turn_id))
            except Exception:
                pass
            return None

    def delete(self, scope_key: str, turn_id: str) -> bool:
        if not scope_key or not turn_id:
            return False
        try:
            return bool(self._redis.delete(self._redis_key(scope_key, turn_id)))
        except Exception as exc:
            log.warning("Redis pending turn delete failed: %s", exc)
            return False

    def clear_by_prefix(self, prefix: str) -> int:
        if not prefix:
            return 0
        pattern = f"{self._key_prefix}:{prefix}*"
        return redis_scan_delete(self._redis, pattern)

    def clear_all(self) -> None:
        redis_scan_delete(self._redis, f"{self._key_prefix}:*")

    def _redis_key(self, scope_key: str, turn_id: str) -> str:
        return f"{self._key_prefix}:{scope_key}:{turn_id}"


_store: Optional[PendingTurnResultStore] = None
_store_lock = threading.Lock()


def _build_default_store() -> PendingTurnResultStore:
    if configured_state_backend() == "redis":
        try:
            return RedisPendingTurnResultStore()
        except Exception as exc:
            log.warning(
                "Redis pending turn store unavailable, falling back to memory: %s",
                exc,
            )
    return PendingTurnResultStore()


def get_pending_turn_store() -> PendingTurnResultStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = _build_default_store()
    return _store


def reset_pending_turn_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.clear_all()
        _store = None


def configure_pending_turn_store(store: Optional[PendingTurnResultStore]) -> None:
    """Inject a store instance (used by tests)."""
    global _store
    with _store_lock:
        _store = store


def pending_turn_store_backend() -> str:
    return getattr(get_pending_turn_store(), "backend", "memory")
