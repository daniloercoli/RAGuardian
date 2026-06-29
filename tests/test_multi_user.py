import importlib
import json
from pathlib import Path

import pytest

from app import create_app
from app.utils.file_index import FileIndex
from app.utils.job_store import get_job_store
from app.utils.secret_store import SecretStore
from app.utils.settings_store import SettingsStore
from app.utils.user_store import UserStore
from app.utils.workspace import workspace_for_user


@pytest.fixture
def flask_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    return create_app(
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
            "MAX_UPLOAD_SIZE_MB": 5,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
        }
    )


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def test_first_login_bootstraps_admin_and_user_role_is_not_admin(client, flask_app):
    response = client.post("/admin/login", data={"password": "admin"})

    assert response.status_code == 302
    users = UserStore(flask_app.config["USERS_FILE"]).list()
    assert len(users) == 1
    assert users[0]["role"] == "admin"
    assert users[0]["email"] == "admin@example.local"

    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_store.create_user(
        email="person@example.com",
        password="secret-pass",
        display_name="Person",
        role="user",
    )
    normal_client = flask_app.test_client()
    login = normal_client.post(
        "/admin/login",
        data={"email": "person@example.com", "password": "secret-pass"},
    )

    assert login.status_code == 302
    assert normal_client.get("/admin/files").status_code == 200
    assert normal_client.get("/admin/config").status_code == 403


def test_workspace_context_isolates_settings_file_index_uploads_and_collection(flask_app):
    store = UserStore(flask_app.config["USERS_FILE"])
    alice = store.create_user(email="alice@example.com", password="alice-pass")
    bob = store.create_user(email="bob@example.com", password="bob-pass")

    alice_workspace = workspace_for_user(alice, app=flask_app)
    bob_workspace = workspace_for_user(bob, app=flask_app)

    assert alice_workspace.workspace_id != bob_workspace.workspace_id
    assert alice_workspace.settings_file != bob_workspace.settings_file
    assert alice_workspace.file_index != bob_workspace.file_index
    assert alice_workspace.upload_folder != bob_workspace.upload_folder
    assert alice_workspace.chroma_collection != bob_workspace.chroma_collection

    FileIndex(alice_workspace.file_index).record("a.pdf", "/tmp/a.pdf", 1, status="indexed")
    SettingsStore(alice_workspace.settings_file).update(
        {"data_sources": [{"id": "alice-mail", "plugin": "email_imap", "enabled": True}]}
    )

    assert FileIndex(bob_workspace.file_index).list() == []
    assert SettingsStore(bob_workspace.settings_file).load()["data_sources"] == []


def test_admin_api_keys_are_saved_in_user_store(client, flask_app):
    store = UserStore(flask_app.config["USERS_FILE"])
    admin = store.create_user(
        email="admin@example.local",
        password="admin",
        display_name="Admin",
        role="admin",
    )
    client.post("/admin/login", data={"email": "admin@example.local", "password": "admin"})

    response = client.post(
        "/admin/api-keys",
        data={
            "action": "create",
            "user_id": admin["id"],
            "name": "local-client",
            "scopes": ["query", "ingest"],
            "enabled": "on",
        },
    )

    global_settings = SettingsStore(flask_app.config["SETTINGS_FILE"]).load()
    keys = UserStore(flask_app.config["USERS_FILE"]).get_api_keys(admin["id"], include_raw=True)

    assert response.status_code == 302
    assert global_settings["auth"]["api_keys"] == []
    assert keys[0]["name"] == "local-client"
    assert keys[0]["scopes"] == ["query", "ingest"]


