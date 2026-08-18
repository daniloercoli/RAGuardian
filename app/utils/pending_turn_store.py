"""Lease-scoped pending turn result store.

When a RAG turn finishes streaming but before it is durably committed to the
:class:`ConversationHistoryStore`, the full result payload is staged here so a
client retry that arrives before the commit can replay the answer without
re-running generation. Entries are keyed by ``(scope_key, turn_id,
lease_token)`` so a worker whose lease expired cannot overwrite its successor.

Two backends are provided:

* :class:`PendingTurnResultStore` – thread-safe in-process memory (default).
* :class:`RedisPendingTurnResultStore` – Redis-backed for multi-worker deploys.

The application picks a backend via :func:`configured_state_backend` and the
singleton is exposed through :func:`get_pending_turn_store`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from utils.state_backend import (
    configured_state_backend,
    redis_connection,
    redis_scan_delete,
    state_key_prefix,
)

log = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
PENDING_RESULT_SCHEMA_VERSION = 1


def _encode_payload(
    *,
    lease_token: str,
    result_digest: str,
    result: Any,
    created_at: float,
) -> Optional[bytes]:
    """Return the canonical JSON representation used for quota accounting."""

    payload = {
        "schema_version": PENDING_RESULT_SCHEMA_VERSION,
        "lease_token": lease_token,
        "result_digest": result_digest,
        "result": result,
        "created_at": created_at,
    }
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class PendingTurnResult:
    """Immutable view of a staged turn result."""

    scope_key: str
    turn_id: str
    lease_token: str
    result_digest: str
    result: Any
    created_at: float


def _decode_pending_result(
    raw: bytes | str,
    *,
    scope_key: str,
    turn_id: str,
    fallback_created_at: float,
) -> Optional[PendingTurnResult]:
    """Validate and decode one stored payload from either backend."""

    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(text)
        schema_version = int(payload.get("schema_version") or 0)
        if schema_version != PENDING_RESULT_SCHEMA_VERSION:
            return None
        return PendingTurnResult(
            scope_key=scope_key,
            turn_id=turn_id,
            lease_token=str(payload.get("lease_token") or ""),
            result_digest=str(payload.get("result_digest") or ""),
            result=payload.get("result"),
            created_at=float(payload.get("created_at") or fallback_created_at),
        )
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
        return None


@dataclass
class _Entry:
    lease_token: str
    payload: bytes
    created_at: float
    expires_at: float
    size_bytes: int


class PendingTurnResultStore:
    """Thread-safe in-process store for pending turn results."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_payload_bytes = max(1, int(max_payload_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._total_bytes = 0

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
        ttl = max(1, int(ttl_seconds or self.ttl_seconds))
        encoded = _encode_payload(
            lease_token=lease_token,
            result_digest=result_digest,
            result=result,
            created_at=now,
        )
        if encoded is None:
            log.warning("Unable to serialize pending turn result for %s", turn_id)
            return False
        size_bytes = len(encoded)
        if size_bytes > self.max_payload_bytes:
            log.warning(
                "Pending turn result %s exceeds payload limit (%s > %s bytes)",
                turn_id,
                size_bytes,
                self.max_payload_bytes,
            )
            return False
        entry = _Entry(
            lease_token=lease_token,
            payload=encoded,
            created_at=now,
            expires_at=now + ttl,
            size_bytes=size_bytes,
        )
        with self._lock:
            self._purge_expired_locked(now)
            key = self._key(scope_key, turn_id, lease_token)
            previous = self._entries.get(key)
            previous_size = previous.size_bytes if previous is not None else 0
            projected_total = self._total_bytes - previous_size + size_bytes
            if projected_total > self.max_total_bytes:
                log.warning(
                    "Pending turn store quota exceeded (%s > %s bytes)",
                    projected_total,
                    self.max_total_bytes,
                )
                return False
            self._entries[key] = entry
            self._total_bytes = projected_total
        return True

    def get(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: Optional[str] = None,
    ) -> Optional[PendingTurnResult]:
        if not scope_key or not turn_id:
            return None
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            if lease_token:
                key = self._key(scope_key, turn_id, lease_token)
                entry = self._entries.get(key)
            else:
                prefix = self._turn_prefix(scope_key, turn_id)
                candidates = [
                    (candidate_key, candidate_entry)
                    for candidate_key, candidate_entry in self._entries.items()
                    if candidate_key.startswith(prefix)
                ]
                if not candidates:
                    return None
                key, entry = max(
                    candidates,
                    key=lambda item: item[1].created_at,
                )
            if entry is None:
                return None
            pending = _decode_pending_result(
                entry.payload,
                scope_key=scope_key,
                turn_id=turn_id,
                fallback_created_at=entry.created_at,
            )
            if pending is None:
                self._remove_locked(key)
                return None
            return pending

    def delete(self, scope_key: str, turn_id: str) -> bool:
        if not scope_key or not turn_id:
            return False
        with self._lock:
            prefix = self._turn_prefix(scope_key, turn_id)
            to_remove = [key for key in self._entries if key.startswith(prefix)]
            for key in to_remove:
                self._remove_locked(key)
            return bool(to_remove)

    def delete_if_lease(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
    ) -> bool:
        """Delete only the payload owned by ``lease_token``.

        A stale worker must never remove a result staged by the worker that
        took over its expired durable lease.
        """

        if not scope_key or not turn_id or not lease_token:
            return False
        key = self._key(scope_key, turn_id, lease_token)
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None or entry.lease_token != lease_token:
                return False
            return self._remove_locked(key)

    def clear_by_prefix(self, prefix: str) -> int:
        if not prefix:
            return 0
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            to_remove = [
                key
                for key in self._entries
                if key.partition("\x1f")[0].startswith(prefix)
            ]
            for key in to_remove:
                self._remove_locked(key)
            return len(to_remove)

    def clear_scope(self, scope_key: str) -> int:
        """Remove every staged turn belonging to one exact scope."""

        if not scope_key:
            return 0
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            prefix = f"{scope_key}\x1f"
            to_remove = [key for key in self._entries if key.startswith(prefix)]
            for key in to_remove:
                self._remove_locked(key)
            return len(to_remove)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0

    def _turn_prefix(self, scope_key: str, turn_id: str) -> str:
        return f"{scope_key}\x1f{turn_id}\x1f"

    def _key(self, scope_key: str, turn_id: str, lease_token: str) -> str:
        return f"{self._turn_prefix(scope_key, turn_id)}{lease_token}"

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for key in expired:
            self._remove_locked(key)

    def _remove_locked(self, key: str) -> bool:
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        self._total_bytes = max(0, self._total_bytes - entry.size_bytes)
        return True


