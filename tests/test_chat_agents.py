import json
import threading
from pathlib import Path

import pytest

from app import create_app
from app.utils.agent_resolver import compute_availability, with_availability
from app.utils.chat_agent_store import (
    CHAT_AGENT_SCHEMA_VERSION,
    ChatAgentCatalogError,
    ChatAgentStore,
    ChatAgentValidationError,
    validate_chat_agent_description,
    validate_chat_agent_id,
    validate_chat_agent_name,
    validate_knowledge_base_ids,
    validate_model_id,
    validate_prompt_ref,
    validate_prompt_ref_required,
    validate_provider_id,
)
from app.utils.prompt_store import PromptStore
from app.utils.user_store import UserStore

# ── Fixtures ──────────────────────────────────────────────────────

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
            "USERS_DB": str(tmp_path / "users.db"),
            "PROMPTS_DIR": str(tmp_path / "prompts"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
            "API_KEY_USAGE_FILE": str(tmp_path / "api_key_usage.json"),
            "MAX_CHAT_AGENTS": 3,
            "MAX_QUERY_KNOWLEDGE_BASES": 2,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
        }
    )
    user = UserStore(app.config["USERS_DB"]).create_user(
        email="admin@example.local",
        password="admin",
        display_name="Admin",
        role="admin",
    )
    app.config["TEST_USER_ID"] = user["id"]
    prompt = PromptStore(app.config["PROMPTS_DIR"]).create_shared(
        "Agent system prompt",
        "Rispondi come assistente.",
        user["id"],
    )
    app.config["TEST_SHARED_PROMPT_ID"] = prompt["id"]
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


@pytest.fixture
def agent_payload(flask_app):
    prompt_id = flask_app.config["TEST_SHARED_PROMPT_ID"]

    def _make(**overrides):
        payload = {
            "name": "Support Agent",
            "description": "Customer support",
            "provider_id": "regolo",
            "model_id": "gpt-oss-120b",
            "knowledge_base_ids": ["default"],
            "prompt_ref": {"id": prompt_id, "scope": "shared"},
        }
        payload.update(overrides)
        return payload

    return _make


# ── Store unit tests ──────────────────────────────────────────────

def test_store_bootstraps_empty_catalog(tmp_path):
    catalog_path = tmp_path / "chat_agents.json"
    store = ChatAgentStore(catalog_path)
    catalog = store.ensure_default()
    assert catalog["schema_version"] == CHAT_AGENT_SCHEMA_VERSION
    assert catalog["agents"] == []
    assert catalog_path.exists()


