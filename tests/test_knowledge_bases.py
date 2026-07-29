import importlib
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import create_app
from app.utils.knowledge_base_store import (
    KnowledgeBaseCatalogError,
    KnowledgeBaseStore,
    KnowledgeBaseValidationError,
    validate_knowledge_base_id,
)
from app.utils.file_index import FileIndex
from app.utils.conversation_memory import (
    get_conversation_store,
    reset_conversation_store,
)
from app.utils.user_store import UserStore
from app.utils.workspace import (
    collection_for_knowledge_base,
    knowledge_base_context,
    knowledge_base_store,
    workspace_for_user,
)


@pytest.fixture
def flask_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
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
            "API_KEY_USAGE_FILE": str(tmp_path / "api_key_usage.json"),
            "MAX_KNOWLEDGE_BASES": 2,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
        }
    )
    user = UserStore(app.config["USERS_FILE"]).create_user(
        email="admin@example.local",
        password="admin",
        display_name="Admin",
        role="admin",
    )
    app.config["TEST_USER_ID"] = user["id"]
    return app


@pytest.fixture
def client(flask_app):
    client = flask_app.test_client()
    response = client.post(
        "/admin/login",
        data={"email": "admin@example.local", "password": "admin"},
    )
    assert response.status_code == 302
    return client


def test_catalog_bootstraps_default_and_enforces_names_limit_and_corruption(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"
    store = KnowledgeBaseStore(catalog_path, max_additional=1)

    catalog = store.ensure_default()
    assert [item["id"] for item in catalog["knowledge_bases"]] == ["default"]

    created = store.create(name="Legal", description="Contratti")
    assert created["id"].startswith("kb_")
    assert store.update("default", name="Generale")["name"] == "Generale"

    with pytest.raises(KnowledgeBaseValidationError) as duplicate:
        store.update(created["id"], name="generale")
    assert duplicate.value.code == "duplicate_knowledge_base_name"

    with pytest.raises(KnowledgeBaseValidationError) as limit:
        store.create(name="Finance")
    assert limit.value.code == "knowledge_base_limit_reached"

    with pytest.raises(KnowledgeBaseValidationError) as immutable_default:
        store.remove("default")
    assert immutable_default.value.code == "default_knowledge_base"

    catalog_path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(KnowledgeBaseCatalogError):
        store.list()


def test_catalog_rejects_invalid_timestamps_and_explicit_empty_ids(tmp_path):
    catalog_path = tmp_path / "knowledge_bases.json"
    store = KnowledgeBaseStore(catalog_path)
    store.ensure_default()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["knowledge_bases"][0]["created_at"] = ""
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(KnowledgeBaseCatalogError):
        store.list()
    with pytest.raises(KnowledgeBaseValidationError) as empty_id:
        validate_knowledge_base_id("")
    assert empty_id.value.code == "invalid_knowledge_base_id"
    assert validate_knowledge_base_id(None) == "default"


def test_explicit_empty_kb_request_never_falls_back_to_default(client):
    response = client.get("/admin/files?knowledge_base_id=")

    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_knowledge_base_id"


def test_default_cors_methods_include_public_kb_patch(flask_app):
    assert "PATCH" in flask_app.config["CORS_ALLOWED_METHODS"]


def test_secondary_context_isolates_files_uploads_and_collection(flask_app):
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    created = knowledge_base_store(workspace, app=flask_app).create(name="Legal")

    default = knowledge_base_context(workspace, "default")
    secondary = knowledge_base_context(workspace, created["id"], create_dirs=True)

    assert default.file_index == str(Path(workspace.data_folder) / "files.json")
    assert default.upload_folder == workspace.workspace_upload_folder
    assert secondary.file_index == str(
        Path(workspace.data_folder)
        / "knowledge_bases"
        / created["id"]
        / "files.json"
    )
    assert secondary.upload_folder == str(
        Path(workspace.workspace_upload_folder)
        / "__knowledge_bases__"
        / created["id"]
    )
    assert secondary.chroma_collection != default.chroma_collection
    assert secondary.chroma_collection == collection_for_knowledge_base(
        workspace.workspace_id,
        created["id"],
    )
    knowledge_base_store(workspace, app=flask_app).update(
        created["id"],
        name="Renamed Legal",
    )
    assert (
        knowledge_base_context(workspace, created["id"]).chroma_collection
        == secondary.chroma_collection
    )
    assert Path(secondary.file_index).parent.is_dir()
    assert Path(secondary.upload_folder).is_dir()


def test_session_crud_keeps_default_and_returns_stats(client):
    initial = client.get("/api/knowledge-bases")
    assert initial.status_code == 200
    assert initial.get_json()["knowledge_bases"][0]["id"] == "default"

    created_response = client.post(
        "/api/knowledge-bases",
        json={"name": "Legal", "description": "Contratti"},
    )
    assert created_response.status_code == 201
    created = created_response.get_json()
    assert created["stats"] == {
        "tracked_files": 0,
        "indexed_files": 0,
        "chunks": 0,
        "data_sources": 0,
    }

    renamed = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={"name": "Legal EU"},
    )
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "Legal EU"

    delete_default = client.delete("/api/knowledge-bases/default")
    assert delete_default.status_code == 409
    assert delete_default.get_json()["status"] == "default_knowledge_base"


def test_list_skips_a_knowledge_base_deleted_after_catalog_snapshot(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Transient"},
    ).get_json()
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    store = knowledge_base_store(workspace, app=flask_app)
    target_collection = collection_for_knowledge_base(
        workspace.workspace_id,
        created["id"],
    )
    removed = False

    @contextmanager
    def concurrent_delete(*, scope=None):
        nonlocal removed
        if scope == target_collection and not removed:
            removed = store.remove(created["id"])
        yield

    monkeypatch.setattr(routes, "lifecycle_read_lock", concurrent_delete)

    response = client.get("/api/knowledge-bases")

    assert response.status_code == 200
    assert removed is True
    assert [
        record["id"]
        for record in response.get_json()["knowledge_bases"]
    ] == ["default"]


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_knowledge_base_create_rejects_non_string_name(client, value):
    response = client.post(
        "/api/knowledge-bases",
        json={"name": value},
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_knowledge_base_name"


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_knowledge_base_create_rejects_non_string_description(client, value):
    response = client.post(
        "/api/knowledge-bases",
        json={"name": "Strict", "description": value},
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_knowledge_base_description"


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("name", None, "invalid_knowledge_base_name"),
        ("name", 123, "invalid_knowledge_base_name"),
        ("name", [], "invalid_knowledge_base_name"),
        ("name", {}, "invalid_knowledge_base_name"),
        ("description", None, "invalid_knowledge_base_description"),
        ("description", 123, "invalid_knowledge_base_description"),
        ("description", [], "invalid_knowledge_base_description"),
        ("description", {}, "invalid_knowledge_base_description"),
    ],
)
def test_knowledge_base_patch_rejects_non_string_fields(
    client,
    field,
    value,
    status,
):
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Strict patch"},
    ).get_json()

    response = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={field: value},
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == status


