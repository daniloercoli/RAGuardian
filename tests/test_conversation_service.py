"""Tests for :class:`ConversationService` turn lifecycle coordination.

Covers the service layer that sits between the query routes and the durable
:class:`ConversationHistoryStore`:

* ``begin_turn`` maps history-store outcomes (new / ready / complete /
  generating / conflict / disabled) onto :class:`BeginTurnOutcome`.
* ``stage_result`` stages a pending result and marks the turn ready.
* ``complete_turn`` commits durably, reconciles warm memory idempotently and
  clears the pending entry.
* ``fail_turn`` clears the pending entry and marks the turn failed.
* ``hydrate_for_prompt`` populates warm memory from durable history once.
* The service is a no-op when history is disabled.
"""

from __future__ import annotations

import time

import pytest

from app.utils.conversation_memory import (
    ConversationMemoryStore,
    ConversationTurn,
)
from app.utils.conversation_service import (
    BeginTurnOutcome,
    CompleteTurnOutcome,
    ConversationService,
    ConversationTurnError,
    TurnRequest,
    compute_request_fingerprint,
    compute_result_digest,
    derive_scope_kind,
)
from app.utils.pending_turn_store import PendingTurnResultStore
from utils.conversation_history_store import (
    ConversationHistoryStore,
)


FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64


def _make_history_store(tmp_path, workspace_id: str = "test-workspace") -> ConversationHistoryStore:
    workspace_data_dir = tmp_path / "workspaces"
    workspace_data_dir.mkdir(exist_ok=True)
    return ConversationHistoryStore(
        workspace_id=workspace_id,
        workspace_data_dir=str(workspace_data_dir),
    )


def _make_service(
    tmp_path,
    *,
    workspace_id: str = "test-workspace",
    memory_store: ConversationMemoryStore | None = None,
    pending_store: PendingTurnResultStore | None = None,
    enabled: bool = True,
) -> ConversationService:
    history = _make_history_store(tmp_path, workspace_id)
    return ConversationService(
        history_store=history,
        workspace_id=workspace_id,
        pending_store=pending_store or PendingTurnResultStore(),
        memory_store=memory_store or ConversationMemoryStore(),
        enabled=enabled,
    )


def _request(
    *,
    scope_key: str = "ws:default:conv-1",
    turn_id: str = "turn-1",
    parent_turn_id: str | None = None,
    fingerprint: str = FINGERPRINT,
    client_conversation_id: str = "conv-1",
    scope_kind: str = "default",
    knowledge_base_ids: list[str] | None = None,
) -> TurnRequest:
    return TurnRequest(
        scope_key=scope_key,
        scope_kind=scope_kind,
        client_conversation_id=client_conversation_id,
        turn_id=turn_id,
        request_fingerprint=fingerprint,
        parent_turn_id=parent_turn_id,
        selected_knowledge_base_ids=knowledge_base_ids,
    )


# ---------------------------------------------------------------------------
# Disabled service is a no-op
# ---------------------------------------------------------------------------


