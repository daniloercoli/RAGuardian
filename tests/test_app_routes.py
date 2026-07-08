import importlib
import io
import json
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import request

from app import create_app
from app.utils.conversation_memory import get_conversation_store, reset_conversation_store
from app.utils.file_index import FileIndex
from app.utils.prompt_store import PromptStore
from app.utils.settings_store import SettingsStore
from app.utils.user_store import UserStore


MP3_BYTES = b"ID3\x04\x00\x00\x00\x00\x00\x00fake audio"
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake png"


@pytest.fixture
def flask_app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_FILE": str(tmp_path / "users.json"),
            "PROMPTS_DIR": str(tmp_path / "prompts"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
            "API_KEY_USAGE_FILE": str(tmp_path / "api_keys_usage.json"),
            "MAX_UPLOAD_SIZE_MB": 5,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
        }
    )
    store = UserStore(app.config["USERS_FILE"])
    user = store.create_user(
        email="admin@example.local",
        password="admin",
        display_name="Admin",
        role="admin",
        enabled=True,
    )
    store.create_api_key(
        user_id=user["id"],
        name="client",
        scopes=["query", "ingest"],
        api_key_value="test-api-key",
    )
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def _login(client):
    return client.post("/admin/login", data={"password": "admin"})


def _login_email(client, email, password="admin"):
    return client.post("/admin/login", data={"email": email, "password": password})


def _workspace_context(flask_app):
    from utils.workspace import workspace_for_user

    store = UserStore(flask_app.config["USERS_FILE"])
    users = store.list()
    if users:
        user = users[0]
    else:
        user = store.create_user(
            email="admin@example.local",
            password="admin",
            display_name="Admin",
            role="admin",
            enabled=True,
        )
    return workspace_for_user(user, app=flask_app)