def test_knowledge_base_patch_rejects_an_empty_object(client):
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "No empty patch"},
    ).get_json()

    response = client.patch(
        f"/api/knowledge-bases/{created['id']}",
        json={},
    )

    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_knowledge_base"


def test_api_key_allowlist_is_fail_closed_and_manage_key_authorizes_created_kb(
    client,
    flask_app,
):
    existing = client.post("/api/knowledge-bases", json={"name": "Private"}).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_id = flask_app.config["TEST_USER_ID"]
    user_store.create_api_key(
        user_id=user_id,
        name="default-reader",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="default-reader-key",
    )
    user_store.create_api_key(
        user_id=user_id,
        name="manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="manager-key",
    )

    reader_headers = {"X-API-Key": "default-reader-key"}
    listed = flask_app.test_client().get(
        "/api/v1/knowledge-bases",
        headers=reader_headers,
    )
    assert [item["id"] for item in listed.get_json()["knowledge_bases"]] == ["default"]
    hidden = flask_app.test_client().get(
        f"/api/v1/knowledge-bases/{existing['id']}",
        headers=reader_headers,
    )
    assert hidden.status_code == 404
    hidden_query = flask_app.test_client().post(
        "/api/v1/query",
        json={
            "query": "Questa richiesta non deve raggiungere il motore.",
            "knowledge_base_id": existing["id"],
        },
        headers=reader_headers,
    )
    assert hidden_query.status_code == 404

    manager_headers = {"X-API-Key": "manager-key"}
    manager_list = flask_app.test_client().get(
        "/api/v1/knowledge-bases",
        headers=manager_headers,
    )
    assert manager_list.get_json()["knowledge_bases"] == []
    created = flask_app.test_client().post(
        "/api/v1/knowledge-bases",
        json={"name": "Managed"},
        headers=manager_headers,
    )
    assert created.status_code == 201
    managed_id = created.get_json()["id"]
    stored_key = next(
        key
        for key in user_store.get_api_keys(user_id)
        if key["name"] == "manager"
    )
    assert stored_key["knowledge_base_ids"] == [managed_id]


def test_api_key_principal_wins_over_an_unrelated_browser_session(
    client,
    flask_app,
):
    user_store = UserStore(flask_app.config["USERS_FILE"])
    session_user = user_store.get(flask_app.config["TEST_USER_ID"])
    key_owner = user_store.create_user(
        email="key-owner@example.local",
        password="owner-pass",
    )
    user_store.create_api_key(
        user_id=key_owner["id"],
        name="owner-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="owner-manager-key",
    )
    session_workspace = workspace_for_user(session_user, app=flask_app)
    owner_workspace = workspace_for_user(key_owner, app=flask_app)

    response = client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Owner managed"},
        headers={"X-API-Key": "owner-manager-key"},
    )

    assert response.status_code == 201
    created_id = response.get_json()["id"]
    assert [
        item["id"]
        for item in knowledge_base_store(
            session_workspace,
            app=flask_app,
        ).list()
    ] == ["default"]
    assert [
        item["id"]
        for item in knowledge_base_store(
            owner_workspace,
            app=flask_app,
        ).list()
    ] == ["default", created_id]
    assert user_store.get_api_key(
        key_owner["id"],
        "owner-manager",
    )["knowledge_base_ids"] == [created_id]


def test_public_create_preserves_grants_added_after_authentication(
    client,
    flask_app,
    monkeypatch,
):
    existing = client.post(
        "/api/knowledge-bases",
        json={"name": "Existing target"},
    ).get_json()
    user_id = flask_app.config["TEST_USER_ID"]
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_store.create_api_key(
        user_id=user_id,
        name="concurrent-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=["default"],
        api_key_value="concurrent-manager-key",
    )
    original_create = KnowledgeBaseStore.create

    def create_after_concurrent_grant(store, **kwargs):
        created = original_create(store, **kwargs)
        user_store.add_knowledge_base_to_api_key(
            user_id=user_id,
            key_name="concurrent-manager",
            knowledge_base_id=existing["id"],
        )
        return created

    monkeypatch.setattr(
        KnowledgeBaseStore,
        "create",
        create_after_concurrent_grant,
    )

    response = flask_app.test_client().post(
        "/api/v1/knowledge-bases",
        json={"name": "New managed target"},
        headers={"X-API-Key": "concurrent-manager-key"},
    )

    assert response.status_code == 201
    assert user_store.get_api_key(
        user_id,
        "concurrent-manager",
    )["knowledge_base_ids"] == [
        "default",
        existing["id"],
        response.get_json()["id"],
    ]


def test_public_create_never_grants_a_replacement_key_with_the_same_name(
    flask_app,
    monkeypatch,
):
    user_id = flask_app.config["TEST_USER_ID"]
    user_store = UserStore(flask_app.config["USERS_FILE"])
    original_key = user_store.create_api_key(
        user_id=user_id,
        name="replaceable-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="replaceable-manager-key",
    )
    original_create = KnowledgeBaseStore.create

    def replace_authenticated_key(store, **kwargs):
        created = original_create(store, **kwargs)
        assert user_store.delete_api_key(
            user_id=user_id,
            key_name="replaceable-manager",
        )
        user_store.create_api_key(
            user_id=user_id,
            name="replaceable-manager",
            scopes=["query"],
            knowledge_base_ids=["default"],
            api_key_value="replacement-key",
        )
        return created

    monkeypatch.setattr(
        KnowledgeBaseStore,
        "create",
        replace_authenticated_key,
    )

    with pytest.raises(
        RuntimeError,
        match="API key richiedente non più disponibile",
    ):
        flask_app.test_client().post(
            "/api/v1/knowledge-bases",
            json={"name": "Must not be granted"},
            headers={"X-API-Key": "replaceable-manager-key"},
        )

    replacement = user_store.get_api_key(
        user_id,
        "replaceable-manager",
    )
    assert replacement["id"] != original_key["id"]
    assert replacement["knowledge_base_ids"] == ["default"]
    user = user_store.get(user_id)
    workspace = workspace_for_user(user, app=flask_app)
    assert [
        record["id"]
        for record in knowledge_base_store(workspace, app=flask_app).list()
    ] == ["default"]


def test_public_create_rolls_back_catalog_when_api_key_grant_fails(
    flask_app,
    monkeypatch,
):
    user_id = flask_app.config["TEST_USER_ID"]
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_store.create_api_key(
        user_id=user_id,
        name="broken-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="broken-manager-key",
    )
    runtime_user_store = importlib.import_module("utils.user_store")
    monkeypatch.setattr(
        runtime_user_store.UserStore,
        "add_knowledge_base_to_api_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("users file unavailable")
        ),
    )

    with pytest.raises(OSError, match="users file unavailable"):
        flask_app.test_client().post(
            "/api/v1/knowledge-bases",
            json={"name": "Must be rolled back"},
            headers={"X-API-Key": "broken-manager-key"},
        )

    user = user_store.get(user_id)
    workspace = workspace_for_user(user, app=flask_app)
    assert [
        record["id"]
        for record in knowledge_base_store(workspace, app=flask_app).list()
    ] == ["default"]


