from pathlib import Path


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
