"""Tests for the conversations API endpoints.

The routes derive the workspace_id from the authenticated user (via
``workspace_from_request``) and resolve the service through
``get_conversation_service_for_workspace``. The fixtures create conversations
through the same per-workspace service so the data is visible to the API.

NOTE: imports use the ``utils.*`` path (not ``app.utils.*``) to match the
routes and share the same module-level service cache.
"""
import os
import re
from uuid import uuid4

import pytest

from app import create_app
from utils.conversation_service import (
    ConversationService,
    TurnRequest,
    get_conversation_service_for_workspace,
    reset_conversation_service,
)
from utils.user_store import UserStore
from utils.workspace import safe_workspace_id


_HISTORY_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _fingerprint(seed: str) -> str:
    return (seed * 64)[:64]


@pytest.fixture
def flask_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_CONVERSATION_HISTORY_ENABLED", "1")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_DB": str(tmp_path / "users.db"),
            "PROMPTS_DIR": str(tmp_path / "prompts"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
            "API_KEY_USAGE_FILE": str(tmp_path / "api_keys_usage.json"),
            "MAX_UPLOAD_SIZE_MB": 5,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
            "CONVERSATION_HISTORY_ENABLED": "1",
        }
    )
    yield app
    reset_conversation_service()


@pytest.fixture
def user(flask_app):
    store = UserStore(flask_app.config["USERS_DB"])
    return store.create_user(
        email="test@example.local",
        password="testpass",
        display_name="Test",
        role="admin",
        enabled=True,
    )


@pytest.fixture
def client(flask_app, user):
    client = flask_app.test_client()
    client.post(
        "/admin/login",
        data={"email": "test@example.local", "password": "testpass"},
    )
    return client


@pytest.fixture
def workspace_id(user):
    return safe_workspace_id(user["id"])


@pytest.fixture
def service(flask_app, workspace_id):
    """Resolve the same enabled service the routes will use.

    Passing ``app`` ensures config is read from the app rather than the
    env-var fallback, and resolves+registers the service in the shared
    module-level cache so the routes reuse it.
    """
    with flask_app.app_context():
        svc = get_conversation_service_for_workspace(workspace_id, app=flask_app)
    assert svc.enabled, "conversation service must be enabled for tests"
    return svc


def _create_conversation(service, workspace_id, scope_suffix, query, response):
    scope_key = f"{workspace_id}:default:{scope_suffix}"
    turn_id = str(uuid4())
    req = TurnRequest(
        scope_key=scope_key,
        scope_kind="default",
        client_conversation_id=f"client-{scope_suffix}",
        turn_id=turn_id,
        request_fingerprint=_fingerprint(turn_id),
        parent_turn_id=None,
        selected_knowledge_base_ids=[],
        provider_id="",
        model_id="test",
        workspace_id=workspace_id,
    )
    began = service.begin_turn(req)
    if began.status != "new":
        return None
    service.complete_turn(
        scope_key=scope_key,
        turn_id=turn_id,
        lease_token=began.lease_token,
        request_fingerprint=_fingerprint(turn_id),
        user_content=query,
        assistant_content=response,
    )
    conv = service.get_conversation(scope_key)
    return conv["id"] if conv else None


@pytest.fixture
def setup_conversations(service, workspace_id):
    conv_id = _create_conversation(service, workspace_id, "conv1", "Hello", "Hi there!")
    conv_id2 = _create_conversation(
        service, workspace_id, "conv2", "Second", "Second response"
    )
    return {
        "service": service,
        "workspace_id": workspace_id,
        "conv_id": conv_id,
        "conv_id2": conv_id2,
    }