def test_public_create_rollback_preserves_an_inactive_tombstone(
    flask_app,
    monkeypatch,
):
    user_id = flask_app.config["TEST_USER_ID"]
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_store.create_api_key(
        user_id=user_id,
        name="tombstone-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="tombstone-manager-key",
    )
    runtime_user_store = importlib.import_module("utils.user_store")
    created_id = {"value": ""}

    def fail_after_tombstone(store, **kwargs):
        knowledge_base_id = kwargs["knowledge_base_id"]
        created_id["value"] = knowledge_base_id
        user = store.get(kwargs["user_id"])
        workspace = workspace_for_user(user, app=flask_app)
        knowledge_base_store(workspace, app=flask_app).set_status(
            knowledge_base_id,
            "delete_failed",
            delete_error="simulated concurrent delete",
        )
        raise OSError("grant failed")

    monkeypatch.setattr(
        runtime_user_store.UserStore,
        "add_knowledge_base_to_api_key",
        fail_after_tombstone,
    )

    with pytest.raises(OSError, match="grant failed"):
        flask_app.test_client().post(
            "/api/v1/knowledge-bases",
            json={"name": "Preserve tombstone"},
            headers={"X-API-Key": "tombstone-manager-key"},
        )

    user = user_store.get(user_id)
    workspace = workspace_for_user(user, app=flask_app)
    tombstone = knowledge_base_store(
        workspace,
        app=flask_app,
    ).get(created_id["value"])
    assert tombstone["status"] == "delete_failed"
    assert tombstone["delete_error"] == "simulated concurrent delete"


def test_unauthorized_public_default_delete_is_hidden_before_default_conflict(
    client,
    flask_app,
):
    secondary = client.post(
        "/api/knowledge-bases",
        json={"name": "Only target"},
    ).get_json()
    user_id = flask_app.config["TEST_USER_ID"]
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_store.create_api_key(
        user_id=user_id,
        name="secondary-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[secondary["id"]],
        api_key_value="secondary-manager-key",
    )
    user_store.create_api_key(
        user_id=user_id,
        name="default-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=["default"],
        api_key_value="default-manager-key",
    )
    api_client = flask_app.test_client()

    hidden = api_client.delete(
        "/api/v1/knowledge-bases/default",
        headers={"X-API-Key": "secondary-manager-key"},
    )
    authorized = api_client.delete(
        "/api/v1/knowledge-bases/default",
        headers={"X-API-Key": "default-manager-key"},
    )

    assert hidden.status_code == 404
    assert hidden.get_json()["status"] == "knowledge_base_not_found"
    assert authorized.status_code == 409
    assert authorized.get_json()["status"] == "default_knowledge_base"


def test_admin_can_create_a_manage_only_key_without_query_targets(
    client,
    flask_app,
):
    user_id = flask_app.config["TEST_USER_ID"]

    response = client.post(
        "/admin/api-keys",
        data={
            "action": "create",
            "user_id": user_id,
            "name": "catalog-manager",
            "scopes": "kb_manage",
            "enabled": "on",
        },
    )

    assert response.status_code == 200
    key = UserStore(flask_app.config["USERS_FILE"]).get_api_key(
        user_id,
        "catalog-manager",
    )
    assert key["scopes"] == ["kb_manage"]
    assert key["knowledge_base_ids"] == []


def test_query_selection_freezes_collection_file_index_and_memory_namespace(
    client,
    flask_app,
    monkeypatch,
):
    rag_engine = importlib.import_module("utils.rag_engine")
    captured = []

    def fake_query(_query, **kwargs):
        captured.append(kwargs)
        return {"answer": "ok", "context": []}

    monkeypatch.setattr(rag_engine, "query_rag", fake_query)
    secondary = client.post(
        "/api/knowledge-bases",
        json={"name": "Legal"},
    ).get_json()

    default_response = client.post(
        "/ask",
        json={
            "query": "Domanda sulla default",
            "conversation_id": "conv-12345678",
        },
    )
    secondary_response = client.post(
        "/ask",
        json={
            "query": "Domanda sulla legal",
            "conversation_id": "conv-12345678",
            "knowledge_base_id": secondary["id"],
        },
    )

    assert default_response.status_code == 200
    assert secondary_response.status_code == 200
    assert default_response.get_json()["knowledge_base_id"] == "default"
    assert secondary_response.get_json()["knowledge_base_id"] == secondary["id"]
    assert captured[0]["collection_name"] != captured[1]["collection_name"]
    assert captured[0]["file_index_path"] != captured[1]["file_index_path"]
    assert captured[0]["conversation_id"].endswith(":conv-12345678")
    assert captured[1]["conversation_id"].endswith(
        f":kb:{secondary['id']}:conv-12345678"
    )