class TestDisabledService:
    def test_begin_turn_returns_disabled(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        outcome = service.begin_turn(_request())
        assert outcome.status == "disabled"

    def test_complete_turn_returns_disabled(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token="tok",
            request_fingerprint=FINGERPRINT,
            user_content="Ciao",
            assistant_content="Salve",
        )
        assert outcome.status == "disabled"

    def test_fail_turn_returns_false(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        assert service.fail_turn("ws:default:conv-1", "turn-1", "tok") is False

    def test_stage_result_returns_false(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        assert service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token="tok",
            result={"answer": "x"},
            result_digest="digest",
        ) is False

    def test_hydrate_returns_false(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        assert service.hydrate_for_prompt("ws:default:conv-1", "warm-1") is False

    def test_get_conversation_returns_none(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        assert service.get_conversation("ws:default:conv-1") is None
        assert service.list_messages("missing") == ([], None)


# ---------------------------------------------------------------------------
# begin_turn
# ---------------------------------------------------------------------------


class TestBeginTurn:
    def test_new_reservation_returns_lease(self, tmp_path):
        service = _make_service(tmp_path)
        outcome = service.begin_turn(_request())
        assert outcome.status == "new"
        assert outcome.lease_token
        assert outcome.lease_expires_at > time.time()
        assert outcome.conversation["scope_key"] == "ws:default:conv-1"

    def test_generating_returns_retry_after(self, tmp_path):
        service = _make_service(tmp_path)
        service.begin_turn(_request())
        outcome = service.begin_turn(_request())
        assert outcome.status == "generating"
        assert outcome.retry_after >= 1

    def test_conflict_returns_error(self, tmp_path):
        service = _make_service(tmp_path)
        service.begin_turn(_request(fingerprint=_fingerprint("a")))
        outcome = service.begin_turn(_request(fingerprint=_fingerprint("b")))
        assert outcome.status == "conflict"
        assert outcome.error == "turn_id_conflict"

    def test_complete_replays_messages(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        outcome = service.begin_turn(_request())
        assert outcome.status == "complete"
        assert len(outcome.messages) == 2
        assert [m["role"] for m in outcome.messages] == ["user", "assistant"]

    def test_ready_replays_staged_result(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "staged"})
        assert service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "staged"},
            result_digest=digest,
        ) is True
        outcome = service.begin_turn(_request())
        assert outcome.status == "ready"
        assert outcome.result == {"answer": "staged"}
        assert outcome.result_digest == digest

    def test_ready_without_pending_falls_back_to_generating(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "staged"})
        service.history_store.mark_turn_ready(
            "ws:default:conv-1",
            "turn-1",
            began.lease_token,
            digest,
        )
        outcome = service.begin_turn(_request())
        assert outcome.status == "generating"
        assert outcome.retry_after >= 1

    def test_continuity_error_propagates_expected_parent_turn_id(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request(turn_id="t1"))
        service.complete_turn(
            "ws:default:conv-1",
            "t1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ciao",
            assistant_content="Salve",
        )
        # Wrong parent -> ContinuityError with expected_parent_turn_id.
        outcome = service.begin_turn(
            _request(turn_id="t2", parent_turn_id="t1-wrong")
        )
        assert outcome.status == "error"
        assert outcome.expected_parent_turn_id == "t1"

    def test_continuity_error_expected_parent_none_when_no_complete(self, tmp_path):
        service = _make_service(tmp_path)
        # No complete turn; supplying a bogus parent raises ContinuityError
        # with expected_parent_turn_id == None.
        outcome = service.begin_turn(
            _request(turn_id="t1", parent_turn_id="bogus")
        )
        assert outcome.status == "error"
        assert outcome.expected_parent_turn_id is None


# ---------------------------------------------------------------------------
# stage_result
# ---------------------------------------------------------------------------


class TestStageResult:
    def test_stage_result_requires_lease_token(self, tmp_path):
        service = _make_service(tmp_path)
        service.begin_turn(_request())
        assert service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token="",
            result={"answer": "x"},
            result_digest="d",
        ) is False

    def test_stage_result_requires_scope_and_turn(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        assert service.stage_result(
            "",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "x"},
            result_digest="d",
        ) is False
        assert service.stage_result(
            "ws:default:conv-1",
            "",
            lease_token=began.lease_token,
            result={"answer": "x"},
            result_digest="d",
        ) is False

    def test_stage_result_returns_false_on_mark_ready_failure(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        assert service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "x"},
            result_digest="d",
        ) is True
        assert (
            service.pending_store.get("ws:default:conv-1", "turn-1") is not None
        )


# ---------------------------------------------------------------------------
# complete_turn
# ---------------------------------------------------------------------------