def test_api_key_resolves_to_owning_user_workspace(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    from flask import request

    store = UserStore(flask_app.config["USERS_FILE"])
    alice = store.create_user(email="alice@example.com", password="alice-pass")
    bob = store.create_user(email="bob@example.com", password="bob-pass")
    alice_workspace = workspace_for_user(alice, app=flask_app)
    bob_workspace = workspace_for_user(bob, app=flask_app)
    store.create_api_key(
        user_id=alice["id"],
        name="alice",
        scopes=["query"],
        api_key_value="alice-key",
    )
    store.create_api_key(
        user_id=bob["id"],
        name="bob",
        scopes=["query"],
        api_key_value="bob-key",
    )
    captured = {}

    def fake_query(payload, stream=False, public=False):
        captured["api_key"] = request.api_key
        return {
            "answer": "ok",
            "context": [],
            "sources": [],
            "model": "m",
            "provider": "p",
            "usage": None,
        }

    monkeypatch.setattr(app_module, "run_rag_query", fake_query)

    response = client.post(
        "/api/v1/query",
        json={"query": "Domanda valida?"},
        headers={"X-API-Key": "bob-key"},
    )

    assert response.status_code == 200
    assert captured["api_key"]["user_id"] == bob["id"]
    assert captured["api_key"]["workspace_id"] == bob_workspace.workspace_id


def test_api_query_model_validation_uses_api_key_workspace_settings(client, flask_app, monkeypatch):
    app_module = importlib.import_module("app.app")
    store = UserStore(flask_app.config["USERS_FILE"])
    alice = store.create_user(email="alice@example.com", password="alice-pass")
    alice_workspace = workspace_for_user(alice, app=flask_app)
    store.create_api_key(
        user_id=alice["id"],
        name="alice",
        scopes=["query"],
        api_key_value="alice-key",
    )
    SettingsStore(flask_app.config["SETTINGS_FILE"]).update(
        {
            "custom_providers": [
                {
                    "id": "global-only",
                    "name": "Global Only",
                    "base_url": "https://global.example.com/v1",
                    "models": ["global-model"],
                    "default_model": "global-model",
                    "enabled": True,
                }
            ]
        }
    )

    monkeypatch.setattr(app_module, "run_rag_query", lambda *args, **kwargs: pytest.fail("query should not run"))

    response = client.post(
        "/api/v1/query",
        json={
            "query": "Domanda valida?",
            "provider": "global-only",
            "model": "global-model",
        },
        headers={"X-API-Key": "alice-key"},
    )

    assert response.status_code == 400
    assert response.get_json()["field"] == "provider"


def test_secret_store_encrypts_values_without_plaintext(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(str(path), key="test-secret-key")

    ref = store.set_secret("workspace-a", "mail:password", "very-secret-value")

    assert store.get_secret(ref) == "very-secret-value"
    raw = path.read_text(encoding="utf-8")
    assert "very-secret-value" not in raw
    assert json.loads(raw)[ref]["ciphertext"]


def test_data_source_password_is_saved_as_user_secret(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "legal-mailbox",
            "name": "Legal Mailbox",
            "plugin": "email_imap",
            "enabled": "on",
            "host": "imap.example.com",
            "port": "993",
            "use_ssl": "on",
            "username": "legal@example.com",
            "password": "mail-secret-value",
            "folder": "INBOX",
            "include_body": "on",
            "include_attachments": "on",
            "max_messages": "10",
        },
    )

    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    workspace = workspace_for_user(user, app=flask_app)
    settings = SettingsStore(workspace.settings_file).load()
    source = settings["data_sources"][0]

    assert response.status_code == 302
    assert "password" not in source["config"]
    assert source["secrets"]["password"]["mode"] == "user_secret"
    assert "mail-secret-value" not in Path(flask_app.config["SECRETS_FILE"]).read_text(encoding="utf-8")
    assert SecretStore(flask_app.config["SECRETS_FILE"], key=flask_app.config["SECRET_KEY"]).get_secret(
        source["secrets"]["password"]["ref"]
    ) == "mail-secret-value"


def test_job_status_is_hidden_across_workspaces(client, flask_app):
    store = UserStore(flask_app.config["USERS_FILE"])
    alice = store.create_user(email="alice@example.com", password="alice-pass")
    bob = store.create_user(email="bob@example.com", password="bob-pass")
    alice_workspace = workspace_for_user(alice, app=flask_app)
    bob_workspace = workspace_for_user(bob, app=flask_app)
    store.create_api_key(
        user_id=alice["id"],
        name="alice",
        scopes=["ingest"],
        api_key_value="alice-key",
    )
    get_job_store().create_job(
        {
            "id": "bob-job",
            "type": "file_upload",
            "status": "completed",
            "message": "",
            "processed": 1,
            "total": 1,
            "current_file": "",
            "workspace_id": bob_workspace.workspace_id,
            "errors": [],
            "result": None,
            "started_at": 0,
            "finished_at": 1,
        }
    )

    response = client.get("/api/v1/jobs/bob-job", headers={"X-API-Key": "alice-key"})

    assert response.status_code == 404
