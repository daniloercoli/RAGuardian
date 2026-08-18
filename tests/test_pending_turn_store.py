"""Tests for the lease-scoped :class:`PendingTurnResultStore`.

Covers the in-process memory backend:

* ``put`` / ``get`` round-trip and immutability of the returned snapshot.
* Required-field validation (scope, turn, lease token).
* TTL expiry purges entries on access.
* ``delete`` removes a single entry.
* ``clear_by_prefix`` removes only matching scope prefixes.
* ``clear_all`` wipes the store.
* The ``backend`` property reports ``"memory"``.
"""

from __future__ import annotations

import time

from app.utils.pending_turn_store import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_TTL_SECONDS,
    PendingTurnResult,
    PendingTurnResultStore,
    RedisPendingTurnResultStore,
)


def _put(
    store: PendingTurnResultStore,
    scope_key: str = "ws:default",
    turn_id: str = "turn-1",
    lease_token: str = "lease-abc",
    result_digest: str = "digest-xyz",
    result: object | None = None,
) -> bool:
    return store.put(
        scope_key,
        turn_id,
        lease_token=lease_token,
        result_digest=result_digest,
        result=result if result is not None else {"answer": "staged"},
    )


class TestPutAndGet:
    def test_put_then_get_returns_snapshot(self):
        store = PendingTurnResultStore()
        assert _put(store, result={"answer": "staged"}) is True
        snapshot = store.get("ws:default", "turn-1")
        assert snapshot is not None
        assert isinstance(snapshot, PendingTurnResult)
        assert snapshot.scope_key == "ws:default"
        assert snapshot.turn_id == "turn-1"
        assert snapshot.lease_token == "lease-abc"
        assert snapshot.result_digest == "digest-xyz"
        assert snapshot.result == {"answer": "staged"}
        assert snapshot.created_at > 0

    def test_get_missing_returns_none(self):
        store = PendingTurnResultStore()
        assert store.get("ws:default", "turn-1") is None

    def test_put_rejects_empty_scope(self):
        store = PendingTurnResultStore()
        assert _put(store, scope_key="") is False
        assert store.get("", "turn-1") is None

    def test_put_rejects_empty_turn(self):
        store = PendingTurnResultStore()
        assert _put(store, turn_id="") is False
        assert store.get("ws:default", "") is None

    def test_put_rejects_empty_lease_token(self):
        store = PendingTurnResultStore()
        assert store.put(
            "ws:default",
            "turn-1",
            lease_token="",
            result_digest="d",
            result={"a": 1},
        ) is False

    def test_get_rejects_empty_scope_or_turn(self):
        store = PendingTurnResultStore()
        _put(store)
        assert store.get("", "turn-1") is None
        assert store.get("ws:default", "") is None

    def test_put_overwrites_previous_entry(self):
        store = PendingTurnResultStore()
        _put(store, result={"answer": "first"})
        _put(store, result={"answer": "second"})
        snapshot = store.get("ws:default", "turn-1")
        assert snapshot.result == {"answer": "second"}

    def test_stale_lease_cannot_overwrite_new_owner_payload(self):
        store = PendingTurnResultStore()
        assert _put(
            store,
            lease_token="new-owner",
            result={"answer": "new"},
        ) is True
        assert _put(
            store,
            lease_token="stale-owner",
            result={"answer": "stale"},
        ) is True

        assert store.get(
            "ws:default", "turn-1", lease_token="new-owner"
        ).result == {"answer": "new"}
        assert store.get(
            "ws:default", "turn-1", lease_token="stale-owner"
        ).result == {"answer": "stale"}
        assert store.delete_if_lease(
            "ws:default", "turn-1", "stale-owner"
        ) is True
        assert store.get(
            "ws:default", "turn-1", lease_token="new-owner"
        ).result == {"answer": "new"}

    def test_entries_are_keyed_by_scope_and_turn(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws:a", turn_id="t1", result={"a": 1})
        _put(store, scope_key="ws:b", turn_id="t1", result={"b": 2})
        assert store.get("ws:a", "t1").result == {"a": 1}
        assert store.get("ws:b", "t1").result == {"b": 2}

    def test_backend_property_reports_memory(self):
        store = PendingTurnResultStore()
        assert store.backend == "memory"

    def test_default_ttl_constant(self):
        assert DEFAULT_TTL_SECONDS == 6 * 60 * 60

    def test_default_payload_and_store_limits_are_bounded(self):
        assert DEFAULT_MAX_PAYLOAD_BYTES == 2 * 1024 * 1024
        assert DEFAULT_MAX_TOTAL_BYTES == 64 * 1024 * 1024

    def test_put_rejects_unserializable_result(self):
        store = PendingTurnResultStore()

        assert _put(store, result={"bad": object()}) is False
        assert store.get("ws:default", "turn-1") is None

    def test_put_rejects_oversized_payload_without_replacing_existing(self):
        store = PendingTurnResultStore(
            max_payload_bytes=300,
            max_total_bytes=1_000,
        )
        assert _put(store, result={"answer": "small"}) is True

        assert _put(store, result={"answer": "x" * 1_000}) is False
        assert store.get("ws:default", "turn-1").result == {
            "answer": "small"
        }

    def test_result_is_detached_from_mutable_caller_payload(self):
        store = PendingTurnResultStore()
        result = {"answer": "staged", "sources": []}
        assert _put(store, result=result) is True

        result["answer"] = "mutated"
        result["sources"].append({"source": "huge" * 10_000})

        assert store.get("ws:default", "turn-1").result == {
            "answer": "staged",
            "sources": [],
        }

    def test_total_quota_rejects_new_entry_and_delete_reclaims_capacity(self):
        store = PendingTurnResultStore(
            max_payload_bytes=1_000,
            max_total_bytes=320,
        )
        assert _put(
            store,
            scope_key="ws:a",
            turn_id="t1",
            result={"answer": "a" * 80},
        ) is True
        assert _put(
            store,
            scope_key="ws:b",
            turn_id="t2",
            result={"answer": "b" * 80},
        ) is False
        assert store.get("ws:a", "t1") is not None
        assert store.get("ws:b", "t2") is None

        assert store.delete("ws:a", "t1") is True
        assert _put(
            store,
            scope_key="ws:b",
            turn_id="t2",
            result={"answer": "b" * 80},
        ) is True

    def test_overwrite_accounts_for_replaced_payload_only_once(self):
        store = PendingTurnResultStore(
            max_payload_bytes=1_000,
            max_total_bytes=260,
        )
        assert _put(store, result={"answer": "a" * 80}) is True
        assert _put(store, result={"answer": "b" * 80}) is True
        assert store.get("ws:default", "turn-1").result == {
            "answer": "b" * 80
        }


class TestTtlExpiry:
    def test_expired_entry_is_purged_on_get(self):
        store = PendingTurnResultStore(ttl_seconds=1)
        _put(store)
        snapshot = store.get("ws:default", "turn-1")
        assert snapshot is not None
        time.sleep(1.1)
        assert store.get("ws:default", "turn-1") is None

    def test_expired_entry_is_purged_on_put(self):
        store = PendingTurnResultStore(ttl_seconds=1)
        _put(store, scope_key="ws:a", turn_id="t1")
        time.sleep(1.1)
        _put(store, scope_key="ws:b", turn_id="t2")
        assert store.get("ws:a", "t1") is None
        assert store.get("ws:b", "t2") is not None

    def test_custom_ttl_on_put_overrides_default(self):
        store = PendingTurnResultStore(ttl_seconds=3600)
        _put(store)
        assert store.put(
            "ws:default",
            "turn-2",
            lease_token="lease",
            result_digest="d",
            result={"a": 1},
            ttl_seconds=1,
        ) is True
        time.sleep(1.1)
        assert store.get("ws:default", "turn-2") is None
        assert store.get("ws:default", "turn-1") is not None


class TestDelete:
    def test_delete_removes_entry(self):
        store = PendingTurnResultStore()
        _put(store)
        assert store.delete("ws:default", "turn-1") is True
        assert store.get("ws:default", "turn-1") is None

    def test_delete_missing_returns_false(self):
        store = PendingTurnResultStore()
        assert store.delete("ws:default", "turn-1") is False

    def test_delete_rejects_empty_scope_or_turn(self):
        store = PendingTurnResultStore()
        _put(store)
        assert store.delete("", "turn-1") is False
        assert store.delete("ws:default", "") is False

    def test_delete_does_not_touch_other_entries(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws:a", turn_id="t1")
        _put(store, scope_key="ws:b", turn_id="t1")
        assert store.delete("ws:a", "t1") is True
        assert store.get("ws:a", "t1") is None
        assert store.get("ws:b", "t1") is not None

    def test_delete_if_lease_preserves_new_owner_payload(self):
        store = PendingTurnResultStore()
        _put(store, lease_token="new-owner")

        assert store.delete_if_lease(
            "ws:default", "turn-1", "stale-owner"
        ) is False
        assert store.get("ws:default", "turn-1").lease_token == "new-owner"
        assert store.delete_if_lease(
            "ws:default", "turn-1", "new-owner"
        ) is True
        assert store.get("ws:default", "turn-1") is None


class TestClearByPrefix:
    def test_clear_by_prefix_removes_matching_entries(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws-a:default:conv-1")
        _put(store, scope_key="ws-a:default:conv-2")
        _put(store, scope_key="ws-b:default:conv-1")
        assert store.clear_by_prefix("ws-a:default") == 2
        assert store.get("ws-a:default:conv-1", "turn-1") is None
        assert store.get("ws-a:default:conv-2", "turn-1") is None
        assert store.get("ws-b:default:conv-1", "turn-1") is not None

    def test_clear_by_prefix_rejects_empty(self):
        store = PendingTurnResultStore()
        _put(store)
        assert store.clear_by_prefix("") == 0
        assert store.get("ws:default", "turn-1") is not None

    def test_clear_by_prefix_returns_zero_when_no_match(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws-a:default")
        assert store.clear_by_prefix("ws-b") == 0
        assert store.get("ws-a:default", "turn-1") is not None


class TestClearScope:
    def test_clear_scope_removes_only_exact_scope(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws:default:conv-1", turn_id="t1")
        _put(store, scope_key="ws:default:conv-1", turn_id="t2")
        _put(store, scope_key="ws:default:conv-10", turn_id="t1")

        assert store.clear_scope("ws:default:conv-1") == 2
        assert store.get("ws:default:conv-1", "t1") is None
        assert store.get("ws:default:conv-1", "t2") is None
        assert store.get("ws:default:conv-10", "t1") is not None

    def test_clear_scope_rejects_empty(self):
        store = PendingTurnResultStore()
        _put(store)
        assert store.clear_scope("") == 0
        assert store.get("ws:default", "turn-1") is not None


class TestClearAll:
    def test_clear_all_wipes_everything(self):
        store = PendingTurnResultStore()
        _put(store, scope_key="ws:a", turn_id="t1")
        _put(store, scope_key="ws:b", turn_id="t2")
        store.clear_all()
        assert store.get("ws:a", "t1") is None
        assert store.get("ws:b", "t2") is None

    def test_clear_all_on_empty_store_is_noop(self):
        store = PendingTurnResultStore()
        store.clear_all()
        assert store.get("ws:default", "turn-1") is None


class _QuotaRedis:
    """Small Redis double implementing the pending-store Lua contracts."""

    def __init__(self):
        self.values = {}
        self.indexes = {}
        self.eval_calls = 0

    def eval(self, _script, key_count, *arguments):
        self.eval_calls += 1
        keys = arguments[:key_count]
        argv = arguments[key_count:]
        if len(argv) == 3:
            value_key, index_key, turn_index_key = keys
            index = self.indexes.setdefault(index_key, set())
            encoded, _ttl, max_total_bytes = argv
            index.intersection_update(self.values)
            current_total = sum(len(self.values[key]) for key in index)
            previous_size = len(self.values[value_key]) if value_key in index else 0
            if current_total - previous_size + len(encoded) > int(max_total_bytes):
                return 0
            self.values[value_key] = encoded
            index.add(value_key)
            self.indexes.setdefault(turn_index_key, set()).add(value_key)
            return 1

        if len(argv) == 1:
            value_key, index_key, turn_index_key = keys
            index = self.indexes.setdefault(index_key, set())
            raw = self.values.get(value_key)
            if raw is None:
                return 0
            import json

            payload = json.loads(raw.decode("utf-8"))
            if payload.get("lease_token") != argv[0]:
                return 0
            removed = int(value_key in self.values)
            self.values.pop(value_key, None)
            index.discard(value_key)
            self.indexes.setdefault(turn_index_key, set()).discard(value_key)
            return removed

        index_key, turn_index_key = keys
        index = self.indexes.setdefault(index_key, set())
        members = set(self.indexes.get(turn_index_key, set()))
        removed = 0
        for value_key in members:
            removed += int(value_key in self.values)
            self.values.pop(value_key, None)
            index.discard(value_key)
        self.indexes.pop(turn_index_key, None)
        return removed

    def get(self, key):
        return self.values.get(key)

    def smembers(self, key):
        return set(self.indexes.get(key, set()))

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def srem(self, key, member):
        existed = member in self.indexes.get(key, set())
        self.indexes.setdefault(key, set()).discard(member)
        return int(existed)


class TestRedisQuotas:
    def test_redis_stale_lease_cannot_clobber_new_owner(self):
        redis = _QuotaRedis()
        store = RedisPendingTurnResultStore(
            redis_client=redis,
            key_prefix="test:pending",
        )

        assert _put(
            store,
            lease_token="new-owner",
            result={"answer": "new"},
        ) is True
        assert _put(
            store,
            lease_token="stale-owner",
            result={"answer": "stale"},
        ) is True
        assert store.delete_if_lease(
            "ws:default", "turn-1", "stale-owner"
        ) is True
        assert store.get(
            "ws:default", "turn-1", lease_token="new-owner"
        ).result == {"answer": "new"}

    def test_redis_rejects_oversized_payload_before_writing(self):
        redis = _QuotaRedis()
        store = RedisPendingTurnResultStore(
            redis_client=redis,
            key_prefix="test:pending",
            max_payload_bytes=200,
            max_total_bytes=1_000,
        )

        assert _put(store, result={"answer": "x" * 500}) is False
        assert redis.eval_calls == 0
        assert store.get("ws:default", "turn-1") is None

    def test_redis_total_quota_is_shared_across_scopes(self):
        redis = _QuotaRedis()
        store = RedisPendingTurnResultStore(
            redis_client=redis,
            key_prefix="test:pending",
            max_payload_bytes=1_000,
            max_total_bytes=320,
        )

        assert _put(
            store,
            scope_key="ws:a",
            turn_id="t1",
            result={"answer": "a" * 80},
        ) is True
        assert _put(
            store,
            scope_key="ws:b",
            turn_id="t2",
            result={"answer": "b" * 80},
        ) is False

        assert store.delete("ws:a", "t1") is True
        assert _put(
            store,
            scope_key="ws:b",
            turn_id="t2",
            result={"answer": "b" * 80},
        ) is True


class TestSingletonHelpers:
    def test_get_pending_turn_store_returns_singleton(self):
        from app.utils.pending_turn_store import (
            get_pending_turn_store,
            reset_pending_turn_store,
        )

        reset_pending_turn_store()
        store_a = get_pending_turn_store()
        store_b = get_pending_turn_store()
        assert store_a is store_b
        reset_pending_turn_store()

    def test_configure_pending_turn_store_injects_instance(self):
        from app.utils.pending_turn_store import (
            configure_pending_turn_store,
            get_pending_turn_store,
            reset_pending_turn_store,
        )

        reset_pending_turn_store()
        custom = PendingTurnResultStore(ttl_seconds=10)
        configure_pending_turn_store(custom)
        assert get_pending_turn_store() is custom
        configure_pending_turn_store(None)
        reset_pending_turn_store()

    def test_reset_pending_turn_store_clears_and_drops_singleton(self):
        from app.utils.pending_turn_store import (
            get_pending_turn_store,
            reset_pending_turn_store,
        )

        store = get_pending_turn_store()
        _put(store)
        reset_pending_turn_store()
        store_after = get_pending_turn_store()
        assert store_after.get("ws:default", "turn-1") is None
        reset_pending_turn_store()

    def test_pending_turn_store_backend_reports_memory(self):
        from app.utils.pending_turn_store import (
            pending_turn_store_backend,
            reset_pending_turn_store,
        )

        reset_pending_turn_store()
        assert pending_turn_store_backend() == "memory"
        reset_pending_turn_store()

    def test_pending_turn_ttl_is_configurable_from_environment(self, monkeypatch):
        from app.utils.pending_turn_store import (
            get_pending_turn_store,
            reset_pending_turn_store,
        )

        reset_pending_turn_store()
        monkeypatch.setenv("RAG_PENDING_TURN_RESULT_TTL_SECONDS", "17")
        store = get_pending_turn_store()
        assert store.ttl_seconds == 17
        reset_pending_turn_store()

    def test_pending_turn_quotas_are_configurable_from_environment(
        self,
        monkeypatch,
    ):
        from app.utils.pending_turn_store import (
            get_pending_turn_store,
            reset_pending_turn_store,
        )

        reset_pending_turn_store()
        monkeypatch.setenv("RAG_PENDING_TURN_RESULT_MAX_PAYLOAD_BYTES", "1234")
        monkeypatch.setenv("RAG_PENDING_TURN_RESULT_MAX_TOTAL_BYTES", "5678")
        store = get_pending_turn_store()
        assert store.max_payload_bytes == 1234
        assert store.max_total_bytes == 5678
        reset_pending_turn_store()