def test_cors_preflight_allows_configured_origin(client, flask_app):
    flask_app.config.update(
        CORS_ALLOWED_ORIGINS=["https://client.example"],
        CORS_ALLOWED_METHODS=["GET", "POST", "OPTIONS"],
        CORS_ALLOWED_HEADERS=["Content-Type", "X-API-Key"],
        CORS_ALLOW_CREDENTIALS=True,
    )

    response = client.options(
        "/api/v1/query",
        headers={
            "Origin": "https://client.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://client.example"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"
    assert "X-API-Key" in response.headers["Access-Control-Allow-Headers"]


def test_cors_does_not_reflect_untrusted_origin(client, flask_app):
    flask_app.config["CORS_ALLOWED_ORIGINS"] = ["https://client.example"]

    response = client.options(
        "/api/v1/query",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "Access-Control-Allow-Origin" not in response.headers


def test_request_timeout_helper_raises_when_deadline_elapsed(flask_app):
    app_module = importlib.import_module("app.app")

    with flask_app.test_request_context("/ask"):
        request._rag_deadline = time.monotonic() - 1
        with pytest.raises(app_module.RequestTimeoutExceeded):
            app_module._ensure_request_not_timed_out()


def test_admin_files_paginates_indexed_files(client, flask_app, tmp_path):
    client.post("/admin/login", data={"password": "admin"})
    workspace = _workspace_context(flask_app)
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(exist_ok=True)
    for index in range(30):
        upload_path = upload_dir / f"file-{index:02d}.pdf"
        upload_path.write_bytes(b"%PDF-1.4")
        FileIndex(workspace.file_index).record(
            f"file-{index:02d}.pdf",
            str(upload_path),
            chunks=index + 1,
            status="indexed",
        )

    response = client.get("/admin/files?page=2&per_page=10")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Showing 11-20 of 30 tracked file(s)." in html
    assert "file-19.pdf" in html
    assert "file-10.pdf" in html
    assert "file-29.pdf" not in html
    assert 'aria-current="page">2</span>' in html


def test_user_api_key_expiration_uses_full_timestamp(flask_app):
    store = UserStore(flask_app.config["USERS_FILE"])
    user = store.create_user(email="api-user@example.com", password="secret-pass")
    store.create_api_key(
        user_id=user["id"],
        name="fresh",
        scopes=["query"],
        api_key_value="fresh-key",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds"),
    )
    store.create_api_key(
        user_id=user["id"],
        name="expired",
        scopes=["query"],
        api_key_value="expired-key",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"),
    )

    with flask_app.app_context():
        from utils.auth import find_api_key

        assert find_api_key("fresh-key") is not None
        assert find_api_key("expired-key") is None


def test_admin_api_key_reveal_does_not_store_raw_key_in_session(client, flask_app):
    _login(client)
    store = UserStore(flask_app.config["USERS_FILE"])
    user = store.list()[0]
    store.create_api_key(
        user_id=user["id"],
        name="integration",
        scopes=["query"],
        api_key_value="raw-admin-secret",
    )

    response = client.post(
        "/admin/api-keys",
        data={
            "action": "download",
            "user_id": user["id"],
            "key_name": "integration",
        },
    )

    assert response.status_code == 200
    assert b"raw-admin-secret" in response.data
    assert "raw-admin-secret" not in response.headers.get("Set-Cookie", "")
    with client.session_transaction() as sess:
        assert "show_key_raw" not in sess
        assert "show_key_name" not in sess


def test_admin_api_keys_shows_latest_usage_entries_and_download_link(client, flask_app):
    _workspace_context(flask_app)
    client.post("/admin/login", data={"email": "admin@example.local", "password": "admin"})
    from app.utils.api_key_logger import ApiKeyLogger

    logger = ApiKeyLogger(flask_app.config["API_KEY_USAGE_FILE"])
    for index in range(25):
        logger.log(
            user_id="user-1",
            key_name=f"entry-{index:02d}",
            endpoint=f"/api/v1/query/{index:02d}",
            method="POST",
            status_code=200,
            scopes_used=["query"],
            duration_ms=index,
        )

    response = client.get("/admin/api-keys")

    assert response.status_code == 200
    assert b"Showing the latest 20 API key usage entries" in response.data
    assert b"Download full JSON log" in response.data
    assert b"entry-24" in response.data
    assert b"entry-05" in response.data
    assert b"entry-04" not in response.data


def test_admin_api_key_usage_log_downloads_json_file(client, flask_app):
    _workspace_context(flask_app)
    client.post("/admin/login", data={"email": "admin@example.local", "password": "admin"})
    from app.utils.api_key_logger import ApiKeyLogger

    ApiKeyLogger(flask_app.config["API_KEY_USAGE_FILE"]).log(
        user_id="user-1",
        key_name="download-me",
        endpoint="/api/v1/query",
        method="POST",
        status_code=200,
    )

    response = client.get("/admin/api-keys/usage-log")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    assert b"download-me" in response.data


def test_legacy_ask_uses_shared_query_handler(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    def fake_query(payload, stream=False):
        assert payload["query"] == "Domanda valida?"
        assert stream is False
        return {
            "answer": "Risposta mock",
            "context": [{"text": "ctx", "metadata": {}}],
            "model": "mistral-medium",
            "provider": "mistral",
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)
    _login(client)

    response = client.post("/ask", json={"query": "Domanda valida?", "model": "mistral-medium"})

    assert response.status_code == 200
    assert response.get_json()["answer"] == "Risposta mock"


def test_prompt_templates_render_literal_variables(client):
    _login_email(client, "admin@example.local")

    my_response = client.get("/my-prompts")
    admin_response = client.get("/admin/prompts")

    assert my_response.status_code == 200
    assert admin_response.status_code == 200
    for response in (my_response, admin_response):
        rendered = unescape(response.get_data(as_text=True))
        assert "{{UTENTE}}" in rendered
        assert "{{NOME_UTENTE}}" in rendered
        assert "{{DATA_ODOIERNO}}" in rendered
        assert "{{ORA}}" in rendered


def test_system_prompt_links_are_visible_without_admin_leaks(client, flask_app):
    UserStore(flask_app.config["USERS_FILE"]).create_user(
        email="user@example.local",
        password="secret",
        display_name="User",
        role="user",
        enabled=True,
    )

    _login_email(client, "user@example.local", "secret")
    user_prompts = client.get("/my-prompts").get_data(as_text=True)
    user_nav = re.search(r"<nav class=\"top-nav[^\"]*\">(.*?)</nav>", user_prompts, re.S).group(1)
    user_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', user_nav)
    assert "System Prompts" in user_nav
    assert 'href="/my-prompts"' not in user_nav
    assert 'href="/admin/prompts"' not in user_nav
    assert 'href="/admin/config"' not in user_nav
    assert '<span class="nav-item active" aria-current="page">System Prompts</span>' in user_nav
    assert user_links[-1] == ("/", "Close")

    _login_email(client, "admin@example.local")
    admin_home = client.get("/").get_data(as_text=True)
    admin_files = client.get("/admin/files").get_data(as_text=True)
    admin_home_nav = re.search(r"<nav class=\"top-nav[^\"]*\">(.*?)</nav>", admin_home, re.S).group(1)
    admin_files_nav = re.search(r"<nav class=\"top-nav[^\"]*\">(.*?)</nav>", admin_files, re.S).group(1)
    admin_home_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', admin_home_nav)
    admin_files_links = re.findall(r'<a[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', admin_files_nav)
    assert '<span class="nav-item active" aria-current="page">Chat</span>' in admin_home_nav
    assert admin_home_links == [("/admin/files", "Configuration")]
    assert 'href="/my-prompts"' not in admin_home_nav
    assert 'href="/admin/prompts"' not in admin_home_nav
    assert 'href="/admin/config"' not in admin_home_nav
    assert 'href="/my-prompts"' in admin_files
    assert 'href="/admin/prompts"' in admin_files
    assert '<span class="nav-item active" aria-current="page">RAG Files</span>' in admin_files_nav
    assert 'href="/admin/files"' not in admin_files_nav
    assert ("/admin/config", "AI Settings") in admin_files_links
    assert admin_files_links[-3:] == [
        ("/my-prompts", "System Prompts"),
        ("/admin/prompts", "Shared Prompts"),
        ("/", "Close"),
    ]


def test_shared_prompt_list_hides_inactive_from_non_admin(client, flask_app):
    store = UserStore(flask_app.config["USERS_FILE"])
    store.create_user(
        email="user@example.local",
        password="secret",
        display_name="User",
        role="user",
        enabled=True,
    )
    admin = store.list()[0]
    prompts = PromptStore(flask_app.config["PROMPTS_DIR"])
    active = prompts.create_shared("Active", "Visible", created_by=admin["id"])
    inactive = prompts.create_shared("Inactive", "Hidden", created_by=admin["id"])
    prompts.deactivate_shared(inactive["id"])

    _login_email(client, "user@example.local", "secret")
    user_response = client.get("/api/prompts/shared")
    _login_email(client, "admin@example.local")
    admin_response = client.get("/api/prompts/shared")

    assert user_response.status_code == 200
    assert [p["id"] for p in user_response.get_json()["prompts"]] == [active["id"]]
    assert admin_response.status_code == 200
    assert [p["id"] for p in admin_response.get_json()["prompts"]] == [
        active["id"],
        inactive["id"],
    ]


def test_api_key_query_applies_user_system_prompt(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    rag_engine = importlib.import_module("utils.rag_engine")
    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    prompt = PromptStore(flask_app.config["PROMPTS_DIR"]).create_user_prompt(
        user["id"],
        "API persona",
        "Rispondi a {{NOME_UTENTE}}.",
    )
    captured = {}

    def fake_query_rag(*args, **kwargs):
        captured.update(kwargs)
        return {
            "answer": "ok",
            "context": [],
            "sources": [],
            "usage": None,
        }

    monkeypatch.setattr(app_module, "_validate_model_selection", lambda *args, **kwargs: None)
    monkeypatch.setattr(rag_engine, "query_rag", fake_query_rag)

    response = client.post(
        "/api/v1/query",
        headers={"X-API-Key": "test-api-key"},
        json={"query": "Domanda valida?", "system_prompt_id": prompt["id"]},
    )

    assert response.status_code == 200
    assert captured["custom_system_prompt"] == "Rispondi a Admin."


def test_legacy_ask_passes_conversation_id(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    def fake_query(payload, stream=False):
        assert payload["conversation_id"] == "conv-12345678"
        return {
            "answer": "Risposta mock",
            "context": [],
            "model": "mistral-medium",
            "provider": "mistral",
            "conversation_id": payload["conversation_id"],
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)
    _login(client)

    response = client.post(
        "/ask",
        json={
            "query": "Domanda valida?",
            "model": "mistral-medium",
            "conversation_id": "conv-12345678",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["conversation_id"] == "conv-12345678"


def test_legacy_ask_streams_ndjson_events(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    def fake_events(payload):
        assert payload["query"] == "Domanda valida?"
        assert payload["stream"] is True
        assert payload["stream_format"] == "ndjson"
        return iter(
            [
                json.dumps({"type": "token", "text": "Ciao"}) + "\n",
                json.dumps({"type": "done", "context": []}) + "\n",
            ]
        )

    monkeypatch.setattr(app_module, "run_rag_query_events", fake_events)
    _login(client)

    response = client.post(
        "/ask",
        json={
            "query": "Domanda valida?",
            "model": "mistral-medium",
            "stream": True,
            "stream_format": "ndjson",
        },
    )

    assert response.status_code == 200
    assert response.content_type.startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
    assert events == [
        {"type": "token", "text": "Ciao"},
        {"type": "done", "context": []},
    ]


def test_conversation_clear_endpoint_resets_memory(client, flask_app):
    _login(client)
    reset_conversation_store()
    conversation_id = "conv-12345678"
    scoped_id = f"{_workspace_context(flask_app).workspace_id}:{conversation_id}"
    get_conversation_store().append_turn(
        scoped_id,
        user="Domanda valida?",
        assistant="Risposta salvata",
    )

    response = client.delete(f"/conversation/{conversation_id}")

    assert response.status_code == 200
    assert response.get_json() == {"conversation_id": conversation_id, "cleared": True}
    assert get_conversation_store().render_for_prompt(scoped_id) == ""


def test_api_v1_query_streams_ndjson_events(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    monkeypatch.setattr(
        app_module,
        "run_rag_query_events",
        lambda payload, public=False: iter([json.dumps({"type": "done", "context": []}) + "\n"]),
    )

    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "model": "mistral-medium",
            "stream": True,
            "stream_format": "ndjson",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    assert response.content_type.startswith("application/x-ndjson")
    assert json.loads(response.get_data(as_text=True).strip())["type"] == "done"


def test_api_v1_query_accepts_client_context(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    captured = {}

    def fake_query(payload, stream=False, public=False):
        captured["payload"] = payload
        captured["public"] = public
        return {
            "answer": "Risposta mock",
            "context": [],
            "sources": [],
            "model": "mistral-medium",
            "provider": "mistral",
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)

    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "client_context": {
                "site_name": "Example Site",
                "page_title": "Pricing",
                "page_url": "https://example.com/pricing",
                "post_type": "page",
                "locale": "it_IT",
                "instructions": "Visitor is reading the pricing page.",
                "ignored": "not forwarded",
            },
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    assert captured["public"] is True
    assert captured["payload"]["client_context"] == {
        "site_name": "Example Site",
        "page_title": "Pricing",
        "page_url": "https://example.com/pricing",
        "post_type": "page",
        "locale": "it_IT",
        "instructions": "Visitor is reading the pricing page.",
    }


def test_api_v1_query_accepts_response_language(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    captured = {}

    def fake_query(payload, stream=False, public=False):
        captured["payload"] = payload
        return {
            "answer": "Risposta mock",
            "context": [],
            "sources": [],
            "model": "mistral-medium",
            "provider": "mistral",
            "response_language": payload["response_language"],
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)

    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "response_language": "IT",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    assert captured["payload"]["response_language"] == "it"
    assert response.get_json()["response_language"] == "it"


def test_api_v1_query_rejects_invalid_client_context(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "Domanda valida?", "client_context": "Pricing page"},
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 400
    assert response.get_json()["field"] == "client_context"


def test_api_v1_query_rejects_invalid_response_language(client):
    response = client.post(
        "/api/v1/query",
        json={"query": "Domanda valida?", "response_language": "../it"},
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 400
    assert response.get_json()["field"] == "response_language"


def test_api_v1_query_truncates_client_context(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    captured = {}

    def fake_query(payload, stream=False, public=False):
        captured["payload"] = payload
        return {
            "answer": "Risposta mock",
            "context": [],
            "sources": [],
            "model": "mistral-medium",
            "provider": "mistral",
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)

    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "client_context": {
                "site_name": "A" * 200,
                "page_title": "B" * 300,
                "page_url": "https://example.com/" + ("c" * 800),
                "post_type": "page",
                "locale": "it_IT",
                "instructions": "D" * 2000,
            },
        },
        headers={"X-API-Key": "test-api-key"},
    )

    context = captured["payload"]["client_context"]
    assert response.status_code == 200
    assert len("".join(context.values())) <= 2000
    assert len(context["site_name"]) == 120
    assert len(context["page_title"]) == 180
    assert len(context["page_url"]) == 500


def test_api_v1_requires_api_key(client):
    response = client.get("/api/v1/models")

    assert response.status_code == 401
    assert response.get_json()["status"] == "unauthorized"


def test_models_endpoint_marks_configured_default(client, flask_app):
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "rag": {
                "default_provider": "regolo",
                "default_model": "gpt-oss-120b",
            }
        }
    )

    response = client.get("/models")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["default_model"] == "gpt-oss-120b"
    assert payload["default_provider"] == "regolo"
    assert payload["default_model"] == "gpt-oss-120b"
    assert payload["default_value"] == "regolo:gpt-oss-120b"
    default_models = [model for model in payload["models"] if model["is_default"]]
    assert default_models == [
        {
            "id": "gpt-oss-120b",
            "name": "gpt-oss-120b (Regolo AI)",
            "provider": "regolo",
            "provider_name": "Regolo AI",
            "value": "regolo:gpt-oss-120b",
            "is_default": True,
        }
    ]


def test_health_status_includes_system_readiness_fields(flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    import utils.chroma_manager as chroma_manager

    upload_path = Path(flask_app.config["UPLOAD_FOLDER"]) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    profile = app_module._current_index_profile(flask_app)
    FileIndex(flask_app.config["FILE_INDEX"]).record(
        "demo.pdf",
        str(upload_path),
        3,
        status="indexed",
        metadata={"index_profile": profile},
    )
    monkeypatch.setattr(
        chroma_manager,
        "get_collection_status",
        lambda: {"collection": "documents", "documents_count": 3},
    )

    status = app_module._health_status(flask_app, deep=False)

    assert status["tracked_files_count"] == 1
    assert status["indexed_files_count"] == 1
    assert status["stale_index_files_count"] == 0
    assert status["needs_rebuild"] is False
    assert status["system_ready"] is True
    assert status["state_backend"] == "memory"
    assert status["queue_backend"] == "inline"
    assert status["redis_ready"] is True
    assert status["queue_ready"] is True
    assert status["queue_depth"] == 0
    assert status["active_jobs_count"] == 0


def test_health_status_degrades_when_configured_redis_is_unavailable(flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    import utils.state_backend as state_backend

    class BrokenRedis:
        def ping(self):
            raise RuntimeError("redis down")

    monkeypatch.setenv("RAG_STATE_BACKEND", "redis")
    monkeypatch.setattr(state_backend, "redis_connection", lambda: BrokenRedis())

    status = app_module._health_status(flask_app, deep=False)

    assert status["status"] == "degraded"
    assert status["state_backend"] == "redis"
    assert status["redis_ready"] is False
    assert "redis down" in status["redis_error"]


def test_api_v1_query_accepts_valid_api_key(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    monkeypatch.setattr(
        app_module,
        "run_rag_query",
        lambda payload, stream=False, public=False: {
            "answer": "OK",
            "context": [],
            "sources": [],
            "model": payload.get("model"),
            "provider": payload.get("provider"),
            "usage": None,
        },
    )

    response = client.post(
        "/api/v1/query",
        json={"query": "Domanda valida?", "model": "mistral-medium"},
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 200
    assert response.get_json()["answer"] == "OK"


def test_api_scope_denies_missing_query_scope(client, flask_app):
    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    UserStore(flask_app.config["USERS_FILE"]).create_api_key(
        user_id=user["id"],
        name="speech",
        scopes=["speech"],
        api_key_value="speech-key",
    )

    response = client.post(
        "/api/v1/query",
        json={"query": "Domanda valida?", "model": "mistral-medium"},
        headers={"X-API-Key": "speech-key"},
    )

    assert response.status_code == 403
    assert response.get_json()["status"] == "forbidden"


def test_admin_can_save_voice_settings_and_preserve_secret(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    store = SettingsStore(flask_app.config["SETTINGS_FILE"])
    store.update({"voice": {"enabled": True, "base_url": "https://voice.example.com/v1", "api_key": "secret-1234"}})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_voice",
            "voice_enabled": "on",
            "voice_base_url": "https://voice.example.com/v1",
            "voice_api_key": "",
            "voice_stt_model": "whisper-1",
            "voice_stt_language": "it",
            "voice_tts_model": "tts-1",
            "voice_default_voice": "alloy",
            "voice_format": "mp3",
        },
    )

    assert response.status_code == 302
    settings = store.load()
    assert settings["voice"]["api_key"] == "secret-1234"
    assert settings["voice"]["stt_model"] == "whisper-1"
    assert settings["voice"]["stt_language"] == "it"
    assert settings["voice"]["tts_model"] == "tts-1"


def test_api_tts_requires_speech_scope(client):
    response = client.post(
        "/api/v1/tts",
        json={"text": "Read this"},
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 403


def test_api_tts_returns_audio_with_speech_scope(client, flask_app, monkeypatch):
    import utils.voice_provider as voice_provider

    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    UserStore(flask_app.config["USERS_FILE"]).create_api_key(
        user_id=user["id"],
        name="speech",
        scopes=["speech"],
        api_key_value="speech-key",
    )
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "voice": {
                "enabled": True,
                "base_url": "https://voice.example.com/v1",
                "api_key": "voice-key",
                "tts_model": "tts-1",
                "voice": "alloy",
                "format": "mp3",
            },
        }
    )

    class FakeVoiceProvider:
        def synthesize(self, text, voice=None, audio_format=None):
            assert text == "Read this"
            assert audio_format == "mp3"
            return b"audio-bytes"

    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda settings: FakeVoiceProvider())

    response = client.post(
        "/api/v1/tts",
        json={"text": "Read this"},
        headers={"X-API-Key": "speech-key"},
    )

    assert response.status_code == 200
    assert response.data == b"audio-bytes"
    assert response.content_type.startswith("audio/mpeg")


def test_api_audio_upload_transcribes_and_indexes(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.voice_provider as voice_provider

    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "voice": {
                "enabled": True,
                "base_url": "https://voice.example.com/v1",
                "api_key": "voice-key",
                "stt_model": "whisper-1",
            }
        }
    )
    indexed_docs = []
    language_hints = []

    class FakeVoiceProvider:
        def transcribe(self, file_path, language=None):
            language_hints.append(language)
            return "Audio transcript with enough text to index."

    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda settings: FakeVoiceProvider())
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda docs, **kwargs: indexed_docs.extend(docs))
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)

    response = client.post(
        "/api/v1/audio",
        data={"file": (io.BytesIO(MP3_BYTES), "meeting.mp3"), "language": "it"},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["source_type"] == "audio"
    assert payload["transcript"] == "Audio transcript with enough text to index."
    assert payload["language_hint"] == "it"
    assert language_hints == ["it"]
    assert indexed_docs
    assert indexed_docs[0].metadata["source_type"] == "audio"


def test_api_audio_upload_rejects_invalid_language(client, flask_app, monkeypatch):
    import utils.voice_provider as voice_provider

    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "voice": {
                "enabled": True,
                "base_url": "https://voice.example.com/v1",
                "api_key": "voice-key",
                "stt_model": "whisper-1",
            }
        }
    )
    monkeypatch.setattr(voice_provider, "get_voice_provider", lambda settings: None)

    response = client.post(
        "/api/v1/audio",
        data={"file": (io.BytesIO(MP3_BYTES), "meeting.mp3"), "language": "../it"},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["field"] == "language"


def test_api_ocr_extracts_pdf_text_before_provider(client, monkeypatch):
    import utils.pdf_processor as pdf_processor

    monkeypatch.setattr(pdf_processor, "extract_pdf_text", lambda file_path: "Parsed PDF text")

    response = client.post(
        "/api/v1/ocr",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "scan.pdf", "application/pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["text"] == "Parsed PDF text"
    assert payload["method"] == "parsed"
    assert payload["ocr_used"] is False


def test_api_ocr_uses_provider_for_image(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    monkeypatch.setattr(app_module, "_run_ocr_for_config", lambda config, file_path, settings=None: "Image text")

    response = client.post(
        "/api/v1/ocr",
        data={"file": (io.BytesIO(PNG_BYTES), "scan.png", "image/png")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["text"] == "Image text"
    assert payload["method"] == "ocr"
    assert payload["ocr_used"] is True


def test_api_v1_query_rejects_wrong_model_casing(client):
    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "provider": "regolo",
            "model": "llama-3.3-70b-instruct",
        },
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["status"] == "validation_error"
    assert payload["field"] == "model"


def test_admin_pages_require_login(client):
    response = client.get("/admin/config")

    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


def test_admin_login_and_config_update(client, flask_app):
    login = client.post("/admin/login", data={"password": "admin"})
    assert login.status_code == 302

    assert client.get("/admin/config").status_code == 200
    assert client.get("/admin/files").status_code == 200

    response = client.post(
        "/admin/config",
        data={
            "action": "save_rag",
            "chunk_size": "1200",
            "chunk_overlap": "100",
            "query_k": "8",
            "temperature": "0.4",
            "embedding_model": "regolo/Qwen3-Embedding-8B",
            "cache_ttl": "600",
            "default_provider": "mistral",
            "default_model": "mistral-medium",
            "enable_cache": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["rag"]["chunk_size"] == 1200
    assert settings["rag"]["query_k"] == 8
    assert settings["rag"]["embedding_provider"] == "regolo"
    assert settings["rag"]["embedding_model"] == "Qwen3-Embedding-8B"


def test_admin_can_add_custom_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_provider",
            "provider_id": "custom",
            "provider_name": "Custom",
            "base_url": "https://example.com/v1",
            "provider_api_key": "provider-key",
            "models": "custom-a\ncustom-b",
            "provider_default_model": "custom-a",
            "provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["custom_providers"][0]["id"] == "custom"
    assert settings["custom_providers"][0]["models"] == ["custom-a", "custom-b"]


def test_admin_can_add_reranker_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_reranker_provider",
            "reranker_provider_id": "ranker",
            "reranker_provider_name": "Ranker",
            "reranker_base_url": "https://rank.example.com/v1",
            "reranker_provider_api_key": "ranker-key",
            "reranker_models": "rerank-a\nvendor/rerank-b",
            "reranker_provider_default_model": "vendor/rerank-b",
            "reranker_provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["reranker_providers"][0]["id"] == "ranker"
    assert settings["reranker_providers"][0]["models"] == ["rerank-a", "vendor/rerank-b"]
    assert settings["reranker_providers"][0]["default_model"] == "vendor/rerank-b"


def test_admin_can_add_embedding_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_embedding_provider",
            "embedding_provider_id": "embedder",
            "embedding_provider_name": "Embedder",
            "embedding_base_url": "https://embed.example.com/v1",
            "embedding_provider_api_key_env": "EMBEDDER_API_KEY",
            "embedding_provider_requires_api_key": "on",
            "embedding_provider_api_key": "embed-key",
            "embedding_models": "embed-a\nvendor/embed-b",
            "embedding_provider_default_model": "vendor/embed-b",
            "embedding_dimensions": "3072",
            "embedding_provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["embedding_providers"][0]["id"] == "embedder"
    assert settings["embedding_providers"][0]["models"] == ["embed-a", "vendor/embed-b"]
    assert settings["embedding_providers"][0]["default_model"] == "vendor/embed-b"
    assert settings["embedding_providers"][0]["dimensions"] == 3072
    assert settings["embedding_providers"][0]["api_key_env"] == "EMBEDDER_API_KEY"
    assert settings["embedding_providers"][0]["requires_api_key"] is True


def test_admin_can_add_no_auth_embedding_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_embedding_provider",
            "embedding_provider_id": "local-embed",
            "embedding_provider_name": "Local Embed",
            "embedding_base_url": "http://localhost:8001/v1",
            "embedding_provider_api_key": "",
            "embedding_models": "embed-local",
            "embedding_provider_default_model": "embed-local",
            "embedding_dimensions": "384",
            "embedding_provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["embedding_providers"][0]["id"] == "local-embed"
    assert settings["embedding_providers"][0]["requires_api_key"] is False
    assert settings["embedding_providers"][0]["api_key"] == ""


def test_admin_can_add_voice_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_voice_provider",
            "voice_provider_id": "speaker",
            "voice_provider_name": "Speaker",
            "voice_provider_base_url": "https://speaker.example.com/v1",
            "voice_provider_api_key": "speaker-key",
            "voice_provider_stt_model": "whisper-large",
            "voice_provider_tts_model": "tts-large",
            "voice_provider_default_voice": "nova",
            "voice_provider_format": "wav",
            "voice_provider_requires_api_key": "on",
            "voice_provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["voice_providers"][0]["id"] == "speaker"
    assert settings["voice_providers"][0]["base_url"] == "https://speaker.example.com/v1"
    assert settings["voice_providers"][0]["stt_model"] == "whisper-large"
    assert settings["voice_providers"][0]["tts_model"] == "tts-large"
    assert settings["voice_providers"][0]["format"] == "wav"


def test_admin_can_add_ocr_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/config",
        data={
            "action": "save_ocr_provider",
            "ocr_provider_id": "vision-ocr",
            "ocr_provider_name": "Vision OCR",
            "ocr_provider_base_url": "https://ocr.example.com/v1",
            "ocr_provider_api_key": "ocr-key",
            "ocr_provider_api_key_env": "OCR_API_KEY",
            "ocr_provider_requires_api_key": "1",
            "ocr_models": "vision-ocr-a\nvision-ocr-b",
            "ocr_provider_default_model": "vision-ocr-b",
            "ocr_provider_mode": "vision_chat",
            "ocr_provider_output_format": "text",
            "ocr_provider_input_types": ["image", "pdf"],
            "ocr_provider_enabled": "on",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["ocr_providers"][0]["id"] == "vision-ocr"
    assert settings["ocr_providers"][0]["base_url"] == "https://ocr.example.com/v1"
    assert settings["ocr_providers"][0]["models"] == ["vision-ocr-a", "vision-ocr-b"]
    assert settings["ocr_providers"][0]["default_model"] == "vision-ocr-b"
    assert settings["ocr_providers"][0]["input_types"] == ["image", "pdf"]


def test_admin_can_select_custom_embedding_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key": "embed-key",
                    "models": ["vendor/embed-b"],
                    "default_model": "vendor/embed-b",
                    "dimensions": 3072,
                    "enabled": True,
                }
            ]
        }
    )

    response = client.post(
        "/admin/config",
        data={
            "action": "save_rag",
            "chunk_size": "1200",
            "chunk_overlap": "100",
            "query_k": "8",
            "temperature": "0.2",
            "embedding_model": "embedder/vendor/embed-b",
            "cache_ttl": "600",
            "default_provider": "mistral",
            "default_model": "mistral-medium",
            "enable_cache": "on",
        },
    )

    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert response.status_code == 302
    assert settings["rag"]["embedding_provider"] == "embedder"
    assert settings["rag"]["embedding_model"] == "vendor/embed-b"


def test_admin_can_select_custom_reranker_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "reranker_providers": [
                {
                    "id": "ranker",
                    "name": "Ranker",
                    "base_url": "https://rank.example.com/v1",
                    "api_key": "ranker-key",
                    "models": ["vendor/rerank-b"],
                    "default_model": "vendor/rerank-b",
                    "enabled": True,
                }
            ]
        }
    )

    response = client.post(
        "/admin/config",
        data={
            "action": "save_reranker",
            "reranker_enabled": "on",
            "reranker_model": "ranker/vendor/rerank-b",
            "reranker_top_n": "24",
            "reranker_diversity_mode": "mmr",
            "reranker_mmr_lambda": "0.6",
            "reranker_mmr_candidate_pool": "96",
            "reranker_threshold": "1.5",
        },
    )

    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert response.status_code == 302
    assert settings["rag"]["reranker_type"] == "ranker"
    assert settings["rag"]["reranker_model"] == "ranker/vendor/rerank-b"
    assert settings["rag"]["reranker_diversity_mode"] == "mmr"
    assert settings["rag"]["reranker_mmr_lambda"] == 0.6
    assert settings["rag"]["reranker_mmr_candidate_pool"] == 96


def test_admin_can_select_custom_voice_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "voice_providers": [
                {
                    "id": "speaker",
                    "name": "Speaker",
                    "base_url": "https://speaker.example.com/v1",
                    "api_key": "speaker-key",
                    "stt_model": "whisper-large",
                    "tts_model": "tts-large",
                    "voice": "nova",
                    "format": "wav",
                    "enabled": True,
                }
            ]
        }
    )

    response = client.post(
        "/admin/config",
        data={
            "action": "save_voice",
            "voice_enabled": "on",
            "voice_provider": "speaker",
            "voice_base_url": "https://speaker.example.com/v1",
            "voice_api_key": "",
            "voice_requires_api_key": "1",
            "voice_stt_model": "whisper-large",
            "voice_stt_language": "it",
            "voice_tts_model": "tts-large",
            "voice_default_voice": "nova",
            "voice_format": "wav",
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["voice"]["provider"] == "speaker"
    assert settings["voice"]["base_url"] == "https://speaker.example.com/v1"
    assert settings["voice"]["api_key"] == "speaker-key"
    assert settings["voice"]["stt_model"] == "whisper-large"
    assert settings["voice"]["stt_language"] == "it"
    assert settings["voice"]["tts_model"] == "tts-large"
    assert settings["voice"]["format"] == "wav"


def test_admin_can_select_custom_ocr_provider(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "ocr_providers": [
                {
                    "id": "vision-ocr",
                    "name": "Vision OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "api_key": "ocr-key",
                    "models": ["vision-ocr-model"],
                    "default_model": "vision-ocr-model",
                    "input_types": ["image", "pdf"],
                    "enabled": True,
                }
            ]
        }
    )

    response = client.post(
        "/admin/config",
        data={
            "action": "save_ocr",
            "ocr_enabled": "on",
            "ocr_auto_on_empty_pdf": "on",
            "ocr_provider": "vision-ocr",
            "ocr_base_url": "https://ocr.example.com/v1",
            "ocr_api_key": "",
            "ocr_requires_api_key": "1",
            "ocr_default_model": "vision-ocr-model",
            "ocr_mode": "vision_chat",
            "ocr_output_format": "text",
            "ocr_input_types": ["image", "pdf"],
        },
    )

    assert response.status_code == 302
    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert settings["ocr"]["provider"] == "vision-ocr"
    assert settings["ocr"]["base_url"] == "https://ocr.example.com/v1"
    assert settings["ocr"]["api_key"] == "ocr-key"
    assert settings["ocr"]["default_model"] == "vision-ocr-model"
    assert settings["ocr"]["auto_on_empty_pdf"] is True


def test_admin_ocr_save_updates_existing_workspace_for_upload(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    client.post("/admin/login", data={"password": "admin"})
    workspace = _workspace_context(flask_app)
    SettingsStore(workspace.settings_file).update({"ocr": {"enabled": False}})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "ocr_providers": [
                {
                    "id": "vision-ocr",
                    "name": "Vision OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "api_key": "",
                    "requires_api_key": False,
                    "models": ["vision-ocr-model"],
                    "default_model": "vision-ocr-model",
                    "input_types": ["image", "pdf"],
                    "enabled": True,
                }
            ]
        }
    )

    save_response = client.post(
        "/admin/config",
        data={
            "action": "save_ocr",
            "ocr_enabled": "on",
            "ocr_auto_on_empty_pdf": "on",
            "ocr_provider": "vision-ocr",
            "ocr_base_url": "https://ocr.example.com/v1",
            "ocr_api_key": "",
            "ocr_requires_api_key": "0",
            "ocr_default_model": "vision-ocr-model",
            "ocr_mode": "vision_chat",
            "ocr_output_format": "text",
            "ocr_input_types": ["image", "pdf"],
        },
    )

    workspace_settings = SettingsStore(workspace.settings_file).load()
    assert save_response.status_code == 302
    assert workspace_settings["ocr"]["enabled"] is True
    assert workspace_settings["ocr"]["provider"] == "vision-ocr"

    fake_pdf_processor = types.ModuleType("utils.pdf_processor")
    fake_pdf_processor.process_pdf = lambda file_path, settings_path=None: []
    monkeypatch.setitem(sys.modules, "utils.pdf_processor", fake_pdf_processor)
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: None)
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)
    monkeypatch.setattr(
        app_module,
        "_run_ocr_for_config",
        lambda config, file_path, settings=None: "OCR extracted text from scanned PDF.",
    )

    upload_response = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "scan.pdf", "application/pdf")},
        content_type="multipart/form-data",
    )

    payload = upload_response.get_json()
    assert upload_response.status_code == 200
    assert payload["ocr_used"] is True


