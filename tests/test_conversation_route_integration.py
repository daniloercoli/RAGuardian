"""Regression coverage for durable history at the HTTP integration boundary."""

from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace

import pytest

from app import create_app
from app.utils.user_store import UserStore
from utils.conversation_memory import (
    ConversationMemoryStore,
    get_conversation_store,
    reset_conversation_store,
)
from utils.conversation_service import (
    TurnRequest,
    get_conversation_service_for_workspace,
    reset_conversation_service,
)
from utils.pending_turn_store import (
    get_pending_turn_store,
    reset_pending_turn_store,
)
from utils.workspace import knowledge_base_store, workspace_for_user


@pytest.fixture
def history_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_CONVERSATION_HISTORY_ENABLED", "1")
    reset_conversation_service()
    reset_conversation_store()
    reset_pending_turn_store()
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "history-test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_DB": str(tmp_path / "users.db"),
            "PROMPTS_DIR": str(tmp_path / "prompts"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
            "API_KEY_USAGE_FILE": str(tmp_path / "api_key_usage.json"),
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
            "CONVERSATION_HISTORY_ENABLED": True,
            "CONVERSATION_TURN_LEASE_SECONDS": 1,
        }
    )
    UserStore(app.config["USERS_DB"]).create_user(
        email="history@example.local",
        password="secret",
        display_name="History Tester",
        role="admin",
        enabled=True,
    )
    yield app
    reset_conversation_service()
    reset_conversation_store()
    reset_pending_turn_store()


@pytest.fixture
def history_client(history_app):
    client = history_app.test_client()
    response = client.post(
        "/admin/login",
        data={"email": "history@example.local", "password": "secret"},
    )
    assert response.status_code == 302
    return client


def _workspace_and_service(app):
    user = UserStore(app.config["USERS_DB"]).list()[0]
    workspace = workspace_for_user(user, app=app)
    with app.app_context():
        service = get_conversation_service_for_workspace(
            workspace.workspace_id,
            app=app,
        )
    return workspace, service


def _patch_model_validation(monkeypatch):
    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(
        app_module,
        "_validate_model_selection",
        lambda *_args, **_kwargs: None,
    )
    return app_module


def _messages_for_scope(service, scope_key):
    conversation = service.get_conversation(scope_key)
    assert conversation is not None
    messages, _cursor = service.list_messages(conversation["id"], limit=50)
    return conversation, messages