class TestConversationsAPI:
    def test_list_conversations_empty(self, client):
        response = client.get("/api/conversations")
        assert response.status_code == 200
        data = response.get_json()
        assert data["conversations"] == []
        pagination = data["pagination"]
        assert pagination["total"] == 0
        assert pagination["page"] == 1
        assert pagination["per_page"] == 20

    def test_list_conversations_with_data(self, client, setup_conversations):
        response = client.get("/api/conversations")
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["conversations"]) >= 1
        assert data["pagination"]["total"] >= 1
        conv = data["conversations"][0]
        assert "id" in conv
        assert "title" in conv
        assert "created_at" in conv
        assert "updated_at" in conv
        assert "status" in conv
        assert "message_count" in conv

    def test_list_conversations_pagination(self, client, setup_conversations):
        response = client.get("/api/conversations?page=1&per_page=1")
        assert response.status_code == 200
        data = response.get_json()
        assert data["pagination"]["per_page"] == 1
        assert data["pagination"]["page"] == 1
        assert len(data["conversations"]) <= 1

    def test_list_conversations_invalid_page(self, client):
        response = client.get("/api/conversations?page=abc")
        assert response.status_code == 400

    def test_list_conversations_filter_archived(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        service = setup_conversations["service"]
        service.update_conversation(conv_id, archived=True)

        response = client.get("/api/conversations?archived=true")
        assert response.status_code == 200
        data = response.get_json()
        assert any(c["id"] == conv_id for c in data["conversations"])

        response = client.get("/api/conversations?archived=false")
        assert response.status_code == 200
        data = response.get_json()
        assert not any(c["id"] == conv_id for c in data["conversations"])

    def test_list_conversations_invalid_archived(self, client):
        response = client.get("/api/conversations?archived=maybe")
        assert response.status_code == 400

    def test_get_conversation(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.get(f"/api/conversations/{conv_id}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["id"] == conv_id
        assert "title" in data
        assert "created_at" in data
        assert "updated_at" in data
        assert "status" in data
        assert "message_count" in data

    def test_get_conversation_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.get(f"/api/conversations/{fake_id}")
        assert response.status_code == 404

    def test_get_conversation_invalid_uuid(self, client):
        response = client.get("/api/conversations/not-a-uuid")
        assert response.status_code == 400

    def test_get_conversation_messages(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.get(f"/api/conversations/{conv_id}/messages")
        assert response.status_code == 200
        data = response.get_json()
        assert "messages" in data
        assert "next_cursor" in data
        assert "limit" in data
        assert len(data["messages"]) >= 1

    def test_get_conversation_messages_pagination(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.get(f"/api/conversations/{conv_id}/messages?limit=1")
        assert response.status_code == 200
        data = response.get_json()
        assert data["limit"] == 1
        assert len(data["messages"]) <= 1

    def test_get_conversation_messages_invalid_limit(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.get(f"/api/conversations/{conv_id}/messages?limit=abc")
        assert response.status_code == 400

    def test_get_conversation_messages_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.get(f"/api/conversations/{fake_id}/messages")
        assert response.status_code == 404

    def test_rename_conversation(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"title": "New Title"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["title"] == "New Title"
        assert "updated_at" in data

    def test_rename_conversation_empty_title(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"title": "   "},
        )
        assert response.status_code == 400

    def test_rename_conversation_too_long(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"title": "x" * 200},
        )
        assert response.status_code == 400

    def test_rename_conversation_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.patch(
            f"/api/conversations/{fake_id}",
            json={"title": "New Title"},
        )
        assert response.status_code == 404

    def test_rename_conversation_no_fields(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={},
        )
        assert response.status_code == 400

    def test_update_rejects_malformed_json_with_json_error(
        self,
        client,
        setup_conversations,
    ):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            data="{",
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.is_json
        assert response.get_json()["code"] == "validation_error"

    def test_conversation_error_handlers_do_not_change_unrelated_routes(self, client):
        response = client.get("/route-that-does-not-exist")

        assert response.status_code == 404
        assert response.mimetype == "text/html"

    def test_archive_conversation(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"archived": True},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "archived"
        assert data["archived_at"] is not None

        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"archived": False},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "active"
        assert data["archived_at"] is None

    def test_archive_conversation_invalid_value(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"archived": "yes"},
        )
        assert response.status_code == 400

    def test_archive_conversation_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.patch(
            f"/api/conversations/{fake_id}",
            json={"archived": True},
        )
        assert response.status_code == 404

    def test_update_both_title_and_archived(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.patch(
            f"/api/conversations/{conv_id}",
            json={"title": "Updated Title", "archived": True},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["title"] == "Updated Title"
        assert data["status"] == "archived"

    def test_delete_conversation(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id2"]
        response = client.get(f"/api/conversations/{conv_id}")
        assert response.status_code == 200

        response = client.delete(f"/api/conversations/{conv_id}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["deleted"] is True

        response = client.get(f"/api/conversations/{conv_id}")
        assert response.status_code == 404

    def test_delete_conversation_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.delete(f"/api/conversations/{fake_id}")
        assert response.status_code == 404

    def test_delete_conversation_invalid_uuid(self, client):
        response = client.delete("/api/conversations/invalid-uuid")
        assert response.status_code == 400

    def test_list_conversations_ordering(self, client, setup_conversations):
        response = client.get("/api/conversations")
        assert response.status_code == 200
        data = response.get_json()
        if len(data["conversations"]) > 1:
            timestamps = [c["updated_at"] for c in data["conversations"]]
            for i in range(len(timestamps) - 1):
                assert timestamps[i] >= timestamps[i + 1]

    def test_history_id_uuid_format(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        assert _HISTORY_ID_RE.match(conv_id)

    def test_reset_continuity_returns_parent_turn_id(self, client, setup_conversations):
        conv_id = setup_conversations["conv_id"]
        response = client.post(f"/api/conversations/{conv_id}/reset-continuity")
        assert response.status_code == 200
        data = response.get_json()
        assert "parent_turn_id" in data
        assert "cleaned_up_leases" in data
        assert data["cleaned_up_leases"] == 0

    def test_reset_continuity_not_found(self, client):
        fake_id = "00000000-0000-0000-0000-000000000000"
        response = client.post(f"/api/conversations/{fake_id}/reset-continuity")
        assert response.status_code == 404

    def test_reset_continuity_invalid_uuid(self, client):
        response = client.post("/api/conversations/invalid-uuid/reset-continuity")
        assert response.status_code == 400
