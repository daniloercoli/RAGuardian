from __future__ import annotations

from flask import jsonify, render_template, request
from utils.agent_resolver import compute_availability, with_availability
from utils.auth import require_api_any_scope, require_api_scope, require_login
from utils.chat_agent_store import (
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
from utils.index_lock import lifecycle_read_lock
from utils.prompt_store import PromptStore
from utils.settings_store import SettingsStore
from utils.workspace import DEFAULT_KNOWLEDGE_BASE_ID, chat_agent_store, workspace_from_request


def register_agent_routes(app) -> None:
    @app.route("/agents", methods=["GET"])
    @require_login
    def agents_page():
        return render_template("agents.html")

    @app.route("/api/agents", methods=["GET"])
    @require_login
    def agents_list():
        return jsonify(_list_payload(app))

    @app.route("/api/agents", methods=["POST"])
    @require_login
    def agents_create():
        return _create_response(app)

    @app.route("/api/agents/<agent_id>", methods=["GET"])
    @require_login
    def agents_get(agent_id):
        return jsonify(_get_payload(app, agent_id))

    @app.route("/api/agents/<agent_id>", methods=["PATCH"])
    @require_login
    def agents_update(agent_id):
        return _update_response(app, agent_id)

    @app.route("/api/agents/<agent_id>", methods=["DELETE"])
    @require_login
    def agents_delete(agent_id):
        return _delete_response(app, agent_id)

    # ── v1 API ───────────────────────────────────────────────────

    @app.route("/api/v1/agents", methods=["GET"])
    @require_api_any_scope("query", "agent_manage")
    def api_agents_list():
        return jsonify(_list_payload(app, public=True))

    @app.route("/api/v1/agents/options", methods=["GET"])
    @require_api_any_scope("query", "agent_manage")
    def api_agents_options():
        return jsonify(_options_payload(app))

    @app.route("/api/v1/agents", methods=["POST"])
    @require_api_scope("agent_manage")
    def api_agents_create():
        return _create_response(app, public=True)

    @app.route("/api/v1/agents/<agent_id>", methods=["GET"])
    @require_api_any_scope("query", "agent_manage")
    def api_agents_get(agent_id):
        return jsonify(_get_payload(app, agent_id, public=True))

    @app.route("/api/v1/agents/<agent_id>", methods=["PATCH"])
    @require_api_scope("agent_manage")
    def api_agents_update(agent_id):
        return _update_response(app, agent_id, public=True)

    @app.route("/api/v1/agents/<agent_id>", methods=["DELETE"])
    @require_api_scope("agent_manage")
    def api_agents_delete(agent_id):
        return _delete_response(app, agent_id, public=True)


def _list_payload(app, *, public: bool = False) -> dict:
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = chat_agent_store(workspace, app=app)
        agents = store.list()
        if public:
            allowed = _api_key_allowed_ids()
            agents = [
                agent for agent in agents
                if set(agent.get("knowledge_base_ids") or []) <= allowed
            ]
        inputs = _availability_inputs(app, workspace)
        return {
            "agents": [
                with_availability(agent, **inputs) for agent in agents
            ],
            "limits": {
                "max_chat_agents": int(app.config.get("MAX_CHAT_AGENTS", 20)),
                "max_query_knowledge_bases": int(
                    app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)
                ),
            },
            "capabilities": {
                "can_manage": (
                    not public
                    or "agent_manage" in _api_key_scopes()
                ),
            },
        }


def _get_payload(app, agent_id: str, *, public: bool = False) -> dict:
    agent_id = validate_chat_agent_id(agent_id)
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = chat_agent_store(workspace, app=app)
        agent = store.get(agent_id)
        if agent is None:
            raise _not_found()
        if public:
            allowed = _api_key_allowed_ids()
            if not set(agent.get("knowledge_base_ids") or []) <= allowed:
                raise _not_found()
        inputs = _availability_inputs(app, workspace)
        return with_availability(agent, **inputs)


