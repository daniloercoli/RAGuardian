from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chat_markdown_is_sanitized_before_dom_insert():
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")
    template = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")

    assert "purify.min.js" in template.lower()
    assert "DOMPurify.sanitize" in script
    assert "messageDiv.innerHTML = marked.parse" not in script
    assert 'stream_format: "ndjson"' in script
    assert "conversation_id: conversationId" in script
    assert "sessionStorage.setItem" in script
    assert "response.body.getReader" in script
    assert 'document.createElement("details")' in script
    assert 'document.createElement("summary")' in script
    assert "renderSourceCard" in script
    assert "sourceSnippet" in script
    assert "<textarea" in template
    assert "clearChatButton" in template
    assert "chatStatus" not in template
    assert "demo-readiness" not in template
    assert "data-prompt" in template
    assert "loadHealth()" not in script


def test_chat_ask_button_recovers_from_stalled_streams():
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")

    assert "createAskTimeout" in script
    assert "controller.abort()" in script
    assert "askTimeout.clear()" in script
    assert "postAsk(body, askTimeout)" in script
    assert "renderStreamingResponse(response, messageDiv, askTimeout)" in script
    assert "renderCodeInterpreterStream(response, messageDiv, askTimeout)" in script
    assert script.count("reader.cancel().catch") >= 2
    assert "formatConnectionError" in script


def test_templates_include_browser_icons():
    templates = [
        ROOT / "app/templates/index.html",
        ROOT / "app/templates/admin_config.html",
        ROOT / "app/templates/admin_files.html",
        ROOT / "app/templates/admin_login.html",
        ROOT / "app/templates/configuration_error.html",
    ]

    assert (ROOT / "app/static/favicon.ico").exists()
    assert (ROOT / "app/static/favicon.png").exists()
    assert (ROOT / "app/static/apple-touch-icon.png").exists()
    for template_path in templates:
        template = template_path.read_text(encoding="utf-8")
        assert "favicon.ico" in template
        assert "favicon.png" in template
        assert "apple-touch-icon.png" in template