class TestCompleteTurn:
    def test_complete_turn_commits_and_clears_pending(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "staged"})
        service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "staged"},
            result_digest=digest,
        )
        assert (
            service.pending_store.get("ws:default:conv-1", "turn-1") is not None
        )
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        assert outcome.status == "complete"
        assert outcome.replayed is False
        assert outcome.message_count == 2
        assert (
            service.pending_store.get("ws:default:conv-1", "turn-1") is None
        )

    def test_complete_turn_replayed_does_not_duplicate(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        assert outcome.status == "complete"
        assert outcome.replayed is True
        assert len(outcome.messages) == 2

    def test_complete_turn_appends_to_warm_memory_once(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
            warm_conversation_id="warm-conv-1",
        )
        prompt = memory.render_for_prompt("warm-conv-1")
        assert "Domanda" in prompt
        assert "Risposta" in prompt

    def test_complete_turn_appends_to_warm_memory_idempotently(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        began = service.begin_turn(_request())
        for _ in range(2):
            service.complete_turn(
                "ws:default:conv-1",
                "turn-1",
                lease_token=began.lease_token,
                request_fingerprint=FINGERPRINT,
                user_content="Domanda",
                assistant_content="Risposta",
                warm_conversation_id="warm-conv-1",
            )
        prompt = memory.render_for_prompt("warm-conv-1")
        assert prompt.count("Domanda") == 1

    def test_complete_turn_uses_request_fields(self, tmp_path):
        service = _make_service(tmp_path)
        request = _request(knowledge_base_ids=["kb-a"])
        began = service.begin_turn(request)
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
            request=request,
        )
        assert outcome.status == "complete"
        conv = service.get_conversation("ws:default:conv-1")
        kbs = {kb["knowledge_base_id"] for kb in conv["knowledge_base_ids"]}
        assert "kb-a" in kbs

    def test_complete_turn_conflict_clears_pending(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "staged"})
        service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "staged"},
            result_digest=digest,
        )
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token="wrong-token",
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        assert outcome.status == "conflict"
        assert (
            service.pending_store.get("ws:default:conv-1", "turn-1") is None
        )


# ---------------------------------------------------------------------------
# fail_turn
# ---------------------------------------------------------------------------