def test_admin_config_does_not_render_full_provider_or_reranker_secrets(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "custom_providers": [
                {
                    "id": "custom",
                    "name": "Custom",
                    "base_url": "https://example.com/v1",
                    "api_key": "provider-secret-1234",
                    "models": ["rerank-model"],
                    "default_model": "rerank-model",
                    "enabled": True,
                }
            ],
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key": "embed-secret-1234",
                    "models": ["embed-model"],
                    "default_model": "embed-model",
                    "dimensions": 1536,
                    "enabled": True,
                }
            ],
            "reranker_providers": [
                {
                    "id": "ranker",
                    "name": "Ranker",
                    "base_url": "https://rank.example.com/v1",
                    "api_key": "ranker-secret-1234",
                    "models": ["rerank-model"],
                    "default_model": "rerank-model",
                    "enabled": True,
                }
            ],
            "voice_providers": [
                {
                    "id": "speaker",
                    "name": "Speaker",
                    "base_url": "https://speaker.example.com/v1",
                    "api_key": "speaker-secret-1234",
                    "stt_model": "whisper-1",
                    "tts_model": "tts-1",
                    "voice": "nova",
                    "format": "wav",
                    "enabled": True,
                }
            ],
            "ocr_providers": [
                {
                    "id": "vision-ocr",
                    "name": "Vision OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "api_key": "ocr-secret-1234",
                    "models": ["vision-ocr-model"],
                    "default_model": "vision-ocr-model",
                    "enabled": True,
                }
            ],
            "rag": {
                "reranker_type": "custom",
                "reranker_model": "ranker/rerank-model",
                "reranker_regolo_api_key": "regolo-secret-1234",
            },
        }
    )

    response = client.get("/admin/config")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "provider-secret-1234" not in html
    assert "embed-secret-1234" not in html
    assert "ranker-secret-1234" not in html
    assert "speaker-secret-1234" not in html
    assert "ocr-secret-1234" not in html
    assert "regolo-secret-1234" not in html
    assert "data-api-key" not in html
    assert "OpenAI-Compatible Embeddings Providers" in html
    assert "OpenAI-Compatible Voice Providers" in html
    assert "OpenAI-Compatible OCR Providers" in html
    assert "Voice &amp; Audio" in html
    assert "Configure separate OpenAI-compatible endpoints for different steps of the RAG flow." in html


