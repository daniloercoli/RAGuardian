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
    DEFAULT_TTL_SECONDS,
    PendingTurnResult,
    PendingTurnResultStore,
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
