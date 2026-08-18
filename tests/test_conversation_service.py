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


def _make_history_store(
    tmp_path,
    workspace_id: str = "test-workspace",
    **kwargs,
) -> ConversationHistoryStore:
    workspace_data_dir = tmp_path / "workspaces"
    workspace_data_dir.mkdir(exist_ok=True)
    return ConversationHistoryStore(
        workspace_id=workspace_id,
        workspace_data_dir=str(workspace_data_dir),
        **kwargs,
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
    recover_lost_result: bool = False,
) -> TurnRequest:
    return TurnRequest(
        scope_key=scope_key,
        scope_kind=scope_kind,
        client_conversation_id=client_conversation_id,
        turn_id=turn_id,
        request_fingerprint=fingerprint,
        parent_turn_id=parent_turn_id,
        selected_knowledge_base_ids=knowledge_base_ids,
        recover_lost_result=recover_lost_result,
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
        assert outcome.lease_token == began.lease_token
        assert outcome.result == {"answer": "staged"}
        assert outcome.result_digest == digest

    def test_ready_without_pending_requires_explicit_regeneration(self, tmp_path):
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
        assert outcome.status == "volatile_result_lost"
        assert outcome.code == "volatile_result_lost"
        assert outcome.lease_token is None

        persisted = service.history_store.get_turn(
            "ws:default:conv-1",
            "turn-1",
        )
        assert persisted["status"] == "ready"
        assert persisted["lease_token"] == began.lease_token

    def test_ready_without_pending_can_be_explicitly_regenerated(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        digest = compute_result_digest({"answer": "lost"})
        assert service.history_store.mark_turn_ready(
            "ws:default:conv-1",
            "turn-1",
            began.lease_token,
            digest,
        ) is True

        outcome = service.begin_turn(_request(recover_lost_result=True))

        assert outcome.status == "new"
        assert outcome.lease_token
        assert outcome.lease_token != began.lease_token
        persisted = service.history_store.get_turn(
            "ws:default:conv-1", "turn-1"
        )
        assert persisted["status"] == "generating"
        assert persisted["lease_token"] == outcome.lease_token

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
        assert outcome.status == "continuity_error"
        assert outcome.expected_parent_turn_id == "t1"

    def test_continuity_error_expected_parent_none_when_no_complete(self, tmp_path):
        service = _make_service(tmp_path)
        # No complete turn; supplying a bogus parent raises ContinuityError
        # with expected_parent_turn_id == None.
        outcome = service.begin_turn(
            _request(turn_id="t1", parent_turn_id="bogus")
        )
        assert outcome.status == "continuity_error"
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
        assert outcome.user_sequence == 1
        assert outcome.assistant_sequence == 2
        assert len(outcome.messages) == 2
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

    def test_distinct_turns_with_identical_text_are_both_kept_warm(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)

        first = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=first.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ripeti",
            assistant_content="Uguale",
            warm_conversation_id="warm-conv-1",
        )
        second = service.begin_turn(
            _request(turn_id="turn-2", parent_turn_id="turn-1")
        )
        service.complete_turn(
            "ws:default:conv-1",
            "turn-2",
            lease_token=second.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Ripeti",
            assistant_content="Uguale",
            warm_conversation_id="warm-conv-1",
        )

        prompt = memory.render_for_prompt("warm-conv-1")
        assert prompt.count("Ripeti") == 2
        assert prompt.count("Uguale") == 2

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

    def test_complete_turn_preserves_message_type_sources_and_metadata(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Analizza",
            assistant_content="Completato",
            message_type="code_interpreter",
            sources=[{"filename": "report.csv"}],
            metadata={"success": True},
        )

        assistant = outcome.messages[-1]
        assert assistant["message_type"] == "code_interpreter"
        assert assistant["sources"] == [{"filename": "report.csv"}]
        assert assistant["metadata"] == {"success": True}

        replay = service.begin_turn(_request())
        replayed_assistant = replay.messages[-1]
        assert replay.status == "complete"
        assert replayed_assistant["message_type"] == "code_interpreter"
        assert replayed_assistant["sources"] == [{"filename": "report.csv"}]
        assert replayed_assistant["metadata"] == {"success": True}

    def test_complete_turn_returns_warm_memory_summary_job(self, tmp_path):
        memory = ConversationMemoryStore(
            summary_threshold_chars=1,
            recent_turns_to_keep=1,
        )
        memory.append_turn(
            "warm-conv-1",
            user="Prima domanda",
            assistant="Prima risposta",
        )
        service = _make_service(tmp_path, memory_store=memory)
        began = service.begin_turn(_request())
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Seconda domanda",
            assistant_content="Seconda risposta",
            warm_conversation_id="warm-conv-1",
        )

        assert outcome.summary_job is not None
        assert outcome.summary_job.conversation_id == "warm-conv-1"
        assert outcome.summary_job.turns_to_summarize == [
            ConversationTurn(user="Prima domanda", assistant="Prima risposta")
        ]

    def test_stale_complete_conflict_preserves_current_pending_owner(self, tmp_path):
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
        pending = service.pending_store.get("ws:default:conv-1", "turn-1")
        assert pending is not None
        assert pending.lease_token == began.lease_token


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

    def test_service_renews_owned_lease(self, tmp_path):
        service = _make_service(tmp_path)
        began = service.begin_turn(_request())
        assert service.renew_turn_lease(
            "ws:default:conv-1", "turn-1", began.lease_token
        ) is True
        assert service.renew_turn_lease(
            "ws:default:conv-1", "turn-1", "wrong"
        ) is False


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

    def test_hydrate_excludes_messages_already_covered_by_summary(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        first = service.begin_turn(_request(turn_id="t1"))
        first_outcome = service.complete_turn(
            "ws:default:conv-1",
            "t1",
            lease_token=first.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda già riassunta",
            assistant_content="Risposta già riassunta",
        )
        assert service.update_summary(
            "ws:default:conv-1",
            "Contesto consolidato",
            expected_version=0,
            through_sequence=first_outcome.assistant_sequence,
        ) is True

        second = service.begin_turn(
            _request(turn_id="t2", parent_turn_id="t1")
        )
        service.complete_turn(
            "ws:default:conv-1",
            "t2",
            lease_token=second.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda recente",
            assistant_content="Risposta recente",
        )

        assert service.hydrate_for_prompt(
            "ws:default:conv-1", "warm-with-summary"
        ) is True
        prompt = memory.render_for_prompt("warm-with-summary")
        assert "Contesto consolidato" in prompt
        assert "Domanda recente" in prompt
        assert "Risposta recente" in prompt
        assert "Domanda già riassunta" not in prompt
        assert "Risposta già riassunta" not in prompt

    def test_hydrate_compacts_unsummarized_tail_before_warming(self, tmp_path):
        memory = ConversationMemoryStore(recent_turns_to_keep=2)
        service = _make_service(tmp_path, memory_store=memory)
        parent = None
        for index in range(1, 7):
            turn_id = f"t{index}"
            began = service.begin_turn(
                _request(turn_id=turn_id, parent_turn_id=parent)
            )
            completed = service.complete_turn(
                "ws:default:conv-1",
                turn_id,
                lease_token=began.lease_token,
                request_fingerprint=FINGERPRINT,
                user_content=f"Domanda {index}",
                assistant_content=f"Risposta {index}",
            )
            assert completed.status == "complete"
            parent = turn_id

        assert service.hydrate_for_prompt(
            "ws:default:conv-1", "warm-compacted"
        ) is True

        state = memory._conversations["warm-compacted"]
        assert len(state.turns) == 2
        assert [turn.assistant_sequence for turn in state.turns] == [10, 12]
        assert state.summary_through_sequence == 8
        durable = service.history_store.get_by_scope_key(
            "ws:default:conv-1"
        )
        assert durable["summary_through_sequence"] == 8
        assert durable["summary_version"] == 1

    def test_hydrate_returns_false_for_unknown_scope(self, tmp_path):
        service = _make_service(tmp_path)
        assert service.hydrate_for_prompt("missing-scope", "warm-1") is False

    def test_hydrate_returns_false_for_empty_conversation_id(self, tmp_path):
        service = _make_service(tmp_path)
        assert service.hydrate_for_prompt("ws:default:conv-1", "") is False

    def test_sync_repairs_a_warm_sequence_gap_from_durable_history(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        parent = None
        for index in range(1, 4):
            turn_id = f"t{index}"
            began = service.begin_turn(
                _request(turn_id=turn_id, parent_turn_id=parent)
            )
            service.complete_turn(
                "ws:default:conv-1",
                turn_id,
                lease_token=began.lease_token,
                request_fingerprint=FINGERPRINT,
                user_content=f"Domanda {index}",
                assistant_content=f"Risposta {index}",
            )
            parent = turn_id

        memory.append_turn(
            "warm-gap",
            user="Domanda 1",
            assistant="Risposta 1",
            assistant_sequence=2,
        )
        memory.append_turn(
            "warm-gap",
            user="Domanda 3",
            assistant="Risposta 3",
            assistant_sequence=6,
        )
        assert memory.durable_state_is_current("warm-gap", 6) is False

        assert service.sync_for_prompt("ws:default:conv-1", "warm-gap") is True
        prompt = memory.render_for_prompt("warm-gap")
        assert prompt.index("Domanda 1") < prompt.index("Domanda 2")
        assert prompt.index("Domanda 2") < prompt.index("Domanda 3")
        assert memory.durable_state_is_current("warm-gap", 6) is True

    def test_sync_replaces_unsequenced_volatile_turns(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        memory.append_turn(
            "warm-legacy",
            user="Domanda volatile precedente",
            assistant="Risposta volatile precedente",
        )
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda durevole",
            assistant_content="Risposta durevole",
        )

        assert service.sync_for_prompt("ws:default:conv-1", "warm-legacy") is True
        prompt = memory.render_for_prompt("warm-legacy")
        assert "Domanda volatile precedente" not in prompt
        assert "Domanda durevole" in prompt


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

    def test_delete_clears_warm_and_all_pending_scope_entries(self, tmp_path):
        memory = ConversationMemoryStore()
        pending = PendingTurnResultStore()
        service = _make_service(
            tmp_path, memory_store=memory, pending_store=pending
        )
        began = service.begin_turn(_request())
        service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Segreto",
            assistant_content="Risposta",
            warm_conversation_id="ws:default:conv-1",
        )
        pending.put(
            "ws:default:conv-1",
            "orphan",
            lease_token="lease",
            result_digest="digest",
            result={"answer": "draft"},
        )
        history_id = service.get_conversation("ws:default:conv-1")["id"]

        assert service.delete_conversation(history_id) is True
        assert memory.render_for_prompt("ws:default:conv-1") == ""
        assert pending.get("ws:default:conv-1", "orphan") is None

    def test_update_summary_hook_persists_and_hydrates(self, tmp_path):
        memory = ConversationMemoryStore()
        service = _make_service(tmp_path, memory_store=memory)
        began = service.begin_turn(_request())
        outcome = service.complete_turn(
            "ws:default:conv-1",
            "turn-1",
            lease_token=began.lease_token,
            request_fingerprint=FINGERPRINT,
            user_content="Domanda",
            assistant_content="Risposta",
        )
        assert service.update_summary(
            "ws:default:conv-1",
            "Riassunto durevole",
            expected_version=0,
            through_sequence=outcome.assistant_sequence,
        ) is True

        assert service.hydrate_for_prompt(
            "ws:default:conv-1", "warm-summary"
        ) is True
        assert "Riassunto durevole" in memory.render_for_prompt("warm-summary")


# ---------------------------------------------------------------------------
# Service cache LRU eviction
# ---------------------------------------------------------------------------


class TestServiceCacheLRU:
    def test_repeated_access_promotes_to_most_recent(self, tmp_path, monkeypatch):
        from utils import conversation_service as cs

        cs.reset_conversation_service()
        monkeypatch.setattr(cs, "_SERVICE_CACHE_MAX_SIZE", 3)
        monkeypatch.setenv(
            "RAG_WORKSPACE_DATA_DIR", str(tmp_path / "workspaces")
        )

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
        monkeypatch.setenv(
            "RAG_WORKSPACE_DATA_DIR", str(tmp_path / "workspaces")
        )

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

    def test_fingerprint_canonicalizes_nested_mapping_order(self):
        fp1 = compute_request_fingerprint(
            query="Ciao", client_context={"a": 1, "nested": {"x": 2, "y": 3}}
        )
        fp2 = compute_request_fingerprint(
            query="Ciao", client_context={"nested": {"y": 3, "x": 2}, "a": 1}
        )
        assert fp1 == fp2

    def test_fingerprint_covers_prompt_agent_and_attachments(self):
        base = compute_request_fingerprint(
            query="Analizza",
            system_prompt_id="prompt-1",
            system_prompt_scope="personal",
            agent_id="agent-1",
            use_code_interpreter=True,
            attached_files=[{"id": "file-a", "name": "a.csv"}],
        )
        changed_file = compute_request_fingerprint(
            query="Analizza",
            system_prompt_id="prompt-1",
            system_prompt_scope="personal",
            agent_id="agent-1",
            use_code_interpreter=True,
            attached_files=[{"id": "file-b", "name": "a.csv"}],
        )
        changed_scope = compute_request_fingerprint(
            query="Analizza",
            system_prompt_id="prompt-1",
            system_prompt_scope="shared",
            agent_id="agent-1",
            use_code_interpreter=True,
            attached_files=[{"id": "file-a", "name": "a.csv"}],
        )
        assert base != changed_file
        assert base != changed_scope

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
            "code": "turn_in_progress",
            "turn_id": "turn-1",
        }


def _fingerprint(seed: str = "a") -> str:
    return (seed * 64)[:64]
