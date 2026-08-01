from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_public_api_documentation_exists():
    api_doc = ROOT / "docs" / "API.md"
    openapi_doc = ROOT / "docs" / "openapi.yaml"

    assert api_doc.exists()
    assert openapi_doc.exists()


def test_public_api_documentation_lists_versioned_endpoints():
    api_doc = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    openapi_doc = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")

    for endpoint in [
        "/api/v1/health",
        "/api/v1/jobs/{job_id}",
        "/api/v1/models",
        "/api/v1/query",
        "/api/v1/ocr",
        "/api/v1/audio",
        "/api/v1/tts",
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/files",
        "/api/v1/files/{filename}",
    ]:
        assert endpoint in api_doc
        assert endpoint in openapi_doc

    assert "X-API-Key" in api_doc
    assert "client_context" in api_doc
    assert "ClientContext" in openapi_doc
    assert "DELETE /api/v1/files/{filename}" in api_doc
    assert "ApiKeyAuth" in openapi_doc


def test_openapi_multi_kb_contract_is_valid_yaml_and_tracks_runtime_limit():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(
        (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    )

    query_operation = document["paths"]["/api/v1/query"]["post"]
    query_schema = document["components"]["schemas"]["QueryRequest"]
    selector = query_schema["properties"]["knowledge_base_ids"]
    assert selector["minItems"] == 1
    assert selector["uniqueItems"] is True
    assert "maxItems" not in selector
    assert "limits.max_query_knowledge_bases" in selector["description"]
    assert {"404", "409"} <= set(query_operation["responses"])
    query_example = query_operation["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["answer"]["value"]
    assert query_example["knowledge_base_ids"] == [
        "default",
        "kb_11111111111111111111111111111111",
    ]

    clear_operation = document["paths"][
        "/api/v1/conversations/{conversation_id}"
    ]["delete"]
    assert {
        parameter.get("$ref") for parameter in clear_operation["parameters"]
    } >= {"#/components/parameters/KnowledgeBaseIds"}
    assert {"404", "409"} <= set(clear_operation["responses"])
    clear_schema = clear_operation["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "knowledge_base_ids" in clear_schema["required"]
    assert "knowledge_base_ids" in clear_schema["properties"]
    assert "knowledge_base_id" in clear_schema["properties"]
    clear_example = clear_operation["responses"]["200"]["content"][
        "application/json"
    ]["examples"]["pluralSelectionCleared"]["value"]
    assert len(clear_example["knowledge_base_ids"]) == 2

    plural_parameter = document["components"]["parameters"]["KnowledgeBaseIds"]
    assert plural_parameter["name"] == "knowledge_base_ids"
    assert plural_parameter["style"] == "form"
    assert plural_parameter["explode"] is True


def test_public_api_documentation_lists_chat_agent_endpoints():
    api_doc = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    openapi_doc = (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")

    for endpoint in [
        "/api/v1/agents",
        "/api/v1/agents/options",
        "/api/v1/agents/{agent_id}",
    ]:
        assert endpoint in api_doc
        assert endpoint in openapi_doc

    assert "agent_manage" in api_doc
    assert "ChatAgent" in openapi_doc
    assert "AgentIdPath" in openapi_doc
    assert "ChatAgentNotFound" in openapi_doc


def test_openapi_chat_agent_contract_is_valid():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(
        (ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8")
    )

    query_schema = document["components"]["schemas"]["QueryRequest"]
    assert "agent_id" in query_schema["properties"]
    query_response = document["components"]["schemas"]["QueryResponse"]
    assert "agent_id" in query_response["properties"]
    assert "agent_name" in query_response["properties"]

    agent_path = document["paths"]["/api/v1/agents"]
    assert {"get", "post"} <= set(agent_path)
    assert {"401", "403"} <= set(agent_path["get"]["responses"])
    assert {"401", "403", "404", "409"} <= set(agent_path["post"]["responses"])

    item_path = document["paths"]["/api/v1/agents/{agent_id}"]
    assert {"get", "patch", "delete"} <= set(item_path)
    assert {"404"} <= set(item_path["get"]["responses"])
    assert {"403", "404"} <= set(item_path["patch"]["responses"])
    assert {"403", "404"} <= set(item_path["delete"]["responses"])

    create_schema = document["components"]["schemas"]["ChatAgentCreateInput"]
    assert create_schema["required"] == [
        "name",
        "provider_id",
        "model_id",
        "knowledge_base_ids",
        "prompt_ref",
    ]
    assert create_schema["properties"]["description"]["maxLength"] == 500
    update_schema = document["components"]["schemas"]["ChatAgentUpdateInput"]
    assert update_schema["minProperties"] == 1
    assert update_schema["properties"]["description"]["maxLength"] == 500

    agent_schema = document["components"]["schemas"]["ChatAgent"]
    for field in ["available", "issues", "created_at", "updated_at"]:
        assert field in agent_schema["required"]
    assert "capabilities" in document["components"]["schemas"]["ChatAgentsResponse"]["required"]
    options_schema = document["components"]["schemas"]["ChatAgentOptionsResponse"]
    assert "capabilities" in options_schema["required"]
    assert options_schema["properties"]["prompts"]["items"]["properties"]["scope"]["enum"] == [
        "personal",
        "shared",
    ]