def test_shared_ocr_and_transcription_ignore_the_selected_kb(
    client,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    captured = []

    monkeypatch.setattr(
        app_module,
        "_ocr_extract_upload",
        lambda _app, persist=False, config=None: captured.append(
            ("ocr", config["KNOWLEDGE_BASE_ID"])
        )
        or {"text": "ok"},
    )
    monkeypatch.setattr(
        app_module,
        "_transcribe_audio",
        lambda _app, config=None: captured.append(
            ("transcribe", config["KNOWLEDGE_BASE_ID"])
        )
        or {"transcript": "ok"},
    )

    ocr = client.post(
        "/ocr",
        data={"knowledge_base_id": "not-a-valid-kb"},
    )
    transcription = client.post(
        "/transcribe",
        data={"knowledge_base_id": "not-a-valid-kb"},
    )

    assert ocr.status_code == 200
    assert transcription.status_code == 200
    assert captured == [("ocr", "default"), ("transcribe", "default")]


def test_async_upload_worker_uses_the_index_write_lock(monkeypatch):
    app_module = importlib.import_module("app.app")
    index_lock = importlib.import_module("utils.index_lock")
    state = {"locked": False}

    @contextmanager
    def fake_lock():
        assert state["locked"] is False
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    def fake_index(_config, **_upload):
        assert state["locked"] is True
        return {"message": "done", "status": "indexed"}

    monkeypatch.setattr(index_lock, "index_write_lock", fake_lock)
    monkeypatch.setattr(
        app_module,
        "_index_saved_document_upload",
        fake_index,
    )
    store = app_module.get_job_store()
    store.create_job(
        {
            "id": "locked-upload",
            "type": "file_upload",
            "status": "running",
            "workspace_id": "workspace-a",
            "knowledge_base_id": "default",
            "errors": [],
        }
    )

    app_module._run_upload_job(
        "locked-upload",
        {"KNOWLEDGE_BASE_ID": "default"},
        "file",
        {"filename": "demo.txt"},
    )

    assert state["locked"] is False
    assert store.get("locked-upload")["status"] == "completed"


def test_delete_worker_holds_the_lifecycle_write_lock(monkeypatch):
    routes = importlib.import_module("routes.knowledge_bases")
    index_lock = importlib.import_module("utils.index_lock")
    state = {"locked": False, "called": False}

    @contextmanager
    def fake_lock(*, scope=None):
        assert state["locked"] is False
        assert scope.startswith("kb_")
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    def fake_delete(job_id, config):
        assert state["locked"] is True
        assert job_id == "delete-under-lock"
        assert config["KNOWLEDGE_BASE_ID"].startswith("kb_")
        state["called"] = True

    monkeypatch.setattr(index_lock, "lifecycle_write_lock", fake_lock)
    monkeypatch.setattr(
        routes,
        "_run_delete_knowledge_base_job",
        fake_delete,
    )

    routes._delete_knowledge_base_job(
        "delete-under-lock",
        {
            "KNOWLEDGE_BASE_ID": f"kb_{'1' * 32}",
            "CHROMA_COLLECTION": f"kb_{'2' * 40}",
        },
    )

    assert state == {"locked": False, "called": True}


def test_delete_waits_for_inflight_query_then_clears_conversation(
    client,
    flask_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    rag_engine = importlib.import_module("utils.rag_engine")
    routes = importlib.import_module("routes.knowledge_bases")
    monkeypatch.setattr(
        app_module,
        "_validate_model_selection",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(routes, "_delete_chroma_collection", lambda _name: False)
    reset_conversation_store()

    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Concurrent query"},
    ).get_json()
    query_started = threading.Event()
    release_query = threading.Event()
    query_result = {}
    scoped_conversation_id = {}

    def blocked_query(
        _query,
        *,
        conversation_id=None,
        **_kwargs,
    ):
        scoped_conversation_id["value"] = conversation_id
        query_started.set()
        assert release_query.wait(timeout=3)
        get_conversation_store().append_turn(
            conversation_id,
            user="Question",
            assistant="Answer",
        )
        return {
            "answer": "Answer",
            "context": [],
            "sources": [],
            "model": "test-model",
            "provider": "test-provider",
            "usage": None,
        }

    monkeypatch.setattr(rag_engine, "query_rag", blocked_query)

    def perform_query():
        query_client = flask_app.test_client()
        query_client.post(
            "/admin/login",
            data={"email": "admin@example.local", "password": "admin"},
        )
        query_result["response"] = query_client.post(
            "/ask",
            json={
                "query": "Question",
                "conversation_id": "conv-concurrent",
                "knowledge_base_id": created["id"],
            },
        )

    query_thread = threading.Thread(target=perform_query)
    query_thread.start()
    assert query_started.wait(timeout=3)

    deleted = client.delete(f"/api/knowledge-bases/{created['id']}")
    assert deleted.status_code == 202
    assert deleted.get_json()["status"] in {"queued", "running"}

    release_query.set()
    query_thread.join(timeout=3)
    assert not query_thread.is_alive()
    assert query_result["response"].status_code == 200

    job_id = deleted.get_json()["job_id"]
    deadline = time.time() + 3
    job = routes.get_job_store().get(job_id)
    while job and job["status"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.01)
        job = routes.get_job_store().get(job_id)

    assert job["status"] == "completed"
    assert (
        get_conversation_store().render_for_prompt(
            scoped_conversation_id["value"]
        )
        == ""
    )


def test_late_upload_worker_stops_before_writing_an_inactive_kb(
    flask_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    secondary = knowledge_base_store(workspace, app=flask_app).create(
        name="Late upload",
    )
    config = knowledge_base_context(
        workspace,
        secondary["id"],
    ).as_config()
    job_store = app_module.get_job_store()
    job_store.create_job(
        {
            "id": "late-upload",
            "type": "file_upload",
            "status": "queued",
            "workspace_id": workspace.workspace_id,
            "knowledge_base_id": secondary["id"],
            "errors": [],
        }
    )
    knowledge_base_store(workspace, app=flask_app).set_status(
        secondary["id"],
        "deleting",
    )
    writes = []
    monkeypatch.setattr(
        app_module,
        "_index_saved_document_upload",
        lambda *_args, **_kwargs: writes.append(True),
    )

    app_module._run_upload_job(
        "late-upload",
        config,
        "file",
        {"filename": "late.txt"},
    )

    assert writes == []
    assert job_store.get("late-upload")["status"] == "failed"


def test_late_rebuild_worker_stops_before_resetting_an_inactive_kb(
    flask_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    chroma_manager = importlib.import_module("utils.chroma_manager")
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    secondary = knowledge_base_store(workspace, app=flask_app).create(
        name="Late rebuild",
    )
    config = knowledge_base_context(
        workspace,
        secondary["id"],
    ).as_config()
    job_store = app_module.get_job_store()
    job_store.create_rebuild_job(
        {
            "id": "late-rebuild",
            "type": "rebuild_index",
            "status": "queued",
            "workspace_id": workspace.workspace_id,
            "knowledge_base_id": secondary["id"],
            "errors": [],
        }
    )
    knowledge_base_store(workspace, app=flask_app).set_status(
        secondary["id"],
        "delete_failed",
    )
    resets = []
    monkeypatch.setattr(
        chroma_manager,
        "reset_chroma_collection",
        lambda **_kwargs: resets.append(True),
    )

    app_module._run_rebuild_index_job(
        "late-rebuild",
        config,
        {},
        [],
    )

    assert resets == []
    assert job_store.get("late-rebuild")["status"] == "failed"


def test_code_interpreter_stream_preserves_kb_unavailable_errors(
    flask_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    knowledge_base_id = "kb_11111111111111111111111111111111"

    def fail_prepare(_payload, **_kwargs):
        raise KnowledgeBaseValidationError(
            "Knowledge base in eliminazione",
            code="knowledge_base_deleting",
            status_code=409,
        )

    monkeypatch.setattr(
        app_module,
        "_workspace_config",
        lambda *_args, **_kwargs: {
            "KNOWLEDGE_BASE_ID": knowledge_base_id,
            "CHROMA_COLLECTION": "test-collection",
        },
    )
    monkeypatch.setattr(
        app_module,
        "_raise_if_knowledge_base_became_unavailable",
        lambda _config: None,
    )
    monkeypatch.setattr(
        app_module,
        "_prepare_code_interpreter_run",
        fail_prepare,
    )
    with flask_app.app_context():
        events = list(
            app_module.run_code_interpreter_query_events(
                {"knowledge_base_id": knowledge_base_id}
            )
        )

    event = json.loads(events[0])
    assert event == {
        "type": "error",
        "error": "Knowledge base in eliminazione",
        "status": "knowledge_base_deleting",
        "knowledge_base_id": knowledge_base_id,
    }


def test_text_stream_normalizes_collection_removal_before_headers(
    client,
    flask_app,
    monkeypatch,
):
    rag_engine = importlib.import_module("utils.rag_engine")
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    store = knowledge_base_store(workspace, app=flask_app)
    secondary = store.create(name="Soon deleted")

    def fake_query(*_args, **_kwargs):
        store.set_status(secondary["id"], "deleting")

        def fail_during_retrieval():
            raise RuntimeError("collection disappeared")
            yield ""

        return fail_during_retrieval()

    monkeypatch.setattr(rag_engine, "query_rag", fake_query)

    response = client.post(
        "/ask",
        json={
            "query": "Domanda in streaming",
            "stream": True,
            "stream_format": "text",
            "knowledge_base_id": secondary["id"],
        },
    )

    assert response.status_code == 409
    assert response.get_json()["status"] == "knowledge_base_deleting"


def test_ndjson_stream_normalizes_collection_removal_in_error_event(
    client,
    flask_app,
    monkeypatch,
):
    rag_engine = importlib.import_module("utils.rag_engine")
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    store = knowledge_base_store(workspace, app=flask_app)
    secondary = store.create(name="NDJSON deleted")

    def fake_events(*_args, **_kwargs):
        store.set_status(secondary["id"], "deleting")
        return iter(
            [
                {
                    "type": "error",
                    "error": "collection disappeared",
                    "status": "server_error",
                }
            ]
        )

    monkeypatch.setattr(
        rag_engine,
        "query_rag_stream_events",
        fake_events,
    )

    response = client.post(
        "/ask",
        json={
            "query": "Domanda NDJSON",
            "stream": True,
            "stream_format": "ndjson",
            "knowledge_base_id": secondary["id"],
        },
    )

    assert response.status_code == 200
    event = json.loads(response.get_data(as_text=True).strip())
    assert event["status"] == "knowledge_base_deleting"
    assert event["knowledge_base_id"] == secondary["id"]


def test_secondary_manual_rebuild_url_keeps_the_selected_kb(
    client,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    secondary = client.post(
        "/api/knowledge-bases",
        json={"name": "Manuals"},
    ).get_json()
    monkeypatch.setattr(
        app_module,
        "_health_status",
        lambda *_args, **_kwargs: {
            "system_ready": False,
            "database_ready": True,
            "documents_count": 0,
        },
    )
    monkeypatch.setattr(
        app_module,
        "_index_rebuild_status",
        lambda *_args, **_kwargs: {
            "needs_rebuild": False,
            "indexed_count": 0,
            "tracked_count": 0,
            "stale_count": 0,
            "current_profile": {
                "embedding_provider": "local",
                "embedding_model": "test",
                "chunk_size": 1000,
                "chunk_overlap": 150,
            },
        },
    )

    response = client.get(
        f"/admin/files?knowledge_base_id={secondary['id']}"
    )

    assert response.status_code == 200
    assert (
        f'data-rebuild-url="/admin/files/rebuild?knowledge_base_id={secondary["id"]}"'
        in response.get_data(as_text=True)
    )


def test_session_job_status_is_derived_from_the_job_target(
    client,
    flask_app,
):
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    secondary = knowledge_base_store(workspace, app=flask_app).create(
        name="Reports"
    )
    app_module = importlib.import_module("app.app")
    app_module.get_job_store().create_job(
        {
            "id": "secondary-rebuild",
            "type": "rebuild_index",
            "status": "completed",
            "workspace_id": workspace.workspace_id,
            "knowledge_base_id": secondary["id"],
            "errors": [],
        }
    )

    response = client.get("/admin/files/rebuild/secondary-rebuild")

    assert response.status_code == 200
    assert response.get_json()["knowledge_base_id"] == secondary["id"]


def test_collection_cache_invalidation_does_not_clear_other_kbs(monkeypatch):
    from app.utils.cache import RAGCache

    RAGCache.reset()
    cache = RAGCache()
    monkeypatch.setattr(
        cache,
        "_get_config",
        lambda: {
            "enable_cache": True,
            "query_k": 4,
            "default_model": "test",
            "cache_ttl": 60,
        },
    )
    try:
        cache.set("documents_a\nquestion", ["a"], k=1, model="test")
        cache.set("documents_b\nquestion", ["b"], k=1, model="test")

        assert cache.clear_collection("documents_a") == 1
        assert cache.get("documents_a\nquestion", k=1, model="test") is None
        assert cache.get("documents_b\nquestion", k=1, model="test") == ["b"]
    finally:
        RAGCache.reset()


def test_catalog_json_has_no_secrets(flask_app):
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    knowledge_base_store(workspace, app=flask_app).create(
        name="Legal",
        description="No credentials",
    )

    raw = json.loads(Path(workspace.knowledge_bases_file).read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "knowledge_bases"}
    assert all(
        set(item)
        == {
            "id",
            "name",
            "description",
            "status",
            "created_at",
            "updated_at",
            "delete_error",
            "delete_requester_api_key_id",
        }
        for item in raw["knowledge_bases"]
    )


def test_legacy_environment_key_is_default_only_and_cannot_manage_kbs(
    client,
    flask_app,
    monkeypatch,
):
    secondary = client.post(
        "/api/knowledge-bases",
        json={"name": "Secondary"},
    ).get_json()
    monkeypatch.setenv("RAG_API_KEY", "legacy-environment-key")
    headers = {"X-API-Key": "legacy-environment-key"}
    api_client = flask_app.test_client()

    listed = api_client.get("/api/v1/knowledge-bases", headers=headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()["knowledge_bases"]] == [
        "default"
    ]
    forbidden = api_client.post(
        "/api/v1/knowledge-bases",
        json={"name": "Not allowed"},
        headers=headers,
    )
    assert forbidden.status_code == 403
    hidden = api_client.get(
        f"/api/v1/health?knowledge_base_id={secondary['id']}",
        headers=headers,
    )
    assert hidden.status_code == 404


def test_delete_job_cascades_secondary_files_and_disables_empty_keys(
    client,
    flask_app,
    monkeypatch,
):
    import routes.knowledge_bases as knowledge_base_routes

    monkeypatch.setattr(
        knowledge_base_routes,
        "_delete_chroma_collection",
        lambda _collection: True,
    )
    created = client.post("/api/knowledge-bases", json={"name": "Temporary"}).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user = user_store.get(flask_app.config["TEST_USER_ID"])
    workspace = workspace_for_user(user, app=flask_app)
    context = knowledge_base_context(
        workspace,
        created["id"],
        create_dirs=True,
    )
    indexed_path = Path(context.upload_folder) / "document.txt"
    indexed_path.write_text("temporary", encoding="utf-8")
    FileIndex(context.file_index).record(
        "document.txt",
        str(indexed_path),
        2,
        status="indexed",
    )
    user_store.create_api_key(
        user_id=user["id"],
        name="temporary-only",
        scopes=["query"],
        knowledge_base_ids=[created["id"]],
        api_key_value="temporary-key",
    )

    response = client.delete(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]

    deadline = time.monotonic() + 3
    job = None
    while time.monotonic() < deadline:
        polled = client.get(f"/api/knowledge-bases/jobs/{job_id}")
        assert polled.status_code == 200
        job = polled.get_json()
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert job is not None
    assert job["status"] == "completed"
    assert job["result"]["files_deleted"] == 1
    assert job["result"]["chunks_deleted"] == 2
    assert not Path(context.file_index).parent.exists()
    assert not Path(context.upload_folder).exists()
    assert knowledge_base_store(workspace, app=flask_app).get(created["id"]) is None
    key = user_store.get_api_key(user["id"], "temporary-only")
    assert key["knowledge_base_ids"] == []
    assert key["enabled"] is False


def test_double_delete_reuses_one_admitted_job(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Delete once"},
    ).get_json()
    starts = []

    class HeldThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            starts.append((self.target, self.args))

    monkeypatch.setattr(routes.threading, "Thread", HeldThread)

    first = client.delete(f"/api/knowledge-bases/{created['id']}")
    second = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert first.status_code == second.status_code == 202
    assert first.get_json()["job_id"] == second.get_json()["job_id"]
    assert len(starts) == 1
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    assert (
        knowledge_base_store(workspace, app=flask_app)
        .get(created["id"])["status"]
        == "active"
    )


def test_delete_worker_stops_live_mutations_after_lease_loss(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    locks = importlib.import_module("utils.index_lock")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Lease fenced delete"},
    ).get_json()
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    context = knowledge_base_context(
        workspace,
        created["id"],
    )
    data_folder = Path(context.file_index).parent
    upload_folder = Path(context.upload_folder)
    data_folder.mkdir(parents=True, exist_ok=True)
    upload_folder.mkdir(parents=True, exist_ok=True)
    (data_folder / "live.txt").write_text("data", encoding="utf-8")
    (upload_folder / "live.txt").write_text("upload", encoding="utf-8")
    starts = []

    class HeldThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target

        def start(self):
            starts.append(self.target)

    monkeypatch.setattr(routes.threading, "Thread", HeldThread)
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )
    response = client.delete(f"/api/knowledge-bases/{created['id']}")
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]
    assert len(starts) == 1

    lease_lost = False
    real_rmtree = routes.shutil.rmtree

    def remove_and_lose_lease(path, *args, **kwargs):
        nonlocal lease_lost
        result = real_rmtree(path, *args, **kwargs)
        if Path(path) == data_folder:
            lease_lost = True
        return result

    def reject_lost_lease():
        if lease_lost:
            raise locks.DistributedLockLeaseLostError(
                "simulated delete lease loss"
            )

    monkeypatch.setattr(routes.shutil, "rmtree", remove_and_lose_lease)
    monkeypatch.setattr(
        locks,
        "assert_distributed_locks_healthy",
        reject_lost_lease,
    )

    starts[0]()

    assert not data_folder.exists()
    assert (upload_folder / "live.txt").read_text(
        encoding="utf-8"
    ) == "upload"
    record = knowledge_base_store(workspace, app=flask_app).get(created["id"])
    assert record["status"] == "deleting"
    job = routes.get_job_store().get(job_id)
    assert job["status"] == "running"
    assert job["processed"] == 3


def test_api_key_cleanup_does_not_rollback_after_lease_loss(
    tmp_path,
):
    locks = importlib.import_module("utils.index_lock")
    store = UserStore(str(tmp_path / "users.json"))
    user = store.create_user(
        email="lease@example.local",
        password="secret",
    )
    knowledge_base_id = "kb_" + ("a" * 32)
    store.create_api_key(
        user_id=user["id"],
        name="lease-key",
        scopes=["query"],
        knowledge_base_ids=[knowledge_base_id],
        api_key_value="lease-key-value",
    )
    checks = 0
    finalized = False

    def lease_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise locks.DistributedLockLeaseLostError(
                "simulated cleanup lease loss"
            )

    def finalize():
        nonlocal finalized
        finalized = True

    with pytest.raises(
        locks.DistributedLockLeaseLostError,
        match="simulated cleanup lease loss",
    ):
        store.remove_knowledge_base_from_api_keys(
            user_id=user["id"],
            knowledge_base_id=knowledge_base_id,
            finalize=finalize,
            lease_check=lease_check,
        )

    key = store.get_api_key(user["id"], "lease-key")
    assert key["knowledge_base_ids"] == []
    assert key["enabled"] is False
    assert finalized is False


def test_inline_delete_retry_restarts_a_persisted_job_without_a_worker(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Recover inline delete"},
    ).get_json()
    starts = []

    class SimulatedProcessCrash(BaseException):
        pass

    class CrashOnceThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            starts.append(self.target)
            if len(starts) == 1:
                raise SimulatedProcessCrash()
            self.target(*self.args)

    monkeypatch.setattr(routes.threading, "Thread", CrashOnceThread)
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )

    with pytest.raises(SimulatedProcessCrash):
        client.delete(f"/api/knowledge-bases/{created['id']}")

    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    job_store = routes.get_job_store()
    job_id = job_store.knowledge_base_deletion_job_id(
        workspace.workspace_id,
        created["id"],
    )
    assert job_id
    assert (
        knowledge_base_store(workspace, app=flask_app)
        .get(created["id"])["status"]
        == "active"
    )

    retried = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert retried.status_code == 202
    assert retried.get_json()["job_id"] == job_id
    assert retried.get_json()["status"] == "completed"
    assert len(starts) == 2


def test_delete_removes_all_kb_owned_secrets_including_orphans(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    secret_store_module = importlib.import_module("utils.secret_store")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Secrets cleanup"},
    ).get_json()
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    secret_store = secret_store_module.SecretStore(
        workspace.secrets_file,
        key=workspace.secret_key,
    )
    orphan_ref = secret_store.set_secret(
        f"{workspace.workspace_id}:{created['id']}",
        "orphan:password",
        "sensitive",
    )

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

    deleted = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert deleted.status_code == 202
    assert deleted.get_json()["status"] == "completed"
    assert deleted.get_json()["result"]["secrets_deleted"] == 1
    assert secret_store.get_secret(orphan_ref) == ""


def test_delete_uses_rq_when_the_queue_backend_is_redis(
    client,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Queued delete"},
    ).get_json()
    enqueued = []

    class UnexpectedThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("inline thread must not be used for Redis")

    monkeypatch.setattr(routes, "configured_queue_backend", lambda: "redis")
    monkeypatch.setattr(routes.threading, "Thread", UnexpectedThread)
    monkeypatch.setattr(
        routes,
        "_enqueue_delete_knowledge_base_job",
        lambda job_id, config: enqueued.append((job_id, config)),
    )

    response = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert response.status_code == 202
    assert response.get_json()["status"] == "queued"
    assert enqueued[0][0] == response.get_json()["job_id"]
    assert enqueued[0][1]["KNOWLEDGE_BASE_ID"] == created["id"]
    assert enqueued[0][1]["USERS_FILE"]


def test_duplicate_redis_delete_reconciles_a_job_crashed_before_enqueue(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Recover enqueue"},
    ).get_json()
    enqueue_attempts = []

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_then_enqueue(job_id, config):
        enqueue_attempts.append((job_id, config))
        if len(enqueue_attempts) == 1:
            raise SimulatedProcessCrash()

    monkeypatch.setattr(routes, "configured_queue_backend", lambda: "redis")
    monkeypatch.setattr(
        routes,
        "_enqueue_delete_knowledge_base_job",
        crash_then_enqueue,
    )

    with pytest.raises(SimulatedProcessCrash):
        client.delete(f"/api/knowledge-bases/{created['id']}")

    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    job_store = routes.get_job_store()
    admitted_job_id = job_store.knowledge_base_deletion_job_id(
        workspace.workspace_id,
        created["id"],
    )

    assert admitted_job_id
    assert job_store.get(admitted_job_id)["status"] == "queued"
    assert (
        knowledge_base_store(workspace, app=flask_app)
        .get(created["id"])["status"]
        == "active"
    )

    retried = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert retried.status_code == 202
    assert retried.get_json()["job_id"] == admitted_job_id
    assert [attempt[0] for attempt in enqueue_attempts] == [
        admitted_job_id,
        admitted_job_id,
    ]


def test_delete_reconciles_a_crash_after_catalog_and_api_key_cleanup(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Finish cleanup"},
    ).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_id = flask_app.config["TEST_USER_ID"]
    user_store.create_api_key(
        user_id=user_id,
        name="cleanup-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[created["id"]],
        api_key_value="cleanup-manager-key",
    )
    api_client = flask_app.test_client()
    headers = {"X-API-Key": "cleanup-manager-key"}
    enqueue_attempts = []

    def delayed_then_immediate(job_id, config):
        enqueue_attempts.append((job_id, config))
        if len(enqueue_attempts) == 2:
            routes._delete_knowledge_base_job(job_id, config)

    monkeypatch.setattr(routes, "configured_queue_backend", lambda: "redis")
    monkeypatch.setattr(
        routes,
        "_enqueue_delete_knowledge_base_job",
        delayed_then_immediate,
    )

    first = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers=headers,
    )
    job_id = first.get_json()["job_id"]
    job_store = routes.get_job_store()
    original_update = job_store.update
    crashed = {"value": False}
    retry_progress = []

    class SimulatedProcessCrash(BaseException):
        pass

    def crash_once_after_api_key_cleanup(current_job_id, **patch):
        if patch.get("processed") == 7 and not crashed["value"]:
            crashed["value"] = True
            raise SimulatedProcessCrash()
        if crashed["value"] and "processed" in patch:
            retry_progress.append(patch["processed"])
        original_update(current_job_id, **patch)

    monkeypatch.setattr(
        job_store,
        "update",
        crash_once_after_api_key_cleanup,
    )

    with pytest.raises(SimulatedProcessCrash):
        routes._delete_knowledge_base_job(
            job_id,
            enqueue_attempts[0][1],
        )

    user = user_store.get(user_id)
    workspace = workspace_for_user(user, app=flask_app)
    key_after_crash = user_store.get_api_key(user_id, "cleanup-manager")
    assert knowledge_base_store(workspace, app=flask_app).get(created["id"]) is None
    assert key_after_crash["knowledge_base_ids"] == []
    assert key_after_crash["enabled"] is True
    assert job_store.get(job_id)["status"] == "running"

    retried = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers=headers,
    )

    assert retried.status_code == 202
    assert retried.get_json()["job_id"] == job_id
    assert retried.get_json()["status"] == "completed"
    assert len(enqueue_attempts) == 2
    assert retry_progress == [6, 7]


def test_public_delete_recovers_a_tombstone_after_key_cleanup_and_job_loss(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")

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
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Recover lost job"},
    ).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_id = flask_app.config["TEST_USER_ID"]
    user = user_store.get(user_id)
    workspace = workspace_for_user(user, app=flask_app)
    manager_key = user_store.create_api_key(
        user_id=user_id,
        name="lost-job-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[created["id"]],
        api_key_value="lost-job-manager-key",
    )

    catalog = knowledge_base_store(workspace, app=flask_app)
    catalog.begin_delete(
        created["id"],
        requester_api_key_id=manager_key["id"],
    )
    user_store.remove_knowledge_base_from_api_keys(
        user_id=user_id,
        knowledge_base_id=created["id"],
    )
    user_store.create_api_key(
        user_id=user_id,
        name="unrelated-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[],
        api_key_value="unrelated-manager-key",
    )

    cleaned_key = user_store.get_api_key(user_id, "lost-job-manager")
    assert cleaned_key["knowledge_base_ids"] == []
    assert cleaned_key["enabled"] is True
    assert catalog.get(created["id"])["status"] == "deleting"
    assert not routes.get_job_store().knowledge_base_deletion_job_id(
        workspace.workspace_id,
        created["id"],
    )

    api_client = flask_app.test_client()
    unrelated = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers={"X-API-Key": "unrelated-manager-key"},
    )
    assert unrelated.status_code == 404
    assert catalog.get(created["id"])["status"] == "deleting"

    recovered = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers={"X-API-Key": "lost-job-manager-key"},
    )

    assert recovered.status_code == 202
    assert recovered.get_json()["status"] == "completed"
    assert catalog.get(created["id"]) is None