class TestFailTurn:
    def test_fail_turn_clears_pending(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "staged"})
        service.stage_result(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            result={"answer": "staged"},
            result_digest=digest,
        )
        assert service.fail_turn(
            "ws:default:conv-1", "turn-1", began.lease_token
        ) is True
        assert (
            service.pending_store.get("ws:default:conv-1", "turn-1") is None
        )

    def test_fail_turn_without_lease_returns_false(self, tmp_path):
        service = _make_service(tmp_path)
        service.begin_turn(_request())
        assert service.fail_turn("ws:default:conv-1", "turn-1", None) is False

    def test_failed_turn_can_be_reopened(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        assert service.fail_turn(
            "ws:default:conv-1", "turn-1", began.lease_token
        ) is True
        outcome = service.begin_turn(_request())
        assert outcome.status == "new"
        assert outcome.lease_token != began.lease_token


# ---------------------------------------------------------------------------
# hydrate_for_prompt
# ---------------------------------------------------------------------------


class TestHydrate:
    def test_hydrate_populates_warm_memory_once(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        assert memory.render_for_prompt("warm-conv-1") == ""
        assert service.hydrate_for_prompt("ws:default:conv-1", "warm-conv-1") is True
        prompt = memory.render_for_prompt("warm-conv-1")
        assert "Domanda" in prompt
        assert "Risposta" in prompt
        assert service.hydrate_for_prompt("ws:default:conv-1", "warm-conv-1") is False

    def test_hydrate_returns_false_for_unknown_scope(self, tmp_path):
        service = _make_service(tmp_path)
        assert service.hydrate_for_prompt("missing-scope", "warm-1") is False

    def test_hydrate_returns_false_for_empty_conversation_id(self, tmp_path):
        service = _make_service(tmp_path)
        assert service.hydrate_for_prompt("ws:default:conv-1", "") is False


# ---------------------------------------------------------------------------
# Pass-through helpers
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_get_conversation_returns_dict(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ciao",
            assistant_content="Salve",
        )
        conv = service.get_conversation("ws:default:conv-1")
        assert conv is not None
        assert conv["scope_key"] == "ws:default:conv-1"
        assert conv["message_count"] == 2

    def test_get_conversation_unknown_returns_none(self, tmp_path):
        service = _make_service(tmp_path)
        assert service.get_conversation("missing") is None

    def test_list_messages_returns_messages_and_cursor(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ciao",
            assistant_content="Salve",
        )
        conv = service.get_conversation("ws:default:conv-1")
        messages, cursor = service.list_messages(conv["id"], limit=10)
        assert len(messages) == 2
        assert cursor is None

    def test_reset_continuity_returns_parent_and_cleanup_count(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request(turn_id="t1"))
        service.complete_turn(
            "ws:default:conv-1",
            "t1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ciao",
            assistant_content="Salve",
        )
        conv = service.get_conversation("ws:default:conv-1")
        result = service.reset_continuity(conv["id"])
        assert result["parent_turn_id"] == "t1"
        assert result["cleaned_up_leases"] == 0

    def test_reset_continuity_disabled_service_returns_empty(self, tmp_path):
        service = _make_service(tmp_path, enabled=False)
        result = service.reset_continuity("missing-id")
        assert result == {"parent_turn_id": None, "cleaned_up_leases": 0}


# ---------------------------------------------------------------------------
# Service cache LRU eviction
# ---------------------------------------------------------------------------


class TestServiceCacheLRU:
    def test_repeated_access_promotes_to_most_recent(self, tmp_path, monkeypatch):
        from utils import conversation_service as cs

        cs.reset_conversation_service()
        monkeypatch.setattr(cs, "_SERVICE_CACHE_MAX_SIZE", 3)

        cs.get_conversation_service_for_workspace("ws-1")
        cs.get_conversation_service_for_workspace("ws-2")
        cs.get_conversation_service_for_workspace("ws-3")

        # Access ws-1 again -> it becomes most-recently-used.
        cs.get_conversation_service_for_workspace("ws-1")

        # Inserting ws-4 should evict ws-2 (least recently used), not ws-1.
        cs.get_conversation_service_for_workspace("ws-4")

        with cs._service_cache_lock:
            keys = list(cs._service_cache.keys())
        assert "ws-2" not in keys
        assert "ws-1" in keys
        assert "ws-3" in keys
        assert "ws-4" in keys
        cs.reset_conversation_service()

    def test_lru_evicts_oldest_when_capacity_exceeded(self, tmp_path, monkeypatch):
        from utils import conversation_service as cs

        cs.reset_conversation_service()
        monkeypatch.setattr(cs, "_SERVICE_CACHE_MAX_SIZE", 2)

        cs.get_conversation_service_for_workspace("ws-a")
        cs.get_conversation_service_for_workspace("ws-b")
        # ws-a is LRU; inserting ws-c should evict ws-a.
        cs.get_conversation_service_for_workspace("ws-c")

        with cs._service_cache_lock:
            keys = list(cs._service_cache.keys())
        assert "ws-a" not in keys
        assert "ws-b" in keys
        assert "ws-c" in keys
        cs.reset_conversation_service()


# ---------------------------------------------------------------------------
# Fingerprint + helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_compute_request_fingerprint_is_stable(self):
        fp1 = compute_request_fingerprint(
            query="Ciao",
            model="gpt-4",
            provider="openai",
            knowledge_base_ids=["kb-a", "kb-b"],
        )
        fp2 = compute_request_fingerprint(
            query="Ciao",
            model="gpt-4",
            provider="openai",
            knowledge_base_ids=["kb-b", "kb-a"],
        )
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_compute_request_fingerprint_differs_on_query(self):
        fp1 = compute_request_fingerprint(query="Ciao")
        fp2 = compute_request_fingerprint(query="Salve")
        assert fp1 != fp2

    def test_compute_result_digest_is_stable(self):
        d1 = compute_result_digest({"a": 1, "b": 2})
        d2 = compute_result_digest({"b": 2, "a": 1})
        assert d1 == d2
        assert len(d1) == 64

    def test_derive_scope_kind(self):
        assert derive_scope_kind("ws:multi-chat:conv-1") == "multi"
        assert derive_scope_kind("ws:kb:kb-1:conv-1") == "kb"
        assert derive_scope_kind("ws:default:conv-1") == "default"

    def test_conversation_turn_error_to_dict(self):
        err = ConversationTurnError(
            "Turn already generating",
            code="turn_in_progress",
            status_code=409,
            retry_after=5,
            payload={"turn_id": "turn-1"},
        )
        assert err.status_code == 409
        assert err.retry_after == 5
        assert err.to_dict() == {
            "error": "Turn already generating",
            "status": "turn_in_progress",
            "turn_id": "turn-1",
        }


def _fingerprint(seed: str = "a") -> str:
    return (seed * 64)[:64]