def test_store_create_and_list(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        description="Contratti",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    assert agent["id"].startswith("agent_")
    assert agent["name"] == "Legal Bot"
    assert agent["knowledge_base_ids"] == ["default"]
    assert agent["prompt_ref"] == {"id": "test-prompt", "scope": "shared"}
    assert agent["created_at"] == agent["updated_at"]

    listed = store.list()
    assert len(listed) == 1
    assert listed[0]["id"] == agent["id"]


def test_store_get(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    assert store.get(agent["id"])["id"] == agent["id"]
    assert store.get("agent_does_not_exist") is None


def test_store_update(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    updated = store.update(agent["id"], name="Legal Bot v2", description="Updated")
    assert updated["name"] == "Legal Bot v2"
    assert updated["description"] == "Updated"
    assert updated["updated_at"] >= agent["updated_at"]


def test_store_update_not_found(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    with pytest.raises(ChatAgentValidationError) as exc:
        store.update("agent_" + "0" * 32, name="Nope")
    assert exc.value.code == "chat_agent_not_found"
    assert exc.value.status_code == 404


def test_store_remove(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    assert store.remove(agent["id"]) is True
    assert store.list() == []
    assert store.remove(agent["id"]) is False


def test_store_duplicate_name_casefold(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    with pytest.raises(ChatAgentValidationError) as exc:
        store.create(
            name="legal bot",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=["default"],
            prompt_ref={"id": "test-prompt", "scope": "shared"},
        )
    assert exc.value.code == "duplicate_chat_agent_name"


def test_store_update_duplicate_name_other_agent(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent_a = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    agent_b = store.create(
        name="Other Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    with pytest.raises(ChatAgentValidationError) as exc:
        store.update(agent_b["id"], name="legal bot")
    assert exc.value.code == "duplicate_chat_agent_name"
    updated = store.update(agent_a["id"], name="LEGAL BOT")
    assert updated["name"] == "LEGAL BOT"


def test_store_limit_reached(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json", max_additional=1)
    store.ensure_default()
    store.create(
        name="First",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    with pytest.raises(ChatAgentValidationError) as exc:
        store.create(
            name="Second",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=["default"],
            prompt_ref={"id": "test-prompt", "scope": "shared"},
        )
    assert exc.value.code == "chat_agent_limit_reached"
    assert exc.value.status_code == 409


def test_store_corrupted_catalog(tmp_path):
    catalog_path = tmp_path / "chat_agents.json"
    catalog_path.write_text("{invalid", encoding="utf-8")
    store = ChatAgentStore(catalog_path)
    with pytest.raises(ChatAgentCatalogError):
        store.list()


def test_store_wrong_schema_version(tmp_path):
    catalog_path = tmp_path / "chat_agents.json"
    catalog_path.write_text(
        json.dumps({"schema_version": 99, "agents": []}), encoding="utf-8"
    )
    store = ChatAgentStore(catalog_path)
    with pytest.raises(ChatAgentCatalogError):
        store.list()


def test_store_unknown_field_rejected(tmp_path):
    catalog_path = tmp_path / "chat_agents.json"
    store = ChatAgentStore(catalog_path)
    store.ensure_default()
    store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["agents"][0]["extra_field"] = "bad"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ChatAgentCatalogError):
        store.list()


def test_store_missing_catalog_file(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    with pytest.raises(ChatAgentCatalogError):
        store.list()


def test_store_knowledge_base_ids_preserve_primary_order(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    kb_a = "kb_" + "a" * 32
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=[kb_a, "default"],
        prompt_ref={"id": "test-prompt", "scope": "shared"},
    )
    assert agent["knowledge_base_ids"] == [kb_a, "default"]


def test_store_knowledge_base_ids_rejects_duplicates(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    with pytest.raises(ChatAgentValidationError) as exc:
        store.create(
            name="Legal Bot",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=["default", "default"],
            prompt_ref={"id": "test-prompt", "scope": "shared"},
        )
    assert exc.value.code == "invalid_knowledge_base_ids"


def test_store_empty_knowledge_base_ids_rejected(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    with pytest.raises(ChatAgentValidationError):
        store.create(
            name="Legal Bot",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=[],
            prompt_ref={"id": "test-prompt", "scope": "shared"},
        )


def test_store_kb_limit_exceeded(tmp_path):
    store = ChatAgentStore(
        tmp_path / "chat_agents.json", max_query_knowledge_bases=2
    )
    store.ensure_default()
    with pytest.raises(ChatAgentValidationError) as exc:
        store.create(
            name="Legal Bot",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=["a", "b", "c"],
            prompt_ref={"id": "test-prompt", "scope": "shared"},
        )
    assert exc.value.code == "knowledge_base_limit_exceeded"


def test_store_prompt_ref_valid(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "abc123", "scope": "personal"},
    )
    assert agent["prompt_ref"] == {"id": "abc123", "scope": "personal"}


def test_store_create_accepts_missing_prompt_ref(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref=None,
    )
    assert agent["prompt_ref"] == {}


def test_store_create_accepts_empty_prompt_ref(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={},
    )
    assert agent["prompt_ref"] == {}


def test_store_update_accepts_empty_prompt_ref(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "abc123", "scope": "shared"},
    )
    updated = store.update(agent["id"], prompt_ref={})
    assert updated["prompt_ref"] == {}


def test_store_update_accepts_null_prompt_ref(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "abc123", "scope": "shared"},
    )
    updated = store.update(agent["id"], prompt_ref=None)
    assert updated["prompt_ref"] == {}


def test_store_update_prompt_ref_to_valid(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    agent = store.create(
        name="Legal Bot",
        provider_id="openai",
        model_id="gpt-4o",
        knowledge_base_ids=["default"],
        prompt_ref={"id": "abc123", "scope": "shared"},
    )
    updated = store.update(
        agent["id"],
        prompt_ref={"id": "new-prompt", "scope": "personal"},
    )
    assert updated["prompt_ref"] == {"id": "new-prompt", "scope": "personal"}


def test_store_prompt_ref_invalid_scope(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    with pytest.raises(ChatAgentValidationError):
        store.create(
            name="Legal Bot",
            provider_id="openai",
            model_id="gpt-4o",
            knowledge_base_ids=["default"],
            prompt_ref={"id": "abc123", "scope": "global"},
        )


def test_store_file_permissions(tmp_path):
    catalog_path = tmp_path / "chat_agents.json"
    store = ChatAgentStore(catalog_path)
    store.ensure_default()
    if Path(catalog_path).exists():
        mode = oct(catalog_path.stat().st_mode & 0o777)
        assert mode in ("0o600", "0o644")


def test_store_concurrent_writes(tmp_path):
    store = ChatAgentStore(tmp_path / "chat_agents.json")
    store.ensure_default()
    errors = []

    def worker(name):
        try:
            store.create(
                name=name,
                provider_id="openai",
                model_id="gpt-4o",
                knowledge_base_ids=["default"],
                prompt_ref={"id": "test-prompt", "scope": "shared"},
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"Agent-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 0
    assert len(store.list()) == 5


# ── Validation function tests ─────────────────────────────────────

def test_validate_chat_agent_id_valid():
    assert validate_chat_agent_id("agent_" + "a" * 32) == "agent_" + "a" * 32


def test_validate_chat_agent_id_invalid():
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_id("bad_id")
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_id(None)
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_id("")


def test_validate_chat_agent_name_valid():
    assert validate_chat_agent_name("My Agent") == "My Agent"


def test_validate_chat_agent_name_too_long():
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_name("x" * 121)


def test_validate_chat_agent_name_empty():
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_name("")


def test_validate_chat_agent_description_none():
    assert validate_chat_agent_description(None) == ""


def test_validate_chat_agent_description_too_long():
    with pytest.raises(ChatAgentValidationError):
        validate_chat_agent_description("x" * 501)


def test_validate_provider_id_valid():
    assert validate_provider_id("openai") == "openai"


def test_validate_provider_id_empty():
    with pytest.raises(ChatAgentValidationError):
        validate_provider_id("")


def test_validate_model_id_valid():
    assert validate_model_id("gpt-4o") == "gpt-4o"


def test_validate_model_id_empty():
    with pytest.raises(ChatAgentValidationError):
        validate_model_id("")


def test_validate_knowledge_base_ids_preserves_order():
    kb_a = "kb_" + "a" * 32
    result = validate_knowledge_base_ids([kb_a, "default"], limit=5)
    assert result == [kb_a, "default"]


def test_validate_knowledge_base_ids_rejects_duplicates():
    with pytest.raises(ChatAgentValidationError):
        validate_knowledge_base_ids(["default", "default"], limit=5)


def test_validate_knowledge_base_ids_empty():
    with pytest.raises(ChatAgentValidationError):
        validate_knowledge_base_ids([], limit=5)


def test_validate_knowledge_base_ids_limit():
    with pytest.raises(ChatAgentValidationError):
        validate_knowledge_base_ids(["a", "b", "c"], limit=2)


def test_validate_prompt_ref_none():
    assert validate_prompt_ref(None) == {}


def test_validate_prompt_ref_valid():
    result = validate_prompt_ref({"id": "abc", "scope": "shared"})
    assert result == {"id": "abc", "scope": "shared"}


def test_validate_prompt_ref_invalid_scope():
    with pytest.raises(ChatAgentValidationError):
        validate_prompt_ref({"id": "abc", "scope": "bad"})


def test_validate_prompt_ref_missing_id():
    with pytest.raises(ChatAgentValidationError):
        validate_prompt_ref({"scope": "personal"})


def test_validate_prompt_ref_required_none():
    with pytest.raises(ChatAgentValidationError) as exc:
        validate_prompt_ref_required(None)
    assert exc.value.code == "invalid_prompt_ref"


def test_validate_prompt_ref_required_empty():
    with pytest.raises(ChatAgentValidationError):
        validate_prompt_ref_required({})


def test_validate_prompt_ref_required_valid():
    result = validate_prompt_ref_required({"id": "abc", "scope": "shared"})
    assert result == {"id": "abc", "scope": "shared"}


# ── Agent resolver tests ──────────────────────────────────────────

def _make_agent(**overrides):
    agent = {
        "id": "agent_" + "1" * 32,
        "name": "Test Agent",
        "description": "",
        "provider_id": "openai",
        "model_id": "gpt-4o",
        "knowledge_base_ids": ["default"],
        "prompt_ref": {"id": "prompt123", "scope": "shared"},
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    agent.update(overrides)
    return agent


def _prompt_exists(_scope, _prompt_id):
    return True, True


def test_resolver_available_no_issues():
    agent = _make_agent()
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    assert result["available"] is True
    assert result["issues"] == []


def test_resolver_model_unavailable():
    agent = _make_agent()
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: False,
        max_query_knowledge_bases=5,
    )
    assert result["available"] is False
    assert result["issues"][0]["code"] == "model_unavailable"


def test_resolver_knowledge_base_missing():
    agent = _make_agent(knowledge_base_ids=["default", "kb_missing"])
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "knowledge_base_missing" in codes
    assert result["available"] is False


def test_resolver_knowledge_base_inactive():
    agent = _make_agent()
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "inactive"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "knowledge_base_inactive" in codes


def test_resolver_knowledge_base_limit_exceeded():
    agent = _make_agent(knowledge_base_ids=["a", "b", "c"])
    result = compute_availability(
        agent,
        knowledge_bases=[
            {"id": "a", "status": "active"},
            {"id": "b", "status": "active"},
            {"id": "c", "status": "active"},
        ],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=2,
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "knowledge_base_limit_exceeded" in codes


def test_resolver_prompt_missing():
    agent = _make_agent(prompt_ref={"id": "prompt123", "scope": "personal"})
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=lambda s, p: (False, False),
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "prompt_missing" in codes


def test_resolver_empty_prompt_ref_is_available():
    agent = _make_agent(prompt_ref={})
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    assert result["available"] is True
    assert result["issues"] == []


def test_resolver_prompt_inactive():
    agent = _make_agent(prompt_ref={"id": "prompt123", "scope": "shared"})
    result = compute_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=lambda s, p: (True, False),
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    codes = [issue["code"] for issue in result["issues"]]
    assert "prompt_inactive" in codes


def test_resolver_with_availability_copies_agent():
    agent = _make_agent()
    result = with_availability(
        agent,
        knowledge_bases=[{"id": "default", "status": "active"}],
        prompt_lookup=_prompt_exists,
        is_model_available=lambda p, m: True,
        max_query_knowledge_bases=5,
    )
    assert result["id"] == agent["id"]
    assert result["available"] is True
    assert result["issues"] == []
    assert "available" not in agent


# ── Route tests ──────────────────────────────────────────────────

def test_agents_page_requires_auth(flask_app):
    client = flask_app.test_client()
    response = client.get("/agents", follow_redirects=False)
    assert response.status_code in (302, 401, 403)


def test_agents_page_renders(client):
    response = client.get("/agents")
    assert response.status_code == 200
    assert b"Chat Agents" in response.data or b"chat agents" in response.data.lower()
    assert b'<h1 class="sr-only">Chat Agents</h1>' in response.data


def test_api_agents_list_empty(client):
    response = client.get("/api/agents")
    assert response.status_code == 200
    data = response.get_json()
    assert data["agents"] == []
    assert data["limits"]["max_chat_agents"] == 3
    assert data["limits"]["max_query_knowledge_bases"] == 2


def test_api_agents_create(client, agent_payload):
    response = client.post(
        "/api/agents",
        json=agent_payload(),
    )
    assert response.status_code == 201
    data = response.get_json()
    assert data["id"].startswith("agent_")
    assert data["name"] == "Support Agent"
    assert data["knowledge_base_ids"] == ["default"]
    assert "available" in data
    assert "issues" in data


def test_api_agents_create_without_prompt_ref(client, agent_payload):
    payload = agent_payload(name="Promptless Agent")
    payload.pop("prompt_ref")
    response = client.post("/api/agents", json=payload)
    assert response.status_code == 201
    assert response.get_json()["prompt_ref"] == {}


def test_api_agents_create_duplicate_name(client, agent_payload):
    client.post("/api/agents", json=agent_payload(name="Support Agent"))
    response = client.post(
        "/api/agents",
        json=agent_payload(name="support agent"),
    )
    assert response.status_code == 409
    data = response.get_json()
    assert data["status"] == "duplicate_chat_agent_name"


def test_api_agents_create_limit(client, agent_payload):
    for i in range(3):
        resp = client.post("/api/agents", json=agent_payload(name=f"Agent-{i}"))
        assert resp.status_code == 201
    response = client.post("/api/agents", json=agent_payload(name="Agent-4"))
    assert response.status_code == 409
    assert response.get_json()["status"] == "chat_agent_limit_reached"


def test_api_agents_create_unknown_field(client, agent_payload):
    response = client.post(
        "/api/agents",
        json={**agent_payload(), "extra": "bad"},
    )
    assert response.status_code == 400
    assert "Campi non consentiti" in response.get_json()["error"]


def test_api_agents_create_invalid_json(client):
    response = client.post(
        "/api/agents",
        data="not json",
        content_type="application/json",
    )
    assert response.status_code == 400


def test_api_agents_get(client, agent_payload):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    response = client.get(f"/api/agents/{agent_id}")
    assert response.status_code == 200
    assert response.get_json()["id"] == agent_id


def test_api_agents_get_not_found(client):
    response = client.get(f"/api/agents/agent_{'0' * 32}")
    assert response.status_code == 404
    assert response.get_json()["status"] == "chat_agent_not_found"


def test_api_agents_get_invalid_id(client):
    response = client.get("/api/agents/bad_id")
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_chat_agent_id"


def test_api_agents_update(client, agent_payload):
    payload = agent_payload()
    create_resp = client.post("/api/agents", json=payload)
    agent_id = create_resp.get_json()["id"]
    response = client.patch(
        f"/api/agents/{agent_id}",
        json={"name": "Updated Name", "description": "New desc"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "New desc"
    assert data["prompt_ref"] == payload["prompt_ref"]


def test_api_agents_update_clears_prompt_with_null(client, agent_payload):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    response = client.patch(
        f"/api/agents/{agent_id}",
        json={"prompt_ref": None},
    )
    assert response.status_code == 200
    assert response.get_json()["prompt_ref"] == {}


def test_api_agents_update_not_found(client):
    response = client.patch(
        f"/api/agents/agent_{'0' * 32}",
        json={"name": "Nope"},
    )
    assert response.status_code == 404


def test_api_agents_update_no_fields(client, agent_payload):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    response = client.patch(f"/api/agents/{agent_id}", json={})
    assert response.status_code == 400


def test_api_agents_update_unknown_field(client, agent_payload):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    response = client.patch(
        f"/api/agents/{agent_id}",
        json={"bad_field": "value"},
    )
    assert response.status_code == 400


def test_api_agents_delete(client, agent_payload):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    response = client.delete(f"/api/agents/{agent_id}")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert client.get(f"/api/agents/{agent_id}").status_code == 404


def test_api_agents_delete_not_found(client):
    response = client.delete(f"/api/agents/agent_{'0' * 32}")
    assert response.status_code == 404


def test_api_agents_list_with_availability(client, agent_payload):
    client.post("/api/agents", json=agent_payload())
    response = client.get("/api/agents")
    assert response.status_code == 200
    agents = response.get_json()["agents"]
    assert len(agents) == 1
    assert "available" in agents[0]
    assert "issues" in agents[0]


def test_api_agents_create_with_prompt_ref(client, agent_payload, flask_app):
    user = UserStore(flask_app.config["USERS_DB"]).list()[0]
    prompt = PromptStore(flask_app.config["PROMPTS_DIR"]).create_user_prompt(
        user["id"],
        "Personal agent prompt",
        "Rispondi come esperto.",
    )
    response = client.post(
        "/api/agents",
        json=agent_payload(prompt_ref={"id": prompt["id"], "scope": "personal"}),
    )
    assert response.status_code == 201
    data = response.get_json()
    assert data["prompt_ref"] == {"id": prompt["id"], "scope": "personal"}


def test_api_agents_create_invalid_prompt_ref(client, agent_payload):
    response = client.post(
        "/api/agents",
        json=agent_payload(prompt_ref={"id": "prompt123", "scope": "bad"}),
    )
    assert response.status_code == 400


def test_api_agents_create_kb_limit(client, agent_payload):
    response = client.post(
        "/api/agents",
        json=agent_payload(knowledge_base_ids=["a", "b", "c"]),
    )
    assert response.status_code == 409
    assert response.get_json()["status"] == "knowledge_base_limit_exceeded"


def test_api_agents_update_knowledge_base_ids(client, agent_payload, flask_app):
    create_resp = client.post("/api/agents", json=agent_payload())
    agent_id = create_resp.get_json()["id"]
    kb_resp = client.post(
        "/api/knowledge-bases",
        json={"name": "Second KB", "description": "Extra"},
    )
    assert kb_resp.status_code == 201
    kb_id = kb_resp.get_json()["id"]
    response = client.patch(
        f"/api/agents/{agent_id}",
        json={"knowledge_base_ids": ["default", kb_id]},
    )
    assert response.status_code == 200
    assert response.get_json()["knowledge_base_ids"] == ["default", kb_id]


def test_api_agents_requires_auth(flask_app, agent_payload):
    client = flask_app.test_client()
    assert client.get("/api/agents").status_code in (302, 401, 403)
    assert client.post("/api/agents", json=agent_payload()).status_code in (302, 401, 403)


# ── System prompt scope tests ────────────────────────────────────

def test_system_prompt_scope_invalid_value(client):
    response = client.post(
        "/ask",
        json={
            "query": "test query",
            "system_prompt_id": "abc",
            "system_prompt_scope": "global",
        },
    )
    assert response.status_code == 400
    data = response.get_json()
    assert data["field"] == "system_prompt_scope"


def test_system_prompt_scope_personal_resolves_prompt(client, flask_app, monkeypatch):
    import importlib

    app_module = importlib.import_module("app.app")
    rag_engine = importlib.import_module("utils.rag_engine")
    user = UserStore(flask_app.config["USERS_DB"]).list()[0]
    prompt = PromptStore(flask_app.config["PROMPTS_DIR"]).create_user_prompt(
        user["id"],
        "Scoped persona",
        "Rispondi come esperto.",
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

    monkeypatch.setattr(app_module, "_validate_model_selection", lambda *a, **kw: None)
    monkeypatch.setattr(rag_engine, "query_rag", fake_query_rag)

    response = client.post(
        "/ask",
        json={
            "query": "test query",
            "system_prompt_id": prompt["id"],
            "system_prompt_scope": "personal",
        },
    )

    assert response.status_code == 200
    assert captured["custom_system_prompt"] == "Rispondi come esperto."


def test_system_prompt_scope_fail_closed_on_missing_prompt(client):
    response = client.post(
        "/ask",
        json={
            "query": "test query",
            "system_prompt_id": "nonexistent",
            "system_prompt_scope": "personal",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["field"] == "system_prompt_id"


# ── v1 API route tests ───────────────────────────────────────────


def _create_api_key(flask_app, **kwargs):
    user_store = UserStore(flask_app.config["USERS_DB"])
    user_id = flask_app.config["TEST_USER_ID"]
    return user_store.create_api_key(
        user_id=user_id,
        name=kwargs.pop("name", "test-key"),
        scopes=kwargs.pop("scopes", ["query"]),
        knowledge_base_ids=kwargs.pop("knowledge_base_ids", ["default"]),
        api_key_value=kwargs.pop("api_key_value", "test-key-value"),
        **kwargs,
    )


def test_agent_manage_key_requires_and_retains_a_kb_boundary(flask_app):
    store = UserStore(flask_app.config["USERS_DB"])
    user_id = flask_app.config["TEST_USER_ID"]
    with pytest.raises(ValueError, match="agent_manage"):
        store.create_api_key(
            user_id=user_id,
            name="invalid-manager",
            scopes=["agent_manage"],
            knowledge_base_ids=[],
        )
    store.create_api_key(
        user_id=user_id,
        name="bounded-manager",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
    )
    result = store.remove_knowledge_base_from_api_keys(
        user_id=user_id,
        knowledge_base_id="default",
    )
    assert result["disabled"] == 1
    assert store.get_api_key(user_id, "bounded-manager")["enabled"] is False


def test_v1_agents_list_filters_by_kb_grants(client, flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="query-default",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="query-default-key",
    )
    headers = {"X-API-Key": "query-default-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Default Agent"),
    )
    assert created.status_code == 201
    listed = flask_app.test_client().get("/api/v1/agents", headers=headers)
    assert listed.status_code == 200
    ids = [a["id"] for a in listed.get_json()["agents"]]
    assert ids == [created.get_json()["id"]]


def test_v1_agents_list_rejects_missing_scope(flask_app):
    _create_api_key(
        flask_app,
        name="ingest-only",
        scopes=["ingest"],
        knowledge_base_ids=["default"],
        api_key_value="ingest-only-key",
    )
    response = flask_app.test_client().get(
        "/api/v1/agents",
        headers={"X-API-Key": "ingest-only-key"},
    )
    assert response.status_code == 403


def test_v1_agents_list_with_agent_manage_scope(client, flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="manager",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="manager-key",
    )
    headers = {"X-API-Key": "manager-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Managed Agent"),
    ).get_json()
    listed = flask_app.test_client().get("/api/v1/agents", headers=headers)
    assert listed.status_code == 200
    assert listed.get_json()["capabilities"]["can_manage"] is True
    ids = [a["id"] for a in listed.get_json()["agents"]]
    assert created["id"] in ids


def test_v1_agents_get_filters_by_kb_grants(client, flask_app, agent_payload):
    other_kb = "kb_" + "a" * 32
    _create_api_key(
        flask_app,
        name="restricted",
        scopes=["query"],
        knowledge_base_ids=[other_kb],
        api_key_value="restricted-key",
    )
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Private Agent"),
    ).get_json()
    resp = flask_app.test_client().get(
        f"/api/v1/agents/{created['id']}",
        headers={"X-API-Key": "restricted-key"},
    )
    assert resp.status_code == 404


def test_v1_agents_create_requires_agent_manage_scope(flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="query-only",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="query-only-key",
    )
    response = flask_app.test_client().post(
        "/api/v1/agents",
        json=agent_payload(name="Via API"),
        headers={"X-API-Key": "query-only-key"},
    )
    assert response.status_code == 403


def test_v1_agents_create_with_agent_manage_scope(flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="creator",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="creator-key",
    )
    response = flask_app.test_client().post(
        "/api/v1/agents",
        json=agent_payload(name="Via API"),
        headers={"X-API-Key": "creator-key"},
    )
    assert response.status_code == 201
    assert response.get_json()["name"] == "Via API"


def test_v1_agents_create_rejects_disallowed_kb(flask_app, agent_payload):
    other_kb = "kb_" + "b" * 32
    _create_api_key(
        flask_app,
        name="creator-default",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="creator-default-key",
    )
    response = flask_app.test_client().post(
        "/api/v1/agents",
        json=agent_payload(name="Other KB", knowledge_base_ids=[other_kb]),
        headers={"X-API-Key": "creator-default-key"},
    )
    assert response.status_code == 404


def test_v1_agents_update_with_agent_manage_scope(client, flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="updater",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="updater-key",
    )
    headers = {"X-API-Key": "updater-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="To Update"),
    ).get_json()
    response = flask_app.test_client().patch(
        f"/api/v1/agents/{created['id']}",
        json={"name": "Updated Name"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["name"] == "Updated Name"


def test_v1_agents_update_rejects_disallowed_kb(client, flask_app, agent_payload):
    other_kb = "kb_" + "c" * 32
    _create_api_key(
        flask_app,
        name="updater-default",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="updater-default-key",
    )
    headers = {"X-API-Key": "updater-default-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="To Patch"),
    ).get_json()
    response = flask_app.test_client().patch(
        f"/api/v1/agents/{created['id']}",
        json={"knowledge_base_ids": [other_kb]},
        headers=headers,
    )
    assert response.status_code == 404


def test_v1_agents_delete_with_agent_manage_scope(client, flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="deleter",
        scopes=["agent_manage"],
        knowledge_base_ids=["default"],
        api_key_value="deleter-key",
    )
    headers = {"X-API-Key": "deleter-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="To Delete"),
    ).get_json()
    response = flask_app.test_client().delete(
        f"/api/v1/agents/{created['id']}",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_v1_agents_delete_rejects_without_agent_manage(client, flask_app, agent_payload):
    _create_api_key(
        flask_app,
        name="reader",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="reader-key",
    )
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Protected"),
    ).get_json()
    response = flask_app.test_client().delete(
        f"/api/v1/agents/{created['id']}",
        headers={"X-API-Key": "reader-key"},
    )
    assert response.status_code == 403


def test_v1_agents_options_returns_filtered_data(flask_app, agent_payload):
    PromptStore(flask_app.config["PROMPTS_DIR"]).create_user_prompt(
        flask_app.config["TEST_USER_ID"],
        "Personal option",
        "Personal instructions",
    )
    _create_api_key(
        flask_app,
        name="options-key",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="options-key-value",
    )
    response = flask_app.test_client().get(
        "/api/v1/agents/options",
        headers={"X-API-Key": "options-key-value"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert "models" in data
    assert "knowledge_bases" in data
    assert "prompts" in data
    assert "limits" in data
    assert data["capabilities"]["can_manage"] is False
    kb_ids = [kb["id"] for kb in data["knowledge_bases"]]
    assert "default" in kb_ids
    prompt_ids = [p["id"] for p in data["prompts"]]
    assert flask_app.config["TEST_SHARED_PROMPT_ID"] in prompt_ids
    assert all(p["scope"] in {"personal", "shared"} for p in data["prompts"])
    assert {p["scope"] for p in data["prompts"]} == {"personal", "shared"}
    assert all("content" not in p for p in data["prompts"])


def test_v1_agents_options_rejects_missing_scope(flask_app):
    _create_api_key(
        flask_app,
        name="ingest-opts",
        scopes=["ingest"],
        knowledge_base_ids=["default"],
        api_key_value="ingest-opts-key",
    )
    response = flask_app.test_client().get(
        "/api/v1/agents/options",
        headers={"X-API-Key": "ingest-opts-key"},
    )
    assert response.status_code == 403


def test_v1_agents_unauthenticated_rejected(flask_app):
    response = flask_app.test_client().get("/api/v1/agents")
    assert response.status_code == 401


# ── agent_id in query tests ──────────────────────────────────────


def test_query_with_agent_id_resolves_config(client, flask_app, agent_payload, monkeypatch):
    import sys
    app_module = sys.modules["app.app"]
    from utils.rag_engine import query_rag as _real
    rag_engine_mod = sys.modules["utils.rag_engine"]

    created = client.post(
        "/api/agents",
        json=agent_payload(name="Query Agent"),
    ).get_json()

    captured = {}

    def fake_query_rag(*args, **kwargs):
        captured.update(kwargs)
        return {"answer": "ok", "context": [], "sources": [], "usage": None}

    monkeypatch.setattr(app_module, "_validate_model_selection", lambda *a, **kw: None)
    monkeypatch.setattr(rag_engine_mod, "query_rag", fake_query_rag)

    response = client.post(
        "/ask",
        json={"query": "test", "agent_id": created["id"]},
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["agent_id"] == created["id"]
    assert result["agent_name"] == "Query Agent"
    assert captured["provider"] == "regolo"
    assert captured["model"] == "gpt-oss-120b"


def test_query_with_agent_id_rejects_conflicting_params(client, flask_app, agent_payload):
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Conflict Agent"),
    ).get_json()
    response = client.post(
        "/ask",
        json={
            "query": "test",
            "agent_id": created["id"],
            "model": "gpt-4o",
        },
    )
    assert response.status_code == 400
    assert response.get_json()["status"] == "agent_conflicting_params"


def test_query_with_agent_id_not_found(client):
    response = client.post(
        "/ask",
        json={"query": "test", "agent_id": "agent_" + "0" * 32},
    )
    assert response.status_code == 400
    assert response.get_json()["status"] == "chat_agent_not_found"


@pytest.mark.parametrize("agent_id", [None, "", False, 0, []])
def test_query_rejects_present_but_falsey_agent_id(client, agent_id):
    response = client.post(
        "/ask",
        json={"query": "test", "agent_id": agent_id},
    )
    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_chat_agent_id"


def test_query_agent_catalog_error_does_not_leak_path(
    client, flask_app, agent_payload
):
    from app.utils.workspace import workspace_for_user

    created = client.post(
        "/api/agents",
        json=agent_payload(name="Catalog Error Agent"),
    ).get_json()
    user = UserStore(flask_app.config["USERS_DB"]).list()[0]
    workspace = workspace_for_user(user, app=flask_app)
    Path(workspace.chat_agents_file).write_text("{broken", encoding="utf-8")

    response = client.post(
        "/ask",
        json={"query": "test", "agent_id": created["id"]},
    )

    assert response.status_code == 500
    assert response.get_json()["status"] == "chat_agent_catalog_error"
    assert workspace.chat_agents_file not in response.get_json()["error"]


def test_agent_without_prompt_is_available_and_runs(
    client, flask_app, agent_payload, monkeypatch
):
    from app.utils.workspace import workspace_for_user

    created = client.post(
        "/api/agents",
        json=agent_payload(name="Promptless Agent"),
    ).get_json()
    user = UserStore(flask_app.config["USERS_DB"]).list()[0]
    workspace = workspace_for_user(user, app=flask_app)
    catalog_path = Path(workspace.chat_agents_file)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["agents"][0]["prompt_ref"] = {}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    listed = client.get("/api/agents").get_json()["agents"]
    assert listed[0]["id"] == created["id"]
    assert listed[0]["available"] is True
    assert listed[0]["issues"] == []

    import sys
    app_module = sys.modules["app.app"]
    rag_engine_mod = sys.modules["utils.rag_engine"]

    captured = {}

    def fake_query_rag(*args, **kwargs):
        captured.update(kwargs)
        return {"answer": "ok", "context": [], "sources": [], "usage": None}

    monkeypatch.setattr(app_module, "_validate_model_selection", lambda *a, **kw: None)
    monkeypatch.setattr(rag_engine_mod, "query_rag", fake_query_rag)

    response = client.post(
        "/ask",
        json={"query": "test", "agent_id": created["id"]},
    )
    assert response.status_code == 200
    assert captured.get("system_prompt_id") in (None, "", "null")


def test_v1_query_with_agent_id_resolves_config(client, flask_app, agent_payload, monkeypatch):
    import sys
    app_module = sys.modules["app.app"]
    rag_engine_mod = sys.modules["utils.rag_engine"]

    _create_api_key(
        flask_app,
        name="api-query",
        scopes=["query"],
        knowledge_base_ids=["default"],
        api_key_value="api-query-key",
    )
    headers = {"X-API-Key": "api-query-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="API Query Agent"),
    ).get_json()

    captured = {}

    def fake_query_rag(*args, **kwargs):
        captured.update(kwargs)
        return {"answer": "ok", "context": [], "sources": [], "usage": None}

    monkeypatch.setattr(app_module, "_validate_model_selection", lambda *a, **kw: None)
    monkeypatch.setattr(rag_engine_mod, "query_rag", fake_query_rag)

    response = flask_app.test_client().post(
        "/api/v1/query",
        json={"query": "test", "agent_id": created["id"]},
        headers=headers,
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["agent_id"] == created["id"]
    assert result["agent_name"] == "API Query Agent"


def test_v1_query_with_agent_id_rejects_disallowed_kb(client, flask_app, agent_payload):
    other_kb = "kb_" + "d" * 32
    _create_api_key(
        flask_app,
        name="restricted-query",
        scopes=["query"],
        knowledge_base_ids=[other_kb],
        api_key_value="restricted-query-key",
    )
    headers = {"X-API-Key": "restricted-query-key"}
    created = client.post(
        "/api/agents",
        json=agent_payload(name="Hidden Agent"),
    ).get_json()
    response = flask_app.test_client().post(
        "/api/v1/query",
        json={"query": "test", "agent_id": created["id"]},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()["status"] == "chat_agent_not_found"