def test_delete_rq_enqueue_uses_one_deterministic_unique_job(
    monkeypatch,
):
    import rq
    from rq.exceptions import DuplicateJobError
    from rq.job import Job

    routes = importlib.import_module("routes.knowledge_bases")
    accepted = []
    attempts = []
    seen_job_ids = set()

    class ActiveJob:
        def get_status(self, refresh=False):
            assert refresh is True
            return "queued"

    class UniqueQueue:
        def __init__(self, name, connection):
            assert name == "delete-tests"
            assert connection == "redis-connection"

        def enqueue(self, function, *args, **kwargs):
            attempts.append((function, args, kwargs))
            rq_job_id = kwargs["job_id"]
            if kwargs["unique"] and rq_job_id in seen_job_ids:
                raise DuplicateJobError(rq_job_id)
            seen_job_ids.add(rq_job_id)
            accepted.append((function, args, kwargs))

    monkeypatch.setattr(rq, "Queue", UniqueQueue)
    monkeypatch.setattr(routes, "queue_name", lambda: "delete-tests")
    monkeypatch.setattr(
        routes,
        "redis_connection",
        lambda: "redis-connection",
    )
    monkeypatch.setattr(
        Job,
        "fetch",
        staticmethod(
            lambda job_id, connection=None: ActiveJob()
        ),
    )
    config = {"KNOWLEDGE_BASE_ID": "kb_delete"}

    routes._enqueue_delete_knowledge_base_job("app-delete-job", config)
    routes._enqueue_delete_knowledge_base_job("app-delete-job", config)

    assert len(attempts) == 2
    assert len(accepted) == 1
    function, args, kwargs = accepted[0]
    assert function is routes._delete_knowledge_base_job
    assert args == ("app-delete-job", config)
    assert kwargs["job_id"] == "delete-kb-app-delete-job"
    assert kwargs["unique"] is True