def _create_response(app, *, public: bool = False):
    data = _json_object()
    _reject_unknown_fields(
        data,
        {
            "name",
            "description",
            "provider_id",
            "model_id",
            "knowledge_base_ids",
            "prompt_ref",
        },
    )
    if not data.get("prompt_ref"):
        raise ChatAgentValidationError(
            "prompt_ref è obbligatorio",
            code="invalid_prompt_ref",
        )
    validate_chat_agent_name(data.get("name"))
    validate_chat_agent_description(data.get("description", ""))
    validate_provider_id(data.get("provider_id"))
    validate_model_id(data.get("model_id"))
    validate_knowledge_base_ids(
        data.get("knowledge_base_ids"),
        limit=int(app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)),
    )
    validate_prompt_ref_required(data.get("prompt_ref"))
    if public:
        _ensure_api_key_allowed_kb_ids(data.get("knowledge_base_ids") or [])
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = chat_agent_store(workspace, app=app)
        inputs = _availability_inputs(app, workspace)
        _reject_availability_issues(
            {
                "provider_id": data.get("provider_id"),
                "model_id": data.get("model_id"),
                "knowledge_base_ids": data.get("knowledge_base_ids"),
                "prompt_ref": data.get("prompt_ref"),
            },
            inputs,
        )
        agent = store.create(
            name=data.get("name"),
            description=data.get("description", ""),
            provider_id=data.get("provider_id"),
            model_id=data.get("model_id"),
            knowledge_base_ids=data.get("knowledge_base_ids"),
            prompt_ref=data.get("prompt_ref"),
        )
        return jsonify(with_availability(agent, **inputs)), 201


def _update_response(app, agent_id: str, *, public: bool = False):
    data = _json_object()
    _reject_unknown_fields(
        data,
        {
            "name",
            "description",
            "provider_id",
            "model_id",
            "knowledge_base_ids",
            "prompt_ref",
        },
    )
    if not data:
        raise ChatAgentValidationError("Nessun campo da aggiornare")
    if "name" in data:
        validate_chat_agent_name(data["name"])
    if "description" in data:
        validate_chat_agent_description(data["description"])
    if "provider_id" in data:
        validate_provider_id(data["provider_id"])
    if "model_id" in data:
        validate_model_id(data["model_id"])
    if "knowledge_base_ids" in data:
        validate_knowledge_base_ids(
            data["knowledge_base_ids"],
            limit=int(app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)),
        )
    if "prompt_ref" in data:
        if not data.get("prompt_ref"):
            raise ChatAgentValidationError(
                "prompt_ref è obbligatorio",
                code="invalid_prompt_ref",
            )
        validate_prompt_ref(data["prompt_ref"])
    agent_id = validate_chat_agent_id(agent_id)
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = chat_agent_store(workspace, app=app)
        existing = store.get(agent_id)
        if existing is None:
            raise _not_found()
        if public:
            allowed = _api_key_allowed_ids()
            if not set(existing.get("knowledge_base_ids") or []) <= allowed:
                raise _not_found()
        inputs = _availability_inputs(app, workspace)
        virtual_agent = dict(existing)
        if "provider_id" in data:
            virtual_agent["provider_id"] = data["provider_id"]
        if "model_id" in data:
            virtual_agent["model_id"] = data["model_id"]
        if "knowledge_base_ids" in data:
            virtual_agent["knowledge_base_ids"] = data["knowledge_base_ids"]
        if "prompt_ref" in data:
            virtual_agent["prompt_ref"] = data["prompt_ref"]
        if public:
            _ensure_api_key_allowed_kb_ids(
                virtual_agent.get("knowledge_base_ids") or []
            )
        _reject_availability_issues(virtual_agent, inputs)
        agent = store.update(
            agent_id,
            name=data.get("name") if "name" in data else None,
            description=(
                data.get("description") if "description" in data else None
            ),
            provider_id=(
                data.get("provider_id") if "provider_id" in data else None
            ),
            model_id=data.get("model_id") if "model_id" in data else None,
            knowledge_base_ids=(
                data.get("knowledge_base_ids")
                if "knowledge_base_ids" in data
                else None
            ),
            prompt_ref=(
                data.get("prompt_ref") if "prompt_ref" in data else None
            ),
        )
        return jsonify(with_availability(agent, **inputs))


def _delete_response(app, agent_id: str, *, public: bool = False):
    agent_id = validate_chat_agent_id(agent_id)
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = chat_agent_store(workspace, app=app)
        existing = store.get(agent_id)
        if existing is None:
            raise _not_found()
        if public:
            allowed = _api_key_allowed_ids()
            if not set(existing.get("knowledge_base_ids") or []) <= allowed:
                raise _not_found()
        if not store.remove(agent_id):
            raise _not_found()
        return jsonify(ok=True)