def test_ask_nonstream_commits_history_and_sources(
    history_client,
    history_app,
    monkeypatch,
):
    _patch_model_validation(monkeypatch)
    rag_engine = importlib.import_module("utils.rag_engine")
    sources = [
        {
            "source": "manual.pdf",
            "page": 7,
            "excerpt": "Dettaglio verificabile",
        }
    ]

    monkeypatch.setattr(
        rag_engine,
        "query_rag",
        lambda *_args, **_kwargs: {
            "answer": "Risposta persistita",
            "context": [],
            "sources": sources,
            "model": "test-model",
            "provider": "test-provider",
            "usage": None,
        },
    )

    response = history_client.post(
        "/ask",
        json={
            "query": "Qual e il dettaglio?",
            "conversation_id": "conv-history-nonstream",
            "turn_id": "turn-one",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["history_status"] == "saved"
    assert payload["history_saved"] is True
    assert payload["history_id"]

    workspace, service = _workspace_and_service(history_app)
    scope_key = f"{workspace.workspace_id}:conv-history-nonstream"
    conversation, messages = _messages_for_scope(service, scope_key)
    assert conversation["message_count"] == 2
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "Risposta persistita"
    assert messages[-1]["sources"] == sources


def test_ask_never_reports_history_saved_when_commit_fails(
    history_client,
    history_app,
    monkeypatch,
):
    _patch_model_validation(monkeypatch)
    rag_engine = importlib.import_module("utils.rag_engine")
    workspace, service = _workspace_and_service(history_app)
    scope_key = f"{workspace.workspace_id}:conv-history-failure"

    monkeypatch.setattr(
        rag_engine,
        "query_rag",
        lambda *_args, **_kwargs: {
            "answer": "La risposta applicativa resta disponibile",
            "context": [],
            "sources": [],
            "model": "test-model",
            "provider": "test-provider",
            "usage": None,
        },
    )

    def fail_commit(**_kwargs):
        raise RuntimeError("simulated history outage")

    monkeypatch.setattr(service, "complete_turn", fail_commit)

    response = history_client.post(
        "/ask",
        json={
            "query": "Questa risposta viene salvata?",
            "conversation_id": "conv-history-failure",
            "turn_id": "turn-failure",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["answer"] == "La risposta applicativa resta disponibile"
    assert payload["history_status"] == "error"
    assert payload["history_saved"] is False
    assert payload["history_error"] == "history_persistence_failed"
    assert service.history_store.get_turn(scope_key, "turn-failure")["status"] == "failed"
    assert get_pending_turn_store().get(scope_key, "turn-failure") is None


def test_ask_ndjson_commits_before_done_and_replays_sources(
    history_client,
    history_app,
    monkeypatch,
):
    _patch_model_validation(monkeypatch)
    rag_engine = importlib.import_module("utils.rag_engine")
    workspace, service = _workspace_and_service(history_app)
    committed = {"value": False}
    provider_calls = []
    original_complete = service.history_store.complete_turn
    sources = [{"source": "stream.pdf", "page": 3}]

    def tracked_complete(**kwargs):
        result = original_complete(**kwargs)
        committed["value"] = True
        return result

    def fake_events(*_args, **_kwargs):
        provider_calls.append(True)
        yield {
            "type": "meta",
            "model": "stream-model",
            "provider": "stream-provider",
            "provider_name": "Stream Provider",
        }
        yield {"type": "token", "text": "Risposta"}
        yield {
            "type": "done",
            "answer": "Risposta completa",
            "context": [],
            "sources": sources,
            "model": "stream-model",
            "provider": "stream-provider",
            "provider_name": "Stream Provider",
        }

    monkeypatch.setattr(service.history_store, "complete_turn", tracked_complete)
    monkeypatch.setattr(rag_engine, "query_rag_stream_events", fake_events)
    request_payload = {
        "query": "Domanda in streaming",
        "conversation_id": "conv-history-stream",
        "turn_id": "turn-stream",
        "stream": True,
        "stream_format": "ndjson",
    }

    response = history_client.post("/ask", json=request_payload, buffered=False)
    chunks = iter(response.response)
    meta = json.loads(next(chunks))
    token = json.loads(next(chunks))
    assert meta["type"] == "meta"
    assert token["type"] == "token"
    assert committed["value"] is False

    done = json.loads(next(chunks))
    assert committed["value"] is True
    assert done["type"] == "done"
    assert done["history_status"] == "saved"
    assert done["history_saved"] is True
    assert done["sources"] == sources
    list(chunks)

    scope_key = f"{workspace.workspace_id}:conv-history-stream"
    _conversation, messages = _messages_for_scope(service, scope_key)
    assert messages[-1]["sources"] == sources

    replay = history_client.post("/ask", json=request_payload)
    replay_events = [
        json.loads(line)
        for line in replay.get_data(as_text=True).splitlines()
    ]
    replay_done = replay_events[-1]
    assert replay_done["type"] == "done"
    assert replay_done["replayed"] is True
    assert replay_done["history_saved"] is True
    assert replay_done["history_status"] == "saved"
    assert replay_done["sources"] == sources
    assert provider_calls == [True]


def test_nonstream_completion_persists_generated_summary(
    history_client,
    history_app,
    monkeypatch,
):
    _patch_model_validation(monkeypatch)
    rag_engine = importlib.import_module("utils.rag_engine")
    workspace, service = _workspace_and_service(history_app)
    service.memory_store = ConversationMemoryStore(
        summary_threshold_chars=1,
        recent_turns_to_keep=1,
    )

    monkeypatch.setattr(
        rag_engine,
        "query_rag",
        lambda query, **_kwargs: {
            "answer": f"Risposta a: {query}",
            "context": [],
            "sources": [],
            "model": "summary-model",
            "provider": "summary-provider",
            "usage": None,
        },
    )

    first = history_client.post(
        "/ask",
        json={
            "query": "Prima domanda da riassumere",
            "conversation_id": "conv-history-summary",
            "turn_id": "turn-summary-one",
        },
    )
    second = history_client.post(
        "/ask",
        json={
            "query": "Seconda domanda recente",
            "conversation_id": "conv-history-summary",
            "turn_id": "turn-summary-two",
            "parent_turn_id": "turn-summary-one",
        },
    )

    assert first.get_json()["history_saved"] is True
    assert second.get_json()["history_saved"] is True
    conversation = service.get_conversation(
        f"{workspace.workspace_id}:conv-history-summary"
    )
    assert conversation["summary_version"] == 1
    assert conversation["summary_through_sequence"] == 2
    assert "Prima domanda da riassumere" in conversation["summary"]


def test_history_lease_heartbeat_renews_slow_generation(
    history_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    workspace, service = _workspace_and_service(history_app)
    service.history_store.lease_seconds = 0.03
    renewals = []
    monkeypatch.setattr(
        service,
        "renew_turn_lease",
        lambda scope_key, turn_id, lease_token: renewals.append(
            (scope_key, turn_id, lease_token)
        )
        or True,
    )
    scope_key = f"{workspace.workspace_id}:conv-heartbeat"
    turn_context = app_module.TurnContext(
        scope_key=scope_key,
        turn_id="turn-heartbeat",
        lease_token="lease-heartbeat",
        request_fingerprint="f" * 64,
        outcome=SimpleNamespace(conversation={}),
    )

    with history_app.app_context():
        with app_module._history_lease_heartbeat(turn_context, scope_key):
            time.sleep(0.32)

    assert renewals
    assert renewals[0] == (scope_key, "turn-heartbeat", "lease-heartbeat")


def test_delete_knowledge_base_clears_durable_warm_and_pending_history(
    history_client,
    history_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    workspace, service = _workspace_and_service(history_app)
    created = knowledge_base_store(workspace, app=history_app).create(
        name="History cleanup",
    )
    knowledge_base_id = created["id"]
    scope_key = (
        f"{workspace.workspace_id}:kb:{knowledge_base_id}:conv-delete-history"
    )
    fingerprint = "d" * 64
    request = TurnRequest(
        scope_key=scope_key,
        scope_kind="kb",
        client_conversation_id="conv-delete-history",
        turn_id="turn-delete-saved",
        request_fingerprint=fingerprint,
        parent_turn_id=None,
        selected_knowledge_base_ids=[knowledge_base_id],
        workspace_id=workspace.workspace_id,
    )
    began = service.begin_turn(request)
    assert began.status == "new"
    completed = service.complete_turn(
        scope_key=scope_key,
        turn_id=request.turn_id,
        lease_token=began.lease_token,
        request_fingerprint=fingerprint,
        user_content="Domanda da eliminare",
        assistant_content="Risposta da eliminare",
        selected_knowledge_base_ids=[knowledge_base_id],
        warm_conversation_id=scope_key,
        warm_knowledge_base_ids=[knowledge_base_id],
    )
    assert completed.status == "complete"
    assert service.get_conversation(scope_key) is not None
    assert get_conversation_store().render_for_prompt(scope_key)

    pending = get_pending_turn_store()
    pending.put(
        scope_key,
        "turn-delete-pending",
        lease_token="lease-delete-pending",
        result_digest="digest-delete-pending",
        result={"answer": "staged"},
    )
    assert pending.get(scope_key, "turn-delete-pending") is not None

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )

    response = history_client.delete(f"/api/knowledge-bases/{knowledge_base_id}")

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "completed"
    assert payload["result"]["conversations_deleted"] == 1
    assert service.get_conversation(scope_key) is None
    assert get_conversation_store().render_for_prompt(scope_key) == ""
    assert pending.get(scope_key, "turn-delete-pending") is None