@pytest.mark.parametrize(
    ("status", "registry_name"),
    [
        ("failed", "failed"),
        ("stopped", "failed"),
        ("canceled", "canceled"),
        ("finished", "finished"),
    ],
)
def test_terminal_delete_rq_job_is_safely_requeued(
    monkeypatch,
    status,
    registry_name,
):
    from rq.job import Job

    routes = importlib.import_module("routes.knowledge_bases")
    requeued = []

    class Registry:
        def __init__(self, name):
            self.name = name

        def requeue(self, job):
            requeued.append((self.name, job))

    class TerminalJob:
        failed_job_registry = Registry("failed")
        finished_job_registry = Registry("finished")

        def get_status(self, refresh=False):
            assert refresh is True
            return status

    terminal_job = TerminalJob()

    class Queue:
        canceled_job_registry = Registry("canceled")

    monkeypatch.setattr(
        Job,
        "fetch",
        staticmethod(
            lambda job_id, connection=None: terminal_job
        ),
    )

    routes._requeue_terminal_rq_delete_job(
        Queue(),
        "redis-connection",
        "delete-kb-app-job",
    )

    assert requeued == [(registry_name, terminal_job)]


def test_public_delete_job_remains_pollable_by_target_only_manager(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "API managed"},
    ).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_id = flask_app.config["TEST_USER_ID"]
    initiating_key = user_store.create_api_key(
        user_id=user_id,
        name="target-only-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[created["id"]],
        api_key_value="target-only-manager-key",
    )
    user_store.create_api_key(
        user_id=user_id,
        name="other-target-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[created["id"]],
        api_key_value="other-target-manager-key",
    )
    api_client = flask_app.test_client()
    headers = {"X-API-Key": "target-only-manager-key"}
    other_headers = {"X-API-Key": "other-target-manager-key"}

    deleted = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers=headers,
    )
    job_id = deleted.get_json()["job_id"]
    polled = api_client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    other_polled = api_client.get(
        f"/api/v1/jobs/{job_id}",
        headers=other_headers,
    )
    key = user_store.get_api_key(user_id, "target-only-manager")
    stored_job = routes.get_job_store().get(job_id)

    assert deleted.status_code == 202
    assert deleted.get_json()["status"] == "completed"
    assert key["knowledge_base_ids"] == []
    assert key["enabled"] is True
    assert stored_job["requester_api_key_id"] == initiating_key["id"]
    assert polled.status_code == 200
    assert polled.get_json()["status"] == "completed"
    assert other_polled.status_code == 404


