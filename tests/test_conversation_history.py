"""Tests for the durable :class:`ConversationHistoryStore`.

Covers the "Store" section of the conversation-history roadmap test plan:
fresh schema validation and idempotent ``ensure_schema``, atomic user+assistant
append, idempotent retries, turn reservations / lease / conflicts, linear
parent continuity, monotonic Knowledge-Base union, title derivation, rename,
archive/unarchive, hard-delete cascade, message cursor pagination, summary
compare-and-swap and quota / size limits.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from app.utils.conversation_history_store import (
    SCHEMA_VERSION,
    ConversationArchivedError,
    ConversationHistoryError,
    ConversationHistoryStore,
    IncompatibleConversationSchemaError,
    ConversationNotFoundError,
    ContinuityError,
    QuotaExceededError,
    TITLE_RENAME_MAX_LEN,
    TurnConflictError,
    TurnInProgressError,
    ensure_schema,
    get_history_connection,
)


FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


def _fingerprint(seed: str = "a") -> str:
    return (seed * 64)[:64]


def _make_store(
    tmp_path,
    *,
    workspace_id: str = "test-workspace",
    max_conversations: int = 200,
    max_pending_turns: int = 100,
    max_messages_per_conversation: int = 2000,
    max_conversation_bytes: int = 33_554_432,
    max_message_chars: int = 50_000,
    max_sources_bytes_per_turn: int = 262_144,
    max_metadata_bytes_per_turn: int = 65_536,
    max_history_bytes: int = 268_435_456,
    lease_seconds: int = 900,
    retention_days: int = 90,
    incomplete_turn_retention_days: int = 7,
) -> ConversationHistoryStore:
    workspace_data_dir = tmp_path / "workspaces"
    workspace_data_dir.mkdir(exist_ok=True)
    return ConversationHistoryStore(
        workspace_id=workspace_id,
        workspace_data_dir=str(workspace_data_dir),
        max_conversations=max_conversations,
        max_pending_turns=max_pending_turns,
        max_messages_per_conversation=max_messages_per_conversation,
        max_conversation_bytes=max_conversation_bytes,
        max_message_chars=max_message_chars,
        max_sources_bytes_per_turn=max_sources_bytes_per_turn,
        max_metadata_bytes_per_turn=max_metadata_bytes_per_turn,
        max_history_bytes=max_history_bytes,
        lease_seconds=lease_seconds,
        retention_days=retention_days,
        incomplete_turn_retention_days=incomplete_turn_retention_days,
    )


def _begin(store, *, scope_key="ws:default:conv-1", turn_id="turn-1", parent=None,
           fingerprint=FINGERPRINT, scope_kind="default", client_id="conv-1",
           **kwargs):
    return store.begin_turn(
        client_conversation_id=client_id,
        scope_key=scope_key,
        scope_kind=scope_kind,
        turn_id=turn_id,
        parent_turn_id=parent,
        request_fingerprint=fingerprint,
        **kwargs,
    )


def _complete(store, *, scope_key="ws:default:conv-1", turn_id="turn-1",
              lease_token, fingerprint=FINGERPRINT, user="Ciao", assistant="Salve",
              sources=None, metadata=None, kb_ids=None, **kwargs):
    return store.complete_turn(
        scope_key=scope_key,
        turn_id=turn_id,
        lease_token=lease_token,
        request_fingerprint=fingerprint,
        user_content=user,
        assistant_content=assistant,
        sources=sources,
        metadata=metadata,
        selected_knowledge_base_ids=kb_ids,
        **kwargs,
    )


def _do_turn(store, *, scope_key="ws:default:conv-1", turn_id="turn-1", parent=None,
             fingerprint=FINGERPRINT, user="Ciao", assistant="Salve",
             sources=None, metadata=None, kb_ids=None, **kwargs):
    began = _begin(store, scope_key=scope_key, turn_id=turn_id, parent=parent,
                   fingerprint=fingerprint, **kwargs)
    return _complete(store, scope_key=scope_key, turn_id=turn_id,
                     lease_token=began["lease_token"], fingerprint=fingerprint,
                     user=user, assistant=assistant, sources=sources,
                     metadata=metadata, kb_ids=kb_ids)


# ---------------------------------------------------------------------------
# Fresh schema initialization
# ---------------------------------------------------------------------------

class TestSchema:
    def test_ensure_schema_creates_tables_and_sets_user_version(self, tmp_path):
        path = tmp_path / "conversations.db"
        ensure_schema(path)
        conn = get_history_connection(path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
        finally:
            conn.close()
        assert version == SCHEMA_VERSION
        assert {
            "conversations",
            "conversation_knowledge_bases",
            "turn_requests",
            "messages",
            "conversation_artifact_cleanup_outbox",
        } == names

    def test_ensure_schema_is_idempotent(self, tmp_path):
        path = tmp_path / "conversations.db"
        ensure_schema(path)
        ensure_schema(path)
        conn = get_history_connection(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_store_constructor_initializes_current_schema(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.path.exists()
        conn = get_history_connection(store.path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_database_file_has_secure_permissions(self, tmp_path):
        store = _make_store(tmp_path)
        mode = os.stat(store.path).st_mode & 0o777
        assert mode == 0o600

    def test_previous_schema_version_is_rejected_without_upgrade(self, tmp_path):
        path = tmp_path / "conversations.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE old_conversations (id TEXT)")
            conn.execute("PRAGMA user_version = 2")

        with pytest.raises(IncompatibleConversationSchemaError):
            ensure_schema(path)

    def test_linear_parent_partial_unique_index_exists(self, tmp_path):
        store = _make_store(tmp_path)
        conn = get_history_connection(store.path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            names = {r[0] for r in rows}
        finally:
            conn.close()
        assert "idx_turn_requests_linear_parent" in names

    def test_foreign_keys_enforced(self, tmp_path):
        store = _make_store(tmp_path)
        conn = sqlite3.connect(str(store.path), timeout=30)
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (conversation_id, turn_id, role, content, "
                "sequence, created_at) VALUES ('nope', 't1', 'user', 'x', 1, 0)"
            )
        conn.close()


# ---------------------------------------------------------------------------
# Turn lifecycle: begin / ready / complete / replay / conflict
# ---------------------------------------------------------------------------

class TestBeginTurn:
    def test_new_reservation_creates_conversation_stub_and_returns_lease(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        assert began["status"] == "new"
        assert began["lease_token"]
        assert began["lease_expires_at"] > time.time()
        conv = began["conversation"]
        assert conv["scope_key"] == "ws:default:conv-1"
        assert conv["message_count"] == 0
        # Stub is not yet a real conversation: list excludes message_count=0.
        items, _ = store.list()
        assert items == []

    def test_repeated_begin_same_fingerprint_while_generating_returns_in_progress(self, tmp_path):
        store = _make_store(tmp_path)
        first = _begin(store)
        with pytest.raises(TurnInProgressError) as exc_info:
            _begin(store)
        assert exc_info.value.code == "turn_in_progress"
        assert first["lease_token"]  # original lease preserved

    def test_different_fingerprint_same_turn_id_raises_conflict(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store, fingerprint=_fingerprint("a"))
        with pytest.raises(TurnConflictError) as exc_info:
            _begin(store, fingerprint=_fingerprint("b"))
        assert exc_info.value.code == "turn_id_conflict"

    def test_complete_turn_replay_returns_saved_messages_without_duplicates(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        result = _complete(store, lease_token=began["lease_token"],
                           user="Domanda", assistant="Risposta")
        assert result["user_sequence"] == 1
        assert result["assistant_sequence"] == 2
        assert result["message_count"] == 2

        # begin on the same turn now reports complete (replay).
        replay = _begin(store)
        assert replay["status"] == "complete"
        assert replay["replayed"] is True
        assert len(replay["messages"]) == 2
        assert [m["role"] for m in replay["messages"]] == ["user", "assistant"]

        # complete_turn again is also a replay (no duplicate rows).
        replay_complete = _complete(store, lease_token=began["lease_token"],
                                    user="Domanda", assistant="Risposta")
        assert replay_complete["replayed"] is True
        messages, _ = store.list_messages(began["conversation"]["id"])
        assert len(messages) == 2

    def test_ready_reservation_replays_without_provider(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        assert store.mark_turn_ready("ws:default:conv-1", "turn-1",
                                     began["lease_token"], "digest-abc") is True
        replay = _begin(store)
        assert replay["status"] == "ready"
        assert replay["replayed"] is True
        assert replay["result_digest"] == "digest-abc"
        assert replay["lease_token"] == began["lease_token"]

    def test_ready_turn_with_lost_payload_can_be_recovered(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        store.mark_turn_ready(
            "ws:default:conv-1", "turn-1", began["lease_token"], "digest-abc"
        )

        recovered = store.recover_ready_turn(
            "ws:default:conv-1",
            "turn-1",
            request_fingerprint=FINGERPRINT,
            expected_lease_token=began["lease_token"],
        )

        assert recovered["status"] == "new"
        assert recovered["lease_token"] != began["lease_token"]
        turn = store.get_turn("ws:default:conv-1", "turn-1")
        assert turn["status"] == "generating"
        assert turn["result_digest"] is None

    def test_failed_retry_rechecks_parent_after_replacement_completed(self, tmp_path):
        store = _make_store(tmp_path)
        first = _begin(store, turn_id="t1")
        _complete(store, turn_id="t1", lease_token=first["lease_token"])
        failed = _begin(store, turn_id="t2", parent="t1")
        store.fail_turn(
            "ws:default:conv-1", "t2", failed["lease_token"]
        )
        replacement = _begin(store, turn_id="t3", parent="t1")
        _complete(store, turn_id="t3", lease_token=replacement["lease_token"])

        with pytest.raises(ContinuityError) as exc_info:
            _begin(store, turn_id="t2", parent="t1")
        assert exc_info.value.expected_parent_turn_id == "t3"

    def test_failed_reservation_can_be_reopened_with_same_fingerprint(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        assert store.fail_turn("ws:default:conv-1", "turn-1",
                                began["lease_token"]) is True
        reopened = _begin(store)
        assert reopened["status"] == "new"
        assert reopened["lease_token"] != began["lease_token"]

    def test_expired_lease_can_be_taken_over_with_same_fingerprint(self, tmp_path):
        store = _make_store(tmp_path, lease_seconds=1)
        began = _begin(store)
        # Force expiry.
        conn = get_history_connection(store.path)
        try:
            conn.execute(
                "UPDATE turn_requests SET lease_expires_at = ? "
                "WHERE conversation_id = ? AND turn_id = ?",
                (time.time() - 10, began["conversation"]["id"], "turn-1"),
            )
            conn.commit()
        finally:
            conn.close()
        takeover = _begin(store)
        assert takeover["status"] == "new"
        assert takeover["lease_token"] != began["lease_token"]

    def test_invalid_scope_kind_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ConversationHistoryError):
            store.begin_turn(
                client_conversation_id="c", scope_key="k", scope_kind="bogus",
                turn_id="t", parent_turn_id=None, request_fingerprint=FINGERPRINT,
            )

    def test_oversized_fingerprint_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ConversationHistoryError):
            store.begin_turn(
                client_conversation_id="c", scope_key="k", scope_kind="default",
                turn_id="t", parent_turn_id=None, request_fingerprint="a" * 65,
            )


class TestMarkReadyAndComplete:
    def test_mark_ready_requires_ownership(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store)
        assert store.mark_turn_ready("ws:default:conv-1", "turn-1", "wrong", "d") is False

    def test_mark_ready_rejects_foreign_turn(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store)
        assert store.mark_turn_ready("ws:default:conv-1", "other", "any", "d") is False

    def test_complete_requires_ownership(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store)
        with pytest.raises(TurnConflictError):
            _complete(store, lease_token="wrong")

    def test_complete_requires_matching_fingerprint(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store, fingerprint=_fingerprint("a"))
        with pytest.raises(TurnConflictError):
            _complete(store, lease_token=began["lease_token"],
                      fingerprint=_fingerprint("b"))

    def test_complete_atomic_user_and_assistant_insert(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        result = _complete(store, lease_token=began["lease_token"],
                          user="Domanda", assistant="Risposta",
                          sources=[{"id": "s1", "title": "Doc"}],
                          metadata={"latency_ms": 12})
        conv = store.get(began["conversation"]["id"])
        assert conv["message_count"] == 2
        assert conv["payload_bytes"] > 0
        assert conv["last_turn_id"] == "turn-1"
        messages, _ = store.list_messages(conv["id"])
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Domanda"
        assert messages[0]["sources"] == []
        assert messages[1]["role"] == "assistant"
        assert messages[1]["sources"] == [{"id": "s1", "title": "Doc"}]
        assert messages[1]["metadata"] == {"latency_ms": 12}

    def test_complete_turn_advances_sequence_and_message_count(self, tmp_path):
        store = _make_store(tmp_path)
        r1 = _do_turn(store, turn_id="t1", user="A", assistant="A1")
        r2 = _do_turn(store, turn_id="t2", parent="t1", user="B", assistant="B1")
        assert r1["user_sequence"] == 1
        assert r1["assistant_sequence"] == 2
        assert r2["user_sequence"] == 3
        assert r2["assistant_sequence"] == 4
        conv_id = store.get_by_scope_key("ws:default:conv-1")["id"]
        conv = store.get(conv_id)
        assert conv["message_count"] == 4

    def test_fail_turn_excludes_messages_from_persistent_history(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        assert store.fail_turn("ws:default:conv-1", "turn-1", began["lease_token"]) is True
        conv = store.get_by_scope_key("ws:default:conv-1")
        assert conv["message_count"] == 0
        messages, _ = store.list_messages(conv["id"])
        assert messages == []
        # The conversation stub stays (no messages) and is excluded from list().
        items, _ = store.list()
        assert items == []

    def test_fail_turn_cannot_fail_complete_turn(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        assert store.fail_turn("ws:default:conv-1", "turn-1",
                               began["lease_token"]) is False

    def test_renew_lease_requires_ownership(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store)
        assert store.renew_turn_lease("ws:default:conv-1", "turn-1", "wrong") is False

    def test_renew_lease_extends_expiry(self, tmp_path):
        store = _make_store(tmp_path, lease_seconds=100)
        began = _begin(store)
        assert store.renew_turn_lease("ws:default:conv-1", "turn-1",
                                      began["lease_token"]) is True
        turn = store.get_turn("ws:default:conv-1", "turn-1")
        assert turn["lease_expires_at"] > began["lease_expires_at"]

    def test_get_turn_returns_reservation_state(self, tmp_path):
        store = _make_store(tmp_path)
        _begin(store)
        turn = store.get_turn("ws:default:conv-1", "turn-1")
        assert turn["status"] == "generating"
        assert turn["turn_id"] == "turn-1"
        assert store.get_turn("ws:default:conv-1", "missing") is None


# ---------------------------------------------------------------------------
# Linear parent / continuity
# ---------------------------------------------------------------------------

class TestContinuity:
    def test_first_turn_must_have_null_parent(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ContinuityError):
            _begin(store, parent="nonexistent")

    def test_follow_up_must_reference_last_complete_turn(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        # Wrong parent -> conflict (linear parent partial unique index).
        with pytest.raises((ContinuityError, ConversationHistoryError)):
            _begin(store, turn_id="t2", parent="t1-wrong")

    def test_second_turn_without_parent_after_first_complete_is_rejected(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        with pytest.raises(ContinuityError):
            _begin(store, turn_id="t2", parent=None)

    def test_linear_chain_allows_one_branch_at_a_time(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        began = _begin(store, turn_id="t2", parent="t1")
        # A concurrent turn t3 with parent t1 cannot start while t2 is generating.
        with pytest.raises(ConversationHistoryError):
            _begin(store, turn_id="t3", parent="t1")
        # Once t2 completes, t3 still cannot attach to t1 (must attach to t2).
        _complete(store, turn_id="t2", lease_token=began["lease_token"])
        with pytest.raises(ContinuityError):
            _begin(store, turn_id="t3", parent="t1")

    def test_continuity_error_carries_expected_parent_turn_id(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        # Wrong parent after a complete turn exists -> ContinuityError with
        # expected_parent_turn_id pointing at the last complete turn.
        with pytest.raises(ContinuityError) as exc_info:
            _begin(store, turn_id="t2", parent="t1-wrong")
        assert exc_info.value.expected_parent_turn_id == "t1"

    def test_continuity_error_expected_parent_none_when_no_complete_turn(self, tmp_path):
        store = _make_store(tmp_path)
        # No complete turn yet; supplying a parent should raise with
        # expected_parent_turn_id == None.
        with pytest.raises(ContinuityError) as exc_info:
            _begin(store, parent="nonexistent")
        assert exc_info.value.expected_parent_turn_id is None

    def test_continuity_error_when_parent_omitted_after_complete_turn(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        with pytest.raises(ContinuityError) as exc_info:
            _begin(store, turn_id="t2", parent=None)
        assert exc_info.value.expected_parent_turn_id == "t1"

    def test_reset_continuity_cleans_expired_leases(self, tmp_path):
        store = _make_store(tmp_path, lease_seconds=1)
        _do_turn(store, turn_id="t1")
        began = _begin(store, turn_id="t2", parent="t1")
        # Force the t2 lease to expire.
        conn = get_history_connection(store.path)
        try:
            conn.execute(
                "UPDATE turn_requests SET lease_expires_at = ? "
                "WHERE conversation_id = ? AND turn_id = ?",
                (time.time() - 10, began["conversation"]["id"], "t2"),
            )
            conn.commit()
        finally:
            conn.close()

        result = store.reset_continuity(began["conversation"]["id"])
        assert result["cleaned_up_leases"] == 1
        # parent_turn_id should be t1 (the last complete turn).
        assert result["parent_turn_id"] == "t1"

        # The expired t2 turn should now be failed.
        turn = store.get_turn(scope_key="ws:default:conv-1", turn_id="t2")
        assert turn["status"] == "failed"

        # A new turn can now begin with parent t1.
        began3 = _begin(store, turn_id="t3", parent="t1")
        assert began3["status"] == "new"

    def test_reset_continuity_returns_none_when_no_complete_turn(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        result = store.reset_continuity(began["conversation"]["id"])
        assert result["parent_turn_id"] is None
        assert result["cleaned_up_leases"] == 0

    def test_reset_continuity_no_op_when_no_expired_leases(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        conv = store.get_by_scope_key("ws:default:conv-1")
        result = store.reset_continuity(conv["id"])
        assert result["cleaned_up_leases"] == 0
        assert result["parent_turn_id"] == "t1"

    def test_reset_continuity_unknown_conversation_raises_not_found(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ConversationNotFoundError):
            store.reset_continuity("missing-id")


# ---------------------------------------------------------------------------
# Knowledge-Base union / selection
# ---------------------------------------------------------------------------

class TestKnowledgeBases:
    def test_union_is_monotonic_and_selection_updates(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1", kb_ids=["kb-a", "kb-b"])
        _do_turn(store, turn_id="t2", parent="t1", kb_ids=["kb-b", "kb-c"])
        conv = store.get_by_scope_key("ws:default:conv-1")
        kbs = {k["knowledge_base_id"]: k for k in conv["knowledge_base_ids"]}
        assert set(kbs) == {"kb-a", "kb-b", "kb-c"}
        assert kbs["kb-b"]["is_selected"] is True
        assert kbs["kb-c"]["is_selected"] is True
        # kb-a was deselected on the second turn but stays in the union.
        assert kbs["kb-a"]["is_selected"] is False

    def test_count_by_knowledge_base_uses_full_union(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:c1", client_id="c1",
                 turn_id="t1", kb_ids=["kb-a", "kb-b"])
        _do_turn(store, scope_key="ws:default:c2", client_id="c2",
                 turn_id="t1", kb_ids=["kb-b"])
        assert store.count_by_knowledge_base("kb-a") == 1
        assert store.count_by_knowledge_base("kb-b") == 2

    def test_delete_by_knowledge_base_cascades_multi_kb_conversations(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:c1", client_id="c1",
                 turn_id="t1", kb_ids=["kb-a", "kb-b"])
        _do_turn(store, scope_key="ws:default:c2", client_id="c2",
                 turn_id="t1", kb_ids=["kb-b"])
        deleted = store.delete_by_knowledge_base("kb-b")
        assert deleted == 2
        assert store.get_by_scope_key("ws:default:c1") is None
        assert store.get_by_scope_key("ws:default:c2") is None
        assert store.count_by_knowledge_base("kb-b") == 0

    def test_scope_keys_by_knowledge_base_supports_ephemeral_cleanup(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:c2", client_id="c2",
                 turn_id="t1", kb_ids=["kb-b"])
        _do_turn(store, scope_key="ws:default:c1", client_id="c1",
                 turn_id="t1", kb_ids=["kb-a", "kb-b"])
        _do_turn(store, scope_key="ws:default:c3", client_id="c3",
                 turn_id="t1", kb_ids=["kb-a"])

        assert store.scope_keys_by_knowledge_base("kb-b") == [
            "ws:default:c1",
            "ws:default:c2",
        ]
        assert store.scope_keys_by_knowledge_base("missing") == []
        assert store.scope_keys_by_knowledge_base("") == []


# ---------------------------------------------------------------------------
# Title, rename, archive, delete
# ---------------------------------------------------------------------------

class TestConversationMutations:
    def test_title_derived_from_first_user_message(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"],
                  user="  Ciao   mondo  ", assistant="Salve")
        conv = store.get(began["conversation"]["id"])
        assert conv["title"] == "Ciao mondo"

    def test_title_truncated_to_80_chars(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        long_user = "x" * 200
        _complete(store, lease_token=began["lease_token"],
                  user=long_user, assistant="ok")
        conv = store.get(began["conversation"]["id"])
        assert len(conv["title"]) == 80

    def test_title_not_overwritten_on_subsequent_turns(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1", user="Prima", assistant="r1")
        _do_turn(store, turn_id="t2", parent="t1", user="Seconda", assistant="r2")
        conv = store.get_by_scope_key("ws:default:conv-1")
        assert conv["title"] == "Prima"

    def test_rename_strips_and_accepts_max_length(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        title = "  " + "z" * TITLE_RENAME_MAX_LEN + "  "
        renamed = store.rename(began["conversation"]["id"], title)
        assert len(renamed["title"]) == TITLE_RENAME_MAX_LEN
        assert renamed["title"] == "z" * TITLE_RENAME_MAX_LEN

    def test_rename_rejects_oversize_title(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        too_long = "x" * (TITLE_RENAME_MAX_LEN + 1)
        with pytest.raises(ConversationHistoryError):
            store.rename(began["conversation"]["id"], too_long)

    def test_rename_rejects_empty(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        with pytest.raises(ConversationHistoryError):
            store.rename(began["conversation"]["id"], "   ")

    def test_rename_unknown_raises_not_found(self, tmp_path):
        store = _make_store(tmp_path)
        with pytest.raises(ConversationNotFoundError):
            store.rename("missing", "titolo")

    def test_archive_and_unarchive_toggle_status(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        cid = began["conversation"]["id"]
        archived = store.archive(cid)
        assert archived["status"] == "archived"
        assert archived["archived_at"] is not None
        active = store.unarchive(cid)
        assert active["status"] == "active"
        assert active["archived_at"] is None

    def test_archived_conversation_rejects_new_turns(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        conversation = store.get_by_scope_key("ws:default:conv-1")
        store.archive(conversation["id"])

        with pytest.raises(ConversationArchivedError):
            _begin(store, turn_id="t2", parent="t1")

        assert store.get_by_scope_key("ws:default:conv-1")["message_count"] == 2

    def test_archive_with_active_turn_rejects_atomic_rename(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)

        with pytest.raises(TurnInProgressError):
            store.update_conversation(
                began["conversation"]["id"],
                title="Must not commit",
                archived=True,
            )

        conversation = store.get(began["conversation"]["id"])
        assert conversation["title"] == ""
        assert conversation["status"] == "active"

    def test_delete_cascades_messages_and_turns(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        cid = began["conversation"]["id"]
        assert store.delete(cid) is True
        assert store.get(cid) is None
        conn = get_history_connection(store.path)
        try:
            msgs = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
            turns = conn.execute(
                "SELECT COUNT(*) FROM turn_requests WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
        finally:
            conn.close()
        assert msgs == 0
        assert turns == 0

    def test_delete_unknown_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.delete("missing") is False


# ---------------------------------------------------------------------------
# Read API: list / get / list_messages
# ---------------------------------------------------------------------------

class TestReadApi:
    def test_list_excludes_stubs_and_paginates(self, tmp_path):
        store = _make_store(tmp_path)
        for i in range(3):
            _do_turn(store, scope_key=f"ws:default:c{i}", client_id=f"c{i}",
                     turn_id="t1", user=f"msg {i}", assistant="r")
        items, page = store.list(page=1, per_page=2)
        assert len(items) == 2
        assert page["total"] == 3
        assert page["page_count"] == 2
        assert page["has_next"] is True
        items2, page2 = store.list(page=2, per_page=2)
        assert len(items2) == 1
        assert page2["has_prev"] is True
        assert page2["has_next"] is False

    def test_list_status_filter(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:c0", client_id="c0", turn_id="t1")
        _do_turn(store, scope_key="ws:default:c1", client_id="c1", turn_id="t1")
        cid = store.get_by_scope_key("ws:default:c0")["id"]
        store.archive(cid)
        active, _ = store.list(status="active")
        archived, _ = store.list(status="archived")
        assert {a["id"] for a in active} == {store.get_by_scope_key("ws:default:c1")["id"]}
        assert {a["id"] for a in archived} == {cid}

    def test_list_orders_by_updated_desc(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:old", client_id="old", turn_id="t1")
        time.sleep(0.01)
        _do_turn(store, scope_key="ws:default:new", client_id="new", turn_id="t1")
        items, _ = store.list()
        assert items[0]["scope_key"] == "ws:default:new"
        assert items[1]["scope_key"] == "ws:default:old"

    def test_get_returns_knowledge_bases_and_incomplete_flag(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"], kb_ids=["kb-a"])
        conv = store.get(began["conversation"]["id"])
        assert conv["knowledge_base_ids"][0]["knowledge_base_id"] == "kb-a"
        assert conv["has_incomplete_turn"] is False
        # An open reservation on a new turn sets the flag.
        _begin(store, turn_id="t2", parent="turn-1")
        conv = store.get(began["conversation"]["id"])
        assert conv["has_incomplete_turn"] is True

    def test_get_unknown_returns_none(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get("missing") is None
        assert store.get_by_scope_key("missing") is None

    def test_list_messages_cursor_pagination_descending_then_reversed(self, tmp_path):
        store = _make_store(tmp_path)
        parent = None
        conv_id = None
        for i in range(5):
            t = f"t{i}"
            r = store.begin_turn(
                client_conversation_id="conv-1", scope_key="ws:default:conv-1",
                scope_kind="default", turn_id=t, parent_turn_id=parent,
                request_fingerprint=FINGERPRINT,
            )
            conv_id = r["conversation"]["id"]
            store.complete_turn(scope_key="ws:default:conv-1", turn_id=t,
                               lease_token=r["lease_token"],
                               request_fingerprint=FINGERPRINT,
                               user_content=f"u{i}", assistant_content=f"a{i}")
            parent = t
        cid = conv_id
        first, cursor = store.list_messages(cid, limit=2)
        assert [m["sequence"] for m in first] == [9, 10]
        assert cursor == 9
        second, cursor2 = store.list_messages(cid, limit=2, before_sequence=cursor)
        assert [m["sequence"] for m in second] == [7, 8]
        assert cursor2 == 7

    def test_list_messages_limit_capped_at_200(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"])
        cid = began["conversation"]["id"]
        messages, _ = store.list_messages(cid, limit=9999)
        assert len(messages) == 2

    def test_list_messages_after_sequence_pages_forward(self, tmp_path):
        store = _make_store(tmp_path)
        first_turn = _begin(store, turn_id="t1")
        _complete(
            store,
            turn_id="t1",
            lease_token=first_turn["lease_token"],
        )
        second_turn = _begin(store, turn_id="t2", parent="t1")
        _complete(
            store,
            turn_id="t2",
            lease_token=second_turn["lease_token"],
        )
        conversation = store.get_by_scope_key("ws:default:conv-1")

        first, cursor = store.list_messages_after_sequence(
            conversation["id"], after_sequence=0, limit=2
        )
        second, final_cursor = store.list_messages_after_sequence(
            conversation["id"], after_sequence=cursor, limit=2
        )

        assert [message["sequence"] for message in first] == [1, 2]
        assert cursor == 2
        assert [message["sequence"] for message in second] == [3, 4]
        assert final_cursor is None


# ---------------------------------------------------------------------------
# Summary compare-and-swap
# ---------------------------------------------------------------------------

class TestSummaryCas:
    def test_update_summary_succeeds_with_matching_version_and_higher_sequence(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        assert store.update_summary("ws:default:conv-1", "Riassunto",
                                    expected_version=0, through_sequence=2) is True
        conv = store.get_by_scope_key("ws:default:conv-1")
        assert conv["summary"] == "Riassunto"
        assert conv["summary_version"] == 1
        assert conv["summary_through_sequence"] == 2

    def test_update_summary_rejects_stale_version(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        store.update_summary("ws:default:conv-1", "v1", expected_version=0,
                             through_sequence=2)
        assert store.update_summary("ws:default:conv-1", "stale",
                                    expected_version=0, through_sequence=2) is False
        assert store.get_by_scope_key("ws:default:conv-1")["summary"] == "v1"

    def test_update_summary_rejects_non_increasing_through_sequence(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, turn_id="t1")
        _do_turn(store, turn_id="t2", parent="t1")
        store.update_summary("ws:default:conv-1", "v1", expected_version=0,
                             through_sequence=3)
        assert store.update_summary("ws:default:conv-1", "v2",
                                    expected_version=1, through_sequence=3) is False
        assert store.update_summary("ws:default:conv-1", "v2",
                                    expected_version=1, through_sequence=2) is False

    def test_update_summary_unknown_scope_returns_false(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.update_summary("missing", "x", expected_version=0,
                                    through_sequence=1) is False


# ---------------------------------------------------------------------------
# Quota and size limits
# ---------------------------------------------------------------------------

class TestQuotaAndLimits:
    def test_message_char_limit_rejects_oversized_content(self, tmp_path):
        store = _make_store(tmp_path, max_message_chars=10)
        began = _begin(store)
        with pytest.raises(ConversationHistoryError):
            _complete(store, lease_token=began["lease_token"],
                      user="x" * 11, assistant="ok")

    def test_conversation_message_count_quota_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_messages_per_conversation=3)
        began = _begin(store)
        _complete(store, lease_token=began["lease_token"],
                  user="a", assistant="b")
        # A second turn would push message_count to 4 > 3.
        r2 = _begin(store, turn_id="t2", parent="turn-1")
        with pytest.raises(QuotaExceededError):
            _complete(store, turn_id="t2", lease_token=r2["lease_token"],
                      user="c", assistant="d")

    def test_conversation_byte_quota_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_conversation_bytes=40)
        began = _begin(store)
        with pytest.raises(QuotaExceededError):
            _complete(store, lease_token=began["lease_token"],
                      user="x" * 50, assistant="y" * 50)

    def test_history_byte_quota_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_conversation_bytes=10_000,
                            max_history_bytes=20)
        _do_turn(store, scope_key="ws:default:c1", client_id="c1", turn_id="t1",
                 user="prima", assistant="risposta")
        with pytest.raises(QuotaExceededError):
            _do_turn(store, scope_key="ws:default:c2", client_id="c2", turn_id="t1",
                     user="seconda", assistant="risposta")

    def test_sources_size_limit_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_sources_bytes_per_turn=10)
        began = _begin(store)
        with pytest.raises(ConversationHistoryError):
            _complete(store, lease_token=began["lease_token"],
                      sources=[{"id": "x" * 100}])

    def test_metadata_size_limit_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_metadata_bytes_per_turn=10)
        began = _begin(store)
        with pytest.raises(ConversationHistoryError):
            _complete(store, lease_token=began["lease_token"],
                      metadata={"k": "v" * 100})

    def test_pending_turn_quota_enforced(self, tmp_path):
        store = _make_store(tmp_path, max_pending_turns=1)
        _begin(store, scope_key="ws:default:c1", client_id="c1", turn_id="t1")
        with pytest.raises(QuotaExceededError):
            _begin(store, scope_key="ws:default:c2", client_id="c2", turn_id="t1")

    def test_failed_stub_still_counts_toward_incomplete_quota(self, tmp_path):
        store = _make_store(tmp_path, max_pending_turns=1)
        began = _begin(
            store,
            scope_key="ws:default:c1",
            client_id="c1",
            turn_id="t1",
        )
        assert store.fail_turn(
            "ws:default:c1", "t1", began["lease_token"]
        ) is True

        with pytest.raises(QuotaExceededError, match="pending turn quota"):
            _begin(
                store,
                scope_key="ws:default:c2",
                client_id="c2",
                turn_id="t1",
            )

        # Retrying the same failed reservation remains possible and can free
        # the quota by completing it.
        retried = _begin(
            store,
            scope_key="ws:default:c1",
            client_id="c1",
            turn_id="t1",
        )
        _complete(
            store,
            scope_key="ws:default:c1",
            turn_id="t1",
            lease_token=retried["lease_token"],
        )
        assert _begin(
            store,
            scope_key="ws:default:c2",
            client_id="c2",
            turn_id="t1",
        )["status"] == "new"

    def test_completed_conversation_quota_is_separate_from_pending(self, tmp_path):
        store = _make_store(
            tmp_path, max_conversations=1, max_pending_turns=10
        )
        _do_turn(
            store,
            scope_key="ws:default:c1",
            client_id="c1",
            turn_id="t1",
        )
        with pytest.raises(QuotaExceededError, match="conversation quota"):
            _begin(
                store,
                scope_key="ws:default:c2",
                client_id="c2",
                turn_id="t1",
            )

    def test_first_commit_serializes_concurrent_stub_quota(self, tmp_path):
        store = _make_store(
            tmp_path, max_conversations=1, max_pending_turns=10
        )
        first = _begin(
            store, scope_key="ws:default:c1", client_id="c1", turn_id="t1"
        )
        second = _begin(
            store, scope_key="ws:default:c2", client_id="c2", turn_id="t1"
        )
        _complete(
            store,
            scope_key="ws:default:c1",
            turn_id="t1",
            lease_token=first["lease_token"],
        )
        with pytest.raises(QuotaExceededError, match="conversation quota"):
            _complete(
                store,
                scope_key="ws:default:c2",
                turn_id="t1",
                lease_token=second["lease_token"],
            )

    def test_quota_status_reports_usage(self, tmp_path):
        store = _make_store(tmp_path, max_conversations=5, max_history_bytes=1000)
        _do_turn(store, turn_id="t1", user="abc", assistant="def")
        status = store.quota_status()
        assert status["conversations"] == 1
        assert status["max_conversations"] == 5
        assert status["bytes"] > 0
        assert status["max_bytes"] == 1000
        assert status["pending_turns"] == 0
        assert status["max_pending_turns"] == 100


class TestRetentionCleanup:
    def test_active_expired_conversation_deleted_but_archive_preserved(self, tmp_path):
        store = _make_store(tmp_path, retention_days=1)
        _do_turn(
            store,
            scope_key="ws:default:old",
            client_id="old",
            turn_id="t-old",
        )
        _do_turn(
            store,
            scope_key="ws:default:archive",
            client_id="archive",
            turn_id="t-archive",
        )
        archived = store.get_by_scope_key("ws:default:archive")
        store.archive(archived["id"])
        old_timestamp = time.time() - (2 * 86_400)
        conn = get_history_connection(store.path)
        try:
            conn.execute("UPDATE conversations SET updated_at = ?", (old_timestamp,))
            conn.commit()
        finally:
            conn.close()

        result = store.cleanup_expired(now=time.time())

        assert result["conversations_deleted"] == 1
        assert store.get_by_scope_key("ws:default:old") is None
        assert store.get_by_scope_key("ws:default:archive") is not None

    def test_expired_incomplete_turn_and_empty_stub_are_removed(self, tmp_path):
        store = _make_store(tmp_path, incomplete_turn_retention_days=1)
        began = _begin(store)
        old_timestamp = time.time() - (2 * 86_400)
        conn = get_history_connection(store.path)
        try:
            conn.execute(
                """
                UPDATE turn_requests
                   SET updated_at = ?, lease_expires_at = ?
                 WHERE conversation_id = ? AND turn_id = ?
                """,
                (
                    old_timestamp,
                    time.time() - 10,
                    began["conversation"]["id"],
                    "turn-1",
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (old_timestamp, began["conversation"]["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        result = store.cleanup_expired(now=time.time())

        assert result["expired_leases_failed"] == 1
        assert result["turns_deleted"] == 1
        assert result["conversations_deleted"] == 1
        assert store.get_by_scope_key("ws:default:conv-1") is None


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------

class TestScopeIsolation:
    def test_same_client_id_with_different_scopes_does_not_collide(self, tmp_path):
        store = _make_store(tmp_path)
        _do_turn(store, scope_key="ws:default:conv-1", client_id="conv-1",
                 turn_id="t1", scope_kind="default")
        _do_turn(store, scope_key="ws:kb:kb-1:conv-1", client_id="conv-1",
                 turn_id="t1", scope_kind="kb")
        _do_turn(store, scope_key="ws:multi-chat:conv-1", client_id="conv-1",
                 turn_id="t1", scope_kind="multi")
        default = store.get_by_scope_key("ws:default:conv-1")
        kb = store.get_by_scope_key("ws:kb:kb-1:conv-1")
        multi = store.get_by_scope_key("ws:multi-chat:conv-1")
        assert len({default["id"], kb["id"], multi["id"]}) == 3
        assert default["scope_kind"] == "default"
        assert kb["scope_kind"] == "kb"
        assert multi["scope_kind"] == "multi"

    def test_two_workspaces_are_isolated(self, tmp_path):
        workspace_data_dir_a = tmp_path / "workspaces" / "ws-a"
        workspace_data_dir_a.mkdir(parents=True, exist_ok=True)
        store_a = ConversationHistoryStore(
            workspace_id="ws-a",
            workspace_data_dir=str(workspace_data_dir_a),
        )
        workspace_data_dir_b = tmp_path / "workspaces" / "ws-b"
        workspace_data_dir_b.mkdir(parents=True, exist_ok=True)
        store_b = ConversationHistoryStore(
            workspace_id="ws-b",
            workspace_data_dir=str(workspace_data_dir_b),
        )
        _do_turn(store_a, scope_key="ws-a:default:c1", client_id="c1", turn_id="t1")
        _do_turn(store_b, scope_key="ws-b:default:c1", client_id="c1", turn_id="t1")
        assert store_a.list()[1]["total"] == 1
        assert store_b.list()[1]["total"] == 1
        assert store_a.get_by_scope_key("ws-b:default:c1") is None
        assert store_b.get_by_scope_key("ws-a:default:c1") is None


# ---------------------------------------------------------------------------
# Payload byte accounting
# ---------------------------------------------------------------------------

class TestPayloadAccounting:
    def test_payload_bytes_counts_content_sources_metadata(self, tmp_path):
        store = _make_store(tmp_path)
        began = _begin(store)
        sources = [{"id": "s1", "title": "Doc"}]
        metadata = {"latency_ms": 12}
        _complete(store, lease_token=began["lease_token"], user="abc",
                  assistant="def", sources=sources, metadata=metadata)
        conv = store.get(began["conversation"]["id"])
        expected = (
            len("abc".encode("utf-8"))
            + len("def".encode("utf-8"))
            + len(json.dumps(sources, ensure_ascii=False).encode("utf-8"))
            + len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        )
        assert conv["payload_bytes"] == expected