def _availability_inputs(app, workspace) -> dict:
    from utils.knowledge_base_store import KnowledgeBaseStore
    from utils.providers.registry import ProviderRegistry

    knowledge_bases = KnowledgeBaseStore(workspace.knowledge_bases_file).list()
    prompt_store = PromptStore(app.config.get("PROMPTS_DIR", "app/data"))
    shared_prompts = {
        str(prompt.get("id") or ""): prompt
        for prompt in prompt_store.all_shared()
    }

    def prompt_lookup(scope: str, prompt_id: str) -> tuple[bool, bool]:
        if scope == "personal":
            prompt = prompt_store.get_user_prompt_any(
                workspace.user_id, prompt_id
            )
            if not prompt:
                return False, False
            return True, bool(prompt.get("is_active", True))
        prompt = shared_prompts.get(prompt_id)
        if not prompt:
            return False, False
        return True, bool(prompt.get("is_active", True))

    providers = ProviderRegistry(
        SettingsStore(workspace.settings_file).load()
    ).providers()

    def is_model_available(provider_id: str, model_id: str) -> bool:
        provider = providers.get(provider_id)
        if not provider:
            return False
        return model_id in [str(item) for item in provider.get("models", [])]

    return {
        "knowledge_bases": knowledge_bases,
        "prompt_lookup": prompt_lookup,
        "is_model_available": is_model_available,
        "max_query_knowledge_bases": int(
            app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)
        ),
    }


def _json_object() -> dict:
    if not request.is_json:
        raise ChatAgentValidationError(
            "Content-Type deve essere application/json"
        )
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ChatAgentValidationError("Body JSON non valido")
    return data


def _reject_unknown_fields(data: dict, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ChatAgentValidationError(
            f"Campi non consentiti: {', '.join(unknown)}"
        )


def _not_found() -> ChatAgentValidationError:
    return ChatAgentValidationError(
        "Agent non trovato",
        code="chat_agent_not_found",
        status_code=404,
    )


def _reject_availability_issues(agent: dict, inputs: dict) -> None:
    availability = compute_availability(agent, **inputs)
    if availability["issues"]:
        issue = availability["issues"][0]
        raise ChatAgentValidationError(
            issue["message"],
            code=issue["code"],
            status_code=400,
        )


def _api_key_allowed_ids() -> set[str]:
    key = getattr(request, "api_key", None) or {}
    values = (
        key.get("knowledge_base_ids")
        if "knowledge_base_ids" in key
        else [DEFAULT_KNOWLEDGE_BASE_ID]
    )
    return set(values or [])


def _api_key_scopes() -> set[str]:
    key = getattr(request, "api_key", None) or {}
    return {str(scope) for scope in (key.get("scopes") or [])}


def _ensure_api_key_allowed_kb_ids(knowledge_base_ids: list[str]) -> None:
    allowed = _api_key_allowed_ids()
    disallowed = set(knowledge_base_ids) - allowed
    if disallowed:
        raise ChatAgentValidationError(
            "Knowledge base non consentita per questa API key",
            code="knowledge_base_not_found",
            status_code=404,
        )


def _options_payload(app) -> dict:
    from utils.knowledge_base_store import KnowledgeBaseStore
    from utils.providers.registry import ProviderRegistry

    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        settings = SettingsStore(workspace.settings_file).load()
        registry = ProviderRegistry(settings)
        default_provider, default_model, _ = registry.resolve()
        models = [
            {
                "id": model.id,
                "name": model.name,
                "provider": model.provider,
                "provider_name": model.provider_name,
                "value": f"{model.provider}:{model.id}",
                "is_default": (
                    model.provider == default_provider
                    and model.id == default_model
                ),
            }
            for model in registry.list_models()
        ]
        allowed = _api_key_allowed_ids()
        knowledge_bases = [
            kb
            for kb in KnowledgeBaseStore(workspace.knowledge_bases_file).list()
            if kb["id"] in allowed and kb.get("status") == "active"
        ]
        prompt_store = PromptStore(app.config.get("PROMPTS_DIR", "app/data"))
        personal_prompts = [
            {
                "id": prompt.get("id") or "",
                "name": prompt.get("name") or "",
                "scope": "personal",
                "is_active": True,
            }
            for prompt in prompt_store.list_user_prompts(workspace.user_id)
        ]
        shared_prompts = [
            {
                "id": prompt.get("id") or "",
                "name": prompt.get("name") or "",
                "scope": "shared",
                "is_active": bool(prompt.get("is_active", True)),
            }
            for prompt in prompt_store.all_shared()
            if prompt.get("is_active", True)
        ]
        return {
            "models": models,
            "default_provider": default_provider,
            "default_model": default_model,
            "knowledge_bases": knowledge_bases,
            "prompts": personal_prompts + shared_prompts,
            "capabilities": {
                "can_manage": "agent_manage" in _api_key_scopes(),
            },
            "limits": {
                "max_chat_agents": int(app.config.get("MAX_CHAT_AGENTS", 20)),
                "max_query_knowledge_bases": int(
                    app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)
                ),
            },
        }