class RedisPendingTurnResultStore(PendingTurnResultStore):
    """Redis-backed pending turn result store for multi-worker deployments."""

    def __init__(
        self,
        *,
        redis_client=None,
        key_prefix: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ):
        super().__init__(
            ttl_seconds=ttl_seconds,
            max_payload_bytes=max_payload_bytes,
            max_total_bytes=max_total_bytes,
        )
        self._redis = redis_client or redis_connection()
        self._key_prefix = key_prefix or f"{state_key_prefix()}:pending-turn"
        self._index_key = f"{self._key_prefix}:__index"

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
        ttl = max(1, int(ttl_seconds or self.ttl_seconds))
        encoded = _encode_payload(
            lease_token=lease_token,
            result_digest=result_digest,
            result=result,
            created_at=now,
        )
        if encoded is None:
            log.warning("Unable to serialize pending turn result for %s", turn_id)
            return False
        if len(encoded) > self.max_payload_bytes:
            log.warning(
                "Pending turn result %s exceeds payload limit (%s > %s bytes)",
                turn_id,
                len(encoded),
                self.max_payload_bytes,
            )
            return False
        script = """
        local members = redis.call('SMEMBERS', KEYS[2])
        local total = 0
        local previous = 0
        for _, member in ipairs(members) do
            local size = redis.call('STRLEN', member)
            if size == 0 then
                redis.call('SREM', KEYS[2], member)
            else
                total = total + size
                if member == KEYS[1] then previous = size end
            end
        end
        local projected = total - previous + string.len(ARGV[1])
        if projected > tonumber(ARGV[3]) then return 0 end
        redis.call('SETEX', KEYS[1], ARGV[2], ARGV[1])
        redis.call('SADD', KEYS[2], KEYS[1])
        redis.call('SADD', KEYS[3], KEYS[1])
        local index_ttl = redis.call('TTL', KEYS[2])
        if index_ttl < tonumber(ARGV[2]) then
            redis.call('EXPIRE', KEYS[2], ARGV[2])
        end
        local turn_index_ttl = redis.call('TTL', KEYS[3])
        if turn_index_ttl < tonumber(ARGV[2]) then
            redis.call('EXPIRE', KEYS[3], ARGV[2])
        end
        return 1
        """
        try:
            stored = self._redis.eval(
                script,
                3,
                self._redis_key(scope_key, turn_id, lease_token),
                self._index_key,
                self._turn_index_key(scope_key, turn_id),
                encoded,
                ttl,
                self.max_total_bytes,
            )
        except Exception as exc:
            log.warning("Redis pending turn put failed: %s", exc)
            return False
        if not stored:
            log.warning(
                "Redis pending turn store quota exceeded (%s byte limit)",
                self.max_total_bytes,
            )
            return False
        return True

    def get(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: Optional[str] = None,
    ) -> Optional[PendingTurnResult]:
        if not scope_key or not turn_id:
            return None
        try:
            if lease_token:
                value_key = self._redis_key(scope_key, turn_id, lease_token)
                raw = self._redis.get(value_key)
            else:
                members = self._redis.smembers(
                    self._turn_index_key(scope_key, turn_id)
                )
                candidates = []
                for member in members or ():
                    member_key = (
                        member.decode("utf-8")
                        if isinstance(member, bytes)
                        else str(member)
                    )
                    candidate_raw = self._redis.get(member_key)
                    if not candidate_raw:
                        continue
                    candidate = _decode_pending_result(
                        candidate_raw,
                        scope_key=scope_key,
                        turn_id=turn_id,
                        fallback_created_at=0,
                    )
                    if candidate is None:
                        continue
                    candidates.append(
                        (candidate.created_at, member_key, candidate_raw)
                    )
                if not candidates:
                    return None
                _, value_key, raw = max(candidates, key=lambda item: item[0])
        except Exception as exc:
            log.warning("Redis pending turn get failed: %s", exc)
            return None
        if not raw:
            return None
        pending = _decode_pending_result(
            raw,
            scope_key=scope_key,
            turn_id=turn_id,
            fallback_created_at=time.time(),
        )
        if pending is None:
            try:
                self._redis.delete(value_key)
                self._redis.srem(self._index_key, value_key)
                self._redis.srem(
                    self._turn_index_key(scope_key, turn_id),
                    value_key,
                )
            except Exception:
                pass
            return None
        return pending

    def delete(self, scope_key: str, turn_id: str) -> bool:
        if not scope_key or not turn_id:
            return False
        script = """
        local members = redis.call('SMEMBERS', KEYS[2])
        local removed = 0
        for _, member in ipairs(members) do
            removed = removed + redis.call('DEL', member)
            redis.call('SREM', KEYS[1], member)
        end
        redis.call('DEL', KEYS[2])
        return removed
        """
        try:
            return bool(
                self._redis.eval(
                    script,
                    2,
                    self._index_key,
                    self._turn_index_key(scope_key, turn_id),
                )
            )
        except Exception as exc:
            log.warning("Redis pending turn delete failed: %s", exc)
            return False

    def delete_if_lease(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
    ) -> bool:
        if not scope_key or not turn_id or not lease_token:
            return False
        script = """
        local raw = redis.call('GET', KEYS[1])
        if not raw then return 0 end
        local ok, value = pcall(cjson.decode, raw)
        if not ok or value['lease_token'] ~= ARGV[1] then return 0 end
        local removed = redis.call('DEL', KEYS[1])
        redis.call('SREM', KEYS[2], KEYS[1])
        redis.call('SREM', KEYS[3], KEYS[1])
        return removed
        """
        try:
            return bool(
                self._redis.eval(
                    script,
                    3,
                    self._redis_key(scope_key, turn_id, lease_token),
                    self._index_key,
                    self._turn_index_key(scope_key, turn_id),
                    lease_token,
                )
            )
        except Exception as exc:
            log.warning("Redis pending lease delete failed: %s", exc)
            return False

    def clear_by_prefix(self, prefix: str) -> int:
        if not prefix:
            return 0
        pattern = f"{self._key_prefix}:{prefix}*"
        deleted = redis_scan_delete(self._redis, pattern)
        redis_scan_delete(self._redis, f"{self._key_prefix}:__turn:{prefix}*")
        return deleted

    def clear_scope(self, scope_key: str) -> int:
        if not scope_key:
            return 0
        pattern = f"{self._key_prefix}:{scope_key}:*"
        deleted = redis_scan_delete(self._redis, pattern)
        redis_scan_delete(
            self._redis,
            f"{self._key_prefix}:__turn:{scope_key}:*",
        )
        return deleted

    def clear_all(self) -> None:
        redis_scan_delete(self._redis, f"{self._key_prefix}:*")

    def _turn_index_key(self, scope_key: str, turn_id: str) -> str:
        return f"{self._key_prefix}:__turn:{scope_key}:{turn_id}"

    def _redis_key(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
    ) -> str:
        return f"{self._key_prefix}:{scope_key}:{turn_id}:{lease_token}"