def test_late_delete_failure_keeps_public_manager_authorized_for_retry(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Retry late failure"},
    ).get_json()
    user_store = UserStore(flask_app.config["USERS_FILE"])
    user_id = flask_app.config["TEST_USER_ID"]
    user_store.create_api_key(
        user_id=user_id,
        name="retry-manager",
        scopes=["kb_manage"],
        knowledge_base_ids=[created["id"]],
        api_key_value="retry-manager-key",
    )
    original_remove = KnowledgeBaseStore.remove
    attempts = {"count": 0}

    def fail_once(store, knowledge_base_id):
        if knowledge_base_id == created["id"] and attempts["count"] == 0:
            attempts["count"] += 1
            raise OSError("catalog write failed")
        return original_remove(store, knowledge_base_id)

    monkeypatch.setattr(KnowledgeBaseStore, "remove", fail_once)
    api_client = flask_app.test_client()
    headers = {"X-API-Key": "retry-manager-key"}

    failed = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers=headers,
    )
    key_after_failure = user_store.get_api_key(user_id, "retry-manager")
    retried = api_client.delete(
        f"/api/v1/knowledge-bases/{created['id']}",
        headers=headers,
    )

    assert failed.status_code == 202
    assert failed.get_json()["status"] == "failed"
    assert key_after_failure["knowledge_base_ids"] == [created["id"]]
    assert key_after_failure["enabled"] is True
    assert retried.status_code == 202
    assert retried.get_json()["status"] == "completed"
    assert retried.get_json()["job_id"] != failed.get_json()["job_id"]


