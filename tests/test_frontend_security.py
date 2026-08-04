from pathlib import Path
import re

from app import create_app


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
    assert "clearChatButton" not in template
    assert "chatStatus" not in template
    assert "demo-readiness" not in template
    assert "data-prompt" in template
    assert "loadHealth()" not in script
    assert "body.knowledge_base_ids = [...knowledgeBaseIds]" in script
    assert 'const knowledgeBaseStorageKey = "ragKnowledgeBaseIds"' in script
    assert "kbPickerPopover" in script
    assert "appendKnowledgeBaseNotice" in script
    assert 'aria-haspopup="dialog"' in template
    assert 'id="kbChips"' in template


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


def test_multi_kb_chat_fails_closed_and_waits_for_recovery():
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")

    assert "function renderSafeMarkdown(value)" in script
    assert "if (!window.marked || !window.DOMPurify)" in script
    assert "return escapeHtml(text)" in script
    assert script.count("DOMPurify.sanitize") == 1
    assert "messageDiv.innerHTML = renderSafeMarkdown(content)" in script
    assert "messageDiv.innerHTML = renderSafeMarkdown(answerText)" in script
    assert script.count(
        "state.recoveryPromise = handleUnavailableKnowledgeBase("
    ) == 2
    assert script.count("await waitForKnowledgeBaseRecovery(state)") >= 4
    assert "clearChatButton" not in script
    assert "function clearChat(targetKnowledgeBaseIds = knowledgeBaseIds)" in script
    assert "if (busy) return;" in script
    assert "function normalizeKnowledgeBaseIds(values)" in script
    assert "focusKnowledgeBaseControlAfterRemoval(index)" in script


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


def test_chat_toolbar_includes_agent_selector():
    template = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert 'id="agentSelect"' in template
    assert '<option value="">None</option>' in template


def test_script_applies_agent_config_and_supports_override():
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")
    assert "async function loadAgents()" in script
    assert "function applyAgentConfig(agent)" in script
    assert "function setAgentActive(active)" in script
    assert "let agentActive = false" in script
    assert "function switchToCustomChat()" in script
    assert "async function newChat()" in script
    assert "async function revalidateActiveAgent()" in script
    assert "agentsCatalog = data.agents || []" in script
    assert 'agentSelect.innerHTML = \'<option value="">None</option>\'' in script
    assert "persistSelectedAgent" in script
    assert "readPendingAgent" in script
    assert "body.system_prompt_scope = systemPromptScope || undefined" in script
    assert "body.agent_id = selectedAgentId" in script
    assert "function applyQueryConfiguration(body, selectedModel)" in script
    assert "agentActive" in script


def test_script_overrides_switch_to_custom_chat_and_revalidates_on_new_chat():
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")
    # Override on model/prompt/KB switches silently to no selected Agent ("None").
    assert script.count("if (agentActive) switchToCustomChat()") == 3
    # "New chat" revalidates and reapplies the active agent instead of just clearing.
    assert 'const newChatLink = document.getElementById("newChatLink")' in script
    assert 'newChatLink.addEventListener("click", (event)' in script
    assert "requestConversationReset(newChat, newChatLink)" in script
    assert "async function newChat()" in script
    assert "await revalidateActiveAgent()" in script
    assert "function handleAgentUnavailable(agentId, agent)" in script
    # Controls are never disabled by an active agent preset anymore.
    assert "modelSelect.disabled = active" not in script
    assert "promptSelect.disabled = active" not in script
    # Page-load with a pending agent that became unavailable fails closed.
    assert "if (agent && agent.available)" in script
    assert "handleAgentUnavailable(pending, agent)" in script
    assert "let agentSelectionBlocked = false" in script
    assert "Seleziona esplicitamente un altro Agent o None" in script
    assert "Sei passato alla chat personalizzata" not in script