def test_admin_reranker_keeps_existing_secret_when_password_field_blank(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "rag": {
                "reranker_enabled": True,
                "reranker_type": "regolo",
                "reranker_model": "regolo/Qwen3-Reranker-4B",
                "reranker_top_n": 20,
                "reranker_diversity_mode": "none",
                "reranker_threshold": 0.0,
                "reranker_regolo_api_key": "regolo-secret-1234",
            }
        }
    )

    response = client.post(
        "/admin/config",
        data={
            "action": "save_reranker",
            "reranker_enabled": "on",
            "reranker_model": "regolo/Qwen3-Reranker-4B",
            "reranker_top_n": "12",
            "reranker_diversity_mode": "source_diversity",
            "reranker_threshold": "2.5",
            "reranker_regolo_api_key": "",
        },
    )

    settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    assert response.status_code == 302
    assert settings["rag"]["reranker_regolo_api_key"] == "regolo-secret-1234"
    assert settings["rag"]["reranker_top_n"] == 12
    assert settings["rag"]["reranker_diversity_mode"] == "source_diversity"
    assert settings["rag"]["reranker_threshold"] == 2.5


def test_api_file_upload_uses_upload_permission(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(
        app_module,
        "_process_upload",
        lambda app, **kwargs: {"message": "uploaded", "filename": "demo.pdf", "chunks": 2},
    )

    response = client.post("/api/v1/files", headers={"X-API-Key": "test-api-key"})

    assert response.status_code == 200
    assert response.get_json()["filename"] == "demo.pdf"


def test_api_file_upload_async_returns_job_status(client, monkeypatch):
    app_module = importlib.import_module("app.app")

    monkeypatch.setattr(
        app_module,
        "_index_saved_document_upload",
        lambda config, filename, file_path, extension, **kwargs: {
            "message": f"{filename} caricato e indicizzato",
            "filename": filename,
            "chunks": 2,
            "status": "indexed",
            "source_type": "pdf",
            "document_id": "doc-async",
            "ocr_used": False,
        },
    )

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)

    response = client.post(
        "/api/v1/files?async=true",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "async.pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    status = client.get(
        f"/api/v1/jobs/{payload['job_id']}",
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 202
    assert payload["type"] == "file_upload"
    assert payload["status"] == "completed"
    assert payload["result"]["filename"] == "async.pdf"
    assert status.status_code == 200
    assert status.get_json()["result"]["document_id"] == "doc-async"


def test_api_audio_upload_async_queues_redis_job(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    enqueued = []

    monkeypatch.setattr(app_module, "configured_queue_backend", lambda: "redis")
    monkeypatch.setattr(
        app_module,
        "_enqueue_upload_job",
        lambda job_id, config, upload_type, upload: enqueued.append(
            {
                "job_id": job_id,
                "config": config,
                "upload_type": upload_type,
                "upload": upload,
            }
        ),
    )

    response = client.post(
        "/api/v1/audio?async=true",
        data={"file": (io.BytesIO(MP3_BYTES), "meeting.mp3"), "language": "it"},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    status = client.get(
        f"/api/v1/jobs/{payload['job_id']}",
        headers={"X-API-Key": "test-api-key"},
    )

    assert response.status_code == 202
    assert payload["status"] == "queued"
    assert payload["type"] == "audio_upload"
    assert enqueued[0]["job_id"] == payload["job_id"]
    assert enqueued[0]["upload_type"] == "audio"
    assert enqueued[0]["upload"]["language_override"] == "it"
    assert status.status_code == 200
    assert status.get_json()["current_file"] == "meeting.mp3"


def test_api_file_upload_rejects_extension_spoofed_pdf(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    monkeypatch.setattr(
        app_module,
        "_index_saved_document_upload",
        lambda *args, **kwargs: pytest.fail("spoofed upload should not be indexed"),
    )

    response = client.post(
        "/api/v1/files",
        data={"file": (io.BytesIO(b"not a pdf"), "demo.pdf", "application/pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["field"] == "file"
    assert "estensione .pdf" in payload["error"]


def test_api_file_upload_replaces_existing_source_chunks(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    deleted_sources = []
    added_documents = []
    fake_pdf_processor = types.ModuleType("utils.pdf_processor")
    fake_pdf_processor.process_pdf = lambda file_path, settings_path=None: [
        SimpleNamespace(
            page_content="testo",
            metadata={"source": file_path, "document_id": "abc123", "chunk_id": 0},
        )
    ]
    monkeypatch.setitem(sys.modules, "utils.pdf_processor", fake_pdf_processor)
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: deleted_sources.append(source) or 3)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.post(
        "/api/v1/files",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "demo.pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    expected_source = str(Path(_workspace_context(flask_app).upload_folder) / "demo.pdf")
    assert response.status_code == 200
    assert deleted_sources == [expected_source]
    assert len(added_documents) == 1


def test_api_file_upload_uses_ocr_fallback_when_pdf_has_no_text(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    added_documents = []
    fake_pdf_processor = types.ModuleType("utils.pdf_processor")
    fake_pdf_processor.process_pdf = lambda file_path, settings_path=None: []
    monkeypatch.setitem(sys.modules, "utils.pdf_processor", fake_pdf_processor)
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    def fake_ocr_documents(config, file_path, parsed_documents=None, parse_error=""):
        assert parsed_documents == []
        return [
            SimpleNamespace(
                page_content="OCR extracted text",
                metadata={
                    "source": file_path,
                    "source_type": "pdf",
                    "document_id": "ocr-document",
                    "source_id": "ocr-source",
                    "chunk_id": 0,
                },
            )
        ], {"ocr_used": True, "ocr_provider": "vision-ocr", "ocr_model": "vision-ocr-model"}, ""

    monkeypatch.setattr(app_module, "_ocr_documents_for_config", fake_ocr_documents)

    response = client.post(
        "/api/v1/files",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "scan.pdf", "application/pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    entry = FileIndex(_workspace_context(flask_app).file_index).get("scan.pdf")
    assert response.status_code == 200
    assert payload["ocr_used"] is True
    assert added_documents[0].page_content == "OCR extracted text"
    assert entry["ocr_used"] is True
    assert entry["ocr_provider"] == "vision-ocr"


def test_api_file_upload_indexes_markdown_document(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    added_documents = []
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.post(
        "/api/v1/files",
        data={"file": (io.BytesIO(b"# Notes\n\nMarkdown content"), "notes.md", "text/markdown")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    entry = FileIndex(_workspace_context(flask_app).file_index).get("notes.md")
    assert response.status_code == 200
    assert payload["source_type"] == "markdown"
    assert payload["ocr_used"] is False
    assert added_documents[0].metadata["source_type"] == "markdown"
    assert "Markdown content" in added_documents[0].page_content
    assert entry["source_type"] == "markdown"


def test_api_file_upload_preserves_relative_path_metadata(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    workspace = _workspace_context(flask_app)
    deleted_sources = []
    added_documents = []
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: deleted_sources.append(source) or 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.post(
        "/api/v1/files",
        data={
            "file": (io.BytesIO(b"customer,total\nacme,42\n"), "report.csv", "text/csv"),
            "relative_path": "contracts/2026/report.csv",
        },
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    expected_path = Path(workspace.upload_folder) / "contracts" / "2026" / "report.csv"
    payload = response.get_json()
    entry = FileIndex(workspace.file_index).get("contracts/2026/report.csv")
    assert response.status_code == 200
    assert payload["filename"] == "contracts/2026/report.csv"
    assert payload["relative_path"] == "contracts/2026/report.csv"
    assert payload["source_type"] == "csv"
    assert expected_path.read_bytes() == b"customer,total\nacme,42\n"
    assert deleted_sources == [str(expected_path)]
    assert added_documents[0].metadata["relative_path"] == "contracts/2026/report.csv"
    assert added_documents[0].metadata["source_type"] == "csv"
    assert entry["filename"] == "contracts/2026/report.csv"
    assert entry["path"] == str(expected_path)
    assert entry["relative_path"] == "contracts/2026/report.csv"


def test_api_file_upload_avoids_same_basename_collision_with_relative_path(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    workspace = _workspace_context(flask_app)
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(chroma_manager, "find_document_by_id", lambda document_id, exclude_source=None, **kwargs: None)
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: None)
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    for relative_path, content in [
        ("contracts/shared.txt", b"Contract folder copy"),
        ("policies/shared.txt", b"Policy folder copy"),
    ]:
        response = client.post(
            "/api/v1/files",
            data={
                "file": (io.BytesIO(content), "shared.txt", "text/plain"),
                "relative_path": relative_path,
            },
            headers={"X-API-Key": "test-api-key"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 200

    file_index = FileIndex(workspace.file_index)
    contract_entry = file_index.get("contracts/shared.txt")
    policy_entry = file_index.get("policies/shared.txt")
    assert contract_entry is not None
    assert policy_entry is not None
    assert contract_entry["path"] == str(Path(workspace.upload_folder) / "contracts" / "shared.txt")
    assert policy_entry["path"] == str(Path(workspace.upload_folder) / "policies" / "shared.txt")
    assert Path(contract_entry["path"]).read_bytes() == b"Contract folder copy"
    assert Path(policy_entry["path"]).read_bytes() == b"Policy folder copy"


def test_api_file_upload_skips_duplicate_document_id(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    added_documents = []
    fake_pdf_processor = types.ModuleType("utils.pdf_processor")
    fake_pdf_processor.process_pdf = lambda file_path, settings_path=None: [
        SimpleNamespace(
            page_content="testo",
            metadata={
                "source": file_path,
                "source_id": "src-new",
                "document_id": "same-document",
                "chunk_id": 0,
            },
        )
    ]
    monkeypatch.setitem(sys.modules, "utils.pdf_processor", fake_pdf_processor)
    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 0)
    monkeypatch.setattr(
        chroma_manager,
        "find_document_by_id",
        lambda document_id, exclude_source=None, **kwargs: {
            "document_id": document_id,
            "source": "app/uploads/original.pdf",
            "chunk_id": "src-old:same-document:chunk:0",
            "chunks": 4,
        },
    )
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.post(
        "/api/v1/files",
        data={"file": (io.BytesIO(b"%PDF-1.4"), "copy.pdf")},
        headers={"X-API-Key": "test-api-key"},
        content_type="multipart/form-data",
    )

    payload = response.get_json()
    entry = FileIndex(_workspace_context(flask_app).file_index).get("copy.pdf")
    assert response.status_code == 200
    assert payload["status"] == "duplicate"
    assert payload["chunks"] == 0
    assert payload["duplicate_of_source"] == "app/uploads/original.pdf"
    assert added_documents == []
    assert entry["status"] == "duplicate"
    assert entry["document_id"] == "same-document"
    assert entry["indexed_chunks"] == 4


def test_api_file_delete_removes_chroma_chunks_file_index_and_upload(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    workspace = _workspace_context(flask_app)
    upload_path = Path(workspace.upload_folder) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    FileIndex(workspace.file_index).record("demo.pdf", str(upload_path), 2, status="indexed")

    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 2)
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.delete("/api/v1/files/demo.pdf", headers={"X-API-Key": "test-api-key"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["chunks_deleted"] == 2
    assert payload["file_deleted"] is True
    assert FileIndex(workspace.file_index).get("demo.pdf") is None
    assert not upload_path.exists()


def test_api_file_delete_supports_nested_relative_path(client, flask_app, monkeypatch):
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine

    workspace = _workspace_context(flask_app)
    upload_path = Path(workspace.upload_folder) / "contracts" / "2026" / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    FileIndex(workspace.file_index).record(
        "contracts/2026/demo.pdf",
        str(upload_path),
        2,
        status="indexed",
        metadata={"relative_path": "contracts/2026/demo.pdf"},
    )

    monkeypatch.setattr(chroma_manager, "delete_documents_by_source", lambda source, **kwargs: 2)
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: None)

    response = client.delete(
        "/api/v1/files/contracts/2026/demo.pdf",
        headers={"X-API-Key": "test-api-key"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["filename"] == "contracts/2026/demo.pdf"
    assert payload["file_deleted"] is True
    assert FileIndex(workspace.file_index).get("contracts/2026/demo.pdf") is None
    assert not upload_path.exists()
    assert not upload_path.parent.exists()


def test_api_file_delete_returns_404_for_unknown_file(client):
    response = client.delete("/api/v1/files/missing.pdf", headers={"X-API-Key": "test-api-key"})

    assert response.status_code == 404
    assert response.get_json()["status"] == "not_found"


def test_rebuild_index_job_resets_collection_and_reindexes_files(flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    import utils.chroma_manager as chroma_manager
    import utils.rag_engine as rag_engine
    from utils.providers.embedding_factory import EmbeddingFactory

    workspace = _workspace_context(flask_app)
    workspace_config = workspace.as_config()
    upload_path = Path(workspace.upload_folder) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    FileIndex(workspace.file_index).record("demo.pdf", str(upload_path), 1, status="indexed")

    reset_calls = []
    added_documents = []
    cache_clears = []
    monkeypatch.setattr(chroma_manager, "reset_chroma_collection", lambda **kwargs: reset_calls.append(True))
    monkeypatch.setattr(chroma_manager, "add_documents_to_chroma", lambda documents, **kwargs: added_documents.extend(documents))
    monkeypatch.setattr(rag_engine, "clear_cache", lambda: cache_clears.append(True))
    monkeypatch.setattr(EmbeddingFactory, "reset_cache", lambda: None)
    fake_pdf_processor = types.ModuleType("utils.pdf_processor")
    fake_pdf_processor.process_pdf = lambda file_path, settings_path=None: [
        SimpleNamespace(
            page_content="testo",
            metadata={
                "source": file_path,
                "source_id": "src123",
                "document_id": "doc123",
                "chunk_id": 0,
            },
        )
    ]
    monkeypatch.setitem(sys.modules, "utils.pdf_processor", fake_pdf_processor)

    profile = app_module._current_index_profile_for_config(workspace_config)
    job_id = "job-test"
    app_module.get_job_store().create_rebuild_job(
        {
            "id": job_id,
            "status": "running",
            "message": "",
            "processed": 0,
            "total": 1,
            "current_file": "",
            "errors": [],
            "profile": profile,
            "started_at": 0,
            "finished_at": None,
        }
    )

    app_module._run_rebuild_index_job(
        job_id,
        workspace_config,
        profile,
        FileIndex(workspace.file_index).list(),
    )

    job = app_module._get_rebuild_job(job_id)
    entry = FileIndex(workspace.file_index).get("demo.pdf")
    assert reset_calls == [True]
    assert len(added_documents) == 1
    assert cache_clears
    assert job["status"] == "completed"
    assert job["processed"] == 1
    assert entry["status"] == "indexed"
    assert entry["chunks"] == 1
    assert entry["document_id"] == "doc123"
    assert entry["index_profile"] == profile


def test_rebuild_index_start_uses_redis_queue_backend(flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    workspace = _workspace_context(flask_app)
    upload_path = Path(workspace.upload_folder) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    FileIndex(workspace.file_index).record("demo.pdf", str(upload_path), 1, status="indexed")
    enqueued = []

    monkeypatch.setattr(app_module, "configured_queue_backend", lambda: "redis")
    monkeypatch.setattr(
        app_module,
        "_enqueue_rebuild_index_job",
        lambda job_id, config, profile, entries, ocr_profile: enqueued.append(
            {
                "job_id": job_id,
                "config": config,
                "profile": profile,
                "entries": entries,
                "ocr_profile": ocr_profile,
            }
        ),
    )

    payload, status = app_module._start_rebuild_index_job(flask_app, config=workspace.as_config())

    assert status == 202
    assert payload["status"] == "queued"
    assert enqueued
    assert enqueued[0]["job_id"] == payload["job_id"]


def test_rebuild_index_start_rejects_concurrent_job(flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    workspace = _workspace_context(flask_app)
    monkeypatch.setattr(app_module, "configured_queue_backend", lambda: "inline")
    app_module.get_job_store().create_rebuild_job(
        {
            "id": "active-job",
            "status": "running",
            "message": "",
            "processed": 0,
            "total": 0,
            "current_file": "",
            "errors": [],
            "profile": {},
            "started_at": 0,
            "finished_at": None,
        }
    )
    second, second_status = app_module._start_rebuild_index_job(flask_app, config=workspace.as_config())

    assert second_status == 409
    assert second["job_id"] == "active-job"


def test_index_rebuild_status_detects_stale_index_profile(flask_app):
    workspace = _workspace_context(flask_app)
    workspace_config = workspace.as_config()
    upload_path = Path(workspace.upload_folder) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    FileIndex(workspace.file_index).record(
        "demo.pdf",
        str(upload_path),
        1,
        status="indexed",
        metadata={"index_profile": {"embedding_provider": "old"}},
    )
    app_module = importlib.import_module("app.app")

    status = app_module._index_rebuild_status(flask_app, config=workspace_config)

    assert status["needs_rebuild"] is True
    assert status["stale_count"] == 1


def test_index_rebuild_status_ignores_legacy_ocr_profile_for_non_ocr_files(flask_app):
    app_module = importlib.import_module("app.app")
    workspace = _workspace_context(flask_app)
    workspace_config = workspace.as_config()
    upload_path = Path(workspace.upload_folder) / "demo.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    legacy_profile = {
        **app_module._current_index_profile_for_config(workspace_config),
        "ocr_enabled": True,
        "ocr_auto_on_empty_pdf": True,
        "ocr_provider": "legacy-ocr",
        "ocr_model": "legacy-model",
        "ocr_mode": "vision_chat",
    }
    FileIndex(workspace.file_index).record(
        "demo.pdf",
        str(upload_path),
        1,
        status="indexed",
        metadata={"index_profile": legacy_profile},
    )

    status = app_module._index_rebuild_status(flask_app, config=workspace_config)

    assert status["needs_rebuild"] is False
    assert status["stale_count"] == 0


def test_index_rebuild_status_detects_stale_ocr_profile_for_ocr_files(flask_app):
    app_module = importlib.import_module("app.app")
    workspace = _workspace_context(flask_app)
    workspace_config = workspace.as_config()
    SettingsStore(workspace.settings_file).update(
        {
            "ocr": {
                "enabled": True,
                "provider": "vision-ocr",
                "default_model": "new-ocr-model",
                "ocr_mode": "vision_chat",
                "output_format": "text",
            }
        }
    )
    upload_path = Path(workspace.upload_folder) / "scanned.pdf"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(b"%PDF-1.4")
    old_profile = {
        **app_module._current_index_profile_for_config(workspace_config, include_ocr=True),
        "ocr_model": "old-ocr-model",
    }
    FileIndex(workspace.file_index).record(
        "scanned.pdf",
        str(upload_path),
        1,
        status="indexed",
        metadata={"index_profile": old_profile, "ocr_used": True},
    )

    status = app_module._index_rebuild_status(flask_app, config=workspace_config)

    assert status["needs_rebuild"] is True
    assert status["stale_count"] == 1


def test_documents_for_rebuild_supports_text_file(flask_app):
    app_module = importlib.import_module("app.app")
    upload_path = Path(flask_app.config["UPLOAD_FOLDER"]) / "notes.txt"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_text("Plain text content for rebuild.", encoding="utf-8")

    file_path, documents, source_type, extra = app_module._documents_for_rebuild(
        flask_app.config,
        {"filename": "notes.txt", "path": str(upload_path), "source_type": "text"},
    )

    assert file_path == str(upload_path)
    assert source_type == "text"
    assert extra == {}
    assert documents[0].metadata["source_type"] == "text"
    assert "Plain text content" in documents[0].page_content


def test_admin_file_delete_route_uses_shared_delete_handler(client, monkeypatch):
    app_module = importlib.import_module("app.app")
    client.post("/admin/login", data={"password": "admin"})
    monkeypatch.setattr(
        app_module,
        "_delete_indexed_file",
        lambda app, filename, **kwargs: {
            "message": f"{filename} rimosso dalla knowledge base",
            "filename": filename,
            "chunks_deleted": 1,
            "file_deleted": True,
        },
    )

    response = client.post("/admin/files/delete", data={"filename": "demo.pdf"})

    assert response.status_code == 302
    assert "/admin/files" in response.headers["Location"]


def test_upload_to_chat_returns_file_id_not_host_path(client, flask_app):
    app_module = importlib.import_module("app.app")
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/upload-to-chat",
        data={"file": (io.BytesIO(b"a,b\n1,2\n"), "demo.csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert re.match(r"^[0-9a-f]{32}$", payload["file_id"])
    assert "path" not in payload
    config = _workspace_context(flask_app).as_config()
    stored = Path(app_module._chat_upload_dir(config)) / f"{payload['file_id']}_demo.csv"
    assert stored.read_text() == "a,b\n1,2\n"


def test_resolve_chat_attachments_ignores_client_supplied_path(tmp_path):
    app_module = importlib.import_module("app.app")
    config = {"UPLOAD_FOLDER": str(tmp_path / "uploads")}
    upload_dir = Path(app_module._chat_upload_dir(config))
    upload_dir.mkdir(parents=True)
    file_id = "a" * 32
    stored = upload_dir / f"{file_id}_demo.csv"
    stored.write_text("a,b\n1,2\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")

    resolved = app_module._resolve_chat_attachments(
        config,
        [{"id": file_id, "name": "demo.csv", "path": str(secret)}],
    )

    assert resolved[0]["path"] == str(stored)
    assert resolved[0]["container_path"] == "/data/demo.csv"


def test_code_interpreter_prompt_includes_rag_context_and_data_path(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    rag_engine = importlib.import_module("utils.rag_engine")
    provider_factory = importlib.import_module("utils.providers.provider_factory")
    client.post("/admin/login", data={"password": "admin"})
    upload = client.post(
        "/upload-to-chat",
        data={"file": (io.BytesIO(b"revenue\n10\n"), "report.csv")},
        content_type="multipart/form-data",
    )
    file_id = upload.get_json()["file_id"]
    captured = {}

    context_docs = [
        SimpleNamespace(
            page_content="Regola aziendale: usa il margine netto nelle analisi executive.",
            metadata={"source": "policy.pdf"},
        )
    ]

    def fake_prepare_rag_context(*args, **kwargs):
        return {
            "settings": {"rag": {"temperature": 0.2, "query_k": 5, "enable_cache": False}},
            "provider": "fake",
            "model": "fake-model",
            "provider_config": {"name": "Fake"},
            "temperature": 0.2,
            "k": 5,
            "response_language": "it",
            "conversation_context": "Turno precedente utile.",
            "context_docs": context_docs,
        }

    class FakeProvider:
        provider_name = "Fake"

        def generate(self, system, user, model, temperature):
            captured["system"] = system
            captured["user"] = user
            return "import pandas as pd\nprint(pd.read_csv('/data/report.csv').shape)"

    monkeypatch.setattr(rag_engine, "prepare_rag_context", fake_prepare_rag_context)
    monkeypatch.setattr(
        provider_factory.ProviderFactory,
        "get_provider",
        staticmethod(lambda model=None, provider=None, settings=None: FakeProvider()),
    )
    monkeypatch.setattr(
        app_module,
        "_execute_interpreter_code",
        lambda prepared, code: {"success": True, "text": "ok", "images": []},
    )

    response = client.post(
        "/ask",
        json={
            "query": "Analizza il report",
            "use_code_interpreter": True,
            "attached_files": [{"id": file_id, "name": "report.csv"}],
        },
    )

    assert response.status_code == 200
    assert "/data/report.csv" in captured["system"]
    assert "revenue" in captured["system"]
    assert "Regola aziendale" in captured["system"]
    assert response.get_json()["context"][0]["metadata"]["source"] == "policy.pdf"


def test_ask_with_attachment_and_code_interpreter_off_uses_ephemeral_rag(client, flask_app, monkeypatch):
    rag_engine = importlib.import_module("utils.rag_engine")
    temp_rag = importlib.import_module("utils.temporary_attachment_rag")
    client.post("/admin/login", data={"password": "admin"})
    upload = client.post(
        "/upload-to-chat",
        data={"file": (io.BytesIO(b"temporary alpha attachment"), "notes.txt")},
        content_type="multipart/form-data",
    )
    file_id = upload.get_json()["file_id"]
    captured = {}
    chroma_doc = SimpleNamespace(
        page_content="Persistent Chroma context",
        metadata={"source": "kb.pdf", "source_type": "pdf"},
    )

    class FakeEmbeddingProvider:
        def encode_query(self, query):
            return [1.0, 0.0]

        def encode_documents(self, texts):
            return [[1.0, 0.0] for _text in texts]

    monkeypatch.setattr(
        temp_rag.EmbeddingFactory,
        "get_provider",
        staticmethod(lambda model_name=None: FakeEmbeddingProvider()),
    )
    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "resolve",
        staticmethod(lambda model=None, provider=None, settings=None: ("fake", "fake-model", {"name": "Fake"})),
    )
    monkeypatch.setattr(rag_engine, "_get_context", lambda *args, **kwargs: [chroma_doc])

    def fake_generate_response(query, context_docs, **kwargs):
        captured["context_docs"] = context_docs
        return iter(["Risposta con allegato"])

    monkeypatch.setattr(rag_engine, "generate_response", fake_generate_response)

    response = client.post(
        "/ask",
        json={
            "query": "usa alpha",
            "use_code_interpreter": False,
            "attached_files": [{"id": file_id, "name": "notes.txt"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["answer"] == "Risposta con allegato"
    assert any(doc.metadata.get("source") == "kb.pdf" for doc in captured["context_docs"])
    assert any(
        doc.metadata.get("source_type") == "temporary_attachment"
        for doc in captured["context_docs"]
    )
    assert any(ctx["metadata"].get("temporary_attachment") is True for ctx in payload["context"])


def test_missing_default_provider_file_is_visible_on_app_load(tmp_path, monkeypatch):
    missing = tmp_path / "missing-default-providers.json"
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("RAG_DEFAULT_PROVIDERS_FILE", str(missing))
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_FILE": str(tmp_path / "users.json"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
        }
    )
    client = app.test_client()

    client.post("/admin/login", data={"password": "admin"})
    home = client.get("/")
    models = client.get("/models")

    assert home.status_code == 500
    assert "Providers and Models Not Configured" in home.get_data(as_text=True)
    assert models.status_code == 500
    assert models.get_json()["status"] == "model_configuration_error"