_store: Optional[PendingTurnResultStore] = None
_store_lock = threading.Lock()


def _build_default_store() -> PendingTurnResultStore:
    ttl_seconds = _positive_env_int(
        "RAG_PENDING_TURN_RESULT_TTL_SECONDS",
        DEFAULT_TTL_SECONDS,
    )
    max_payload_bytes = _positive_env_int(
        "RAG_PENDING_TURN_RESULT_MAX_PAYLOAD_BYTES",
        DEFAULT_MAX_PAYLOAD_BYTES,
    )
    max_total_bytes = _positive_env_int(
        "RAG_PENDING_TURN_RESULT_MAX_TOTAL_BYTES",
        DEFAULT_MAX_TOTAL_BYTES,
    )
    if configured_state_backend() == "redis":
        try:
            return RedisPendingTurnResultStore(
                ttl_seconds=ttl_seconds,
                max_payload_bytes=max_payload_bytes,
                max_total_bytes=max_total_bytes,
            )
        except Exception as exc:
            log.warning(
                "Redis pending turn store unavailable, falling back to memory: %s",
                exc,
            )
    return PendingTurnResultStore(
        ttl_seconds=ttl_seconds,
        max_payload_bytes=max_payload_bytes,
        max_total_bytes=max_total_bytes,
    )


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


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