def test_new_chat_confirmation_modal_guards_started_conversations():
    template = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")

    assert 'id="newChatModal"' in template
    assert 'role="dialog"' in template
    assert 'aria-modal="true"' in template
    assert 'aria-labelledby="newChatModalTitle"' in template
    assert 'aria-describedby="newChatModalDescription"' in template
    assert "This conversation is not saved" in template
    assert 'id="keepConversationButton"' in template
    assert 'id="confirmNewChatButton"' in template
    assert 'chatbox.querySelector(".user-message")' in script
    assert "function requestConversationReset(action, trigger, options = {})" in script
    assert "function closeConversationResetModal()" in script
    assert "function confirmConversationReset()" in script
    assert "function handleConversationResetModalKeydown(event)" in script
    assert 'event.key === "Escape"' in script
    assert 'newChatLink.setAttribute("aria-disabled", String(isBusy))' in script
    assert "requestConversationReset(" in script
    assert "applyAgentSelection" in script


def test_configuration_link_warns_before_leaving_a_started_conversation():
    navigation = (ROOT / "app/templates/_top_nav.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/script.js").read_text(encoding="utf-8")

    assert 'id="configurationLink"' in navigation
    assert 'const configurationLink = document.getElementById("configurationLink")' in script
    assert 'configurationLink.addEventListener("click", (event)' in script
    assert 'title: "Open configuration?"' in script
    assert 'confirmLabel: "Open configuration"' in script
    assert "window.location.assign(configurationLink.href)" in script
    assert 'configurationLink.setAttribute("aria-disabled", String(isBusy))' in script


def test_agents_page_has_start_chat_navigation():
    agents_js = (ROOT / "app/static/agents.js").read_text(encoding="utf-8")
    assert 'data-action="start">Start chat' in agents_js
    assert "window.location.href = `/?agent=${encodeURIComponent(item.id)}`" in agents_js
    assert 'startButton.disabled = true' in agents_js
    assert "renderCard(item, items.length)" in agents_js
    assert "items.length || 0" not in agents_js
    assert 'name="prompt_ref" data-prompt-select' in agents_js
    assert 'name="prompt_ref" data-prompt-select required' not in agents_js


def test_agent_and_knowledge_base_cancel_restore_original_values():
    agents_js = (ROOT / "app/static/agents.js").read_text(encoding="utf-8")
    knowledge_bases_js = (
        ROOT / "app/static/knowledge_bases.js"
    ).read_text(encoding="utf-8")
    stylesheet = (ROOT / "app/static/style.css").read_text(encoding="utf-8")

    assert "function resetCreateForm()" in agents_js
    assert "function resetEditor()" in agents_js
    assert "resetEditor();\n            editor.hidden = true;" in agents_js
    assert "editor.reset();\n            editor.hidden = true;" in knowledge_bases_js
    assert ".form-grid[hidden]" in stylesheet


def test_default_knowledge_base_uses_a_dedicated_tag_style():
    knowledge_bases_js = (
        ROOT / "app/static/knowledge_bases.js"
    ).read_text(encoding="utf-8")
    stylesheet = (ROOT / "app/static/style.css").read_text(encoding="utf-8")

    assert 'class="default-kb-tag"' in knowledge_bases_js
    assert ".default-kb-tag {" in stylesheet
    assert ".default-kb-tag::before" in stylesheet


def test_agents_description_maxlength_matches_limit():
    template = (ROOT / "app/templates/agents.html").read_text(encoding="utf-8")
    agents_js = (ROOT / "app/static/agents.js").read_text(encoding="utf-8")
    assert 'maxlength="500"' in template
    assert 'maxlength="500"' in agents_js


def test_session_posts_require_csrf_token_and_login_redirect_stays_local(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    app = create_app(
        {
            "TESTING": True,
            "CSRF_ENABLED": True,
            "SECRET_KEY": "test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_DB": str(tmp_path / "users.db"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace-uploads"),
        }
    )
    client = app.test_client()

    assert client.post("/admin/login", data={"password": "admin"}).status_code == 403

    login_page = client.get("/admin/login")
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', login_page.data).group(1).decode()
    response = client.post(
        "/admin/login?next=https://attacker.example/",
        data={"password": "admin", "csrf_token": token},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin/config")