def test_api_key_cleanup_failure_keeps_delete_tombstone_retryable(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    monkeypatch.setattr(
        routes,
        "_delete_chroma_collection",
        lambda _collection: False,
    )

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Retry API key cleanup"},
    ).get_json()
    runtime_user_store = importlib.import_module("utils.user_store").UserStore
    original_cleanup = runtime_user_store.remove_knowledge_base_from_api_keys
    attempts = {"count": 0}

    def fail_once(store, **kwargs):
        if attempts["count"] == 0:
            attempts["count"] += 1
            raise OSError("users store unavailable")
        return original_cleanup(store, **kwargs)

    monkeypatch.setattr(
        runtime_user_store,
        "remove_knowledge_base_from_api_keys",
        fail_once,
    )

    failed = client.delete(f"/api/knowledge-bases/{created['id']}")
    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    tombstone = knowledge_base_store(workspace, app=flask_app).get(created["id"])
    retried = client.delete(f"/api/knowledge-bases/{created['id']}")

    assert failed.status_code == 202
    assert failed.get_json()["status"] == "failed"
    assert tombstone["status"] == "delete_failed"
    assert retried.status_code == 202
    assert retried.get_json()["status"] == "completed"
    assert knowledge_base_store(workspace, app=flask_app).get(created["id"]) is None


def test_delete_job_creation_failure_leaves_an_active_kb_available(
    client,
    flask_app,
    monkeypatch,
):
    routes = importlib.import_module("routes.knowledge_bases")
    created = client.post(
        "/api/knowledge-bases",
        json={"name": "Retry me"},
    ).get_json()

    class BrokenJobStore:
        def active_jobs_count(self, *_args):
            return 0

        def create_job(self, _job):
            raise RuntimeError("job backend unavailable")

    monkeypatch.setattr(routes, "get_job_store", lambda: BrokenJobStore())

    with pytest.raises(RuntimeError, match="job backend unavailable"):
        client.delete(f"/api/knowledge-bases/{created['id']}")

    user = UserStore(flask_app.config["USERS_FILE"]).get(
        flask_app.config["TEST_USER_ID"]
    )
    workspace = workspace_for_user(user, app=flask_app)
    record = knowledge_base_store(workspace, app=flask_app).get(created["id"])
    assert record["status"] == "active"
    assert record["delete_error"] == ""
