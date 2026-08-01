(function () {
    const OPEN_STATE_KEY = "ec_rag_widget_open";
    const CONVERSATION_KEY = "ec_rag_conversation_id";

    function ready(fn) {
        if (document.readyState !== "loading") fn();
        else document.addEventListener("DOMContentLoaded", fn);
    }

    function storageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (_error) {
            return null;
        }
    }

    function storageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (_error) {
            // Storage can be unavailable in restricted browser contexts.
        }
    }

    function conversationStorageKey(agentId) {
        return CONVERSATION_KEY + (agentId ? ":" + agentId : ":legacy");
    }

    function createConversationId() {
        return "wp-" + (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
    }

    function setConversationId(agentId, id) {
        storageSet(conversationStorageKey(agentId), id);
        document.cookie = "ec_rag_conversation_id=" + encodeURIComponent(id) + "; path=/; max-age=86400; samesite=lax";
        return id;
    }

    function conversationId(agentId) {
        const key = conversationStorageKey(agentId);
        const stored = storageGet(key);
        if (stored) return stored;

        const match = !agentId && document.cookie.match(/(?:^|;\s*)ec_rag_conversation_id=([^;]+)/);
        if (match && match[1]) {
            const cookieValue = decodeURIComponent(match[1]);
            storageSet(key, cookieValue);
            return cookieValue;
        }

        return setConversationId(agentId, createConversationId());
    }

    function rotateConversationId(agentId) {
        return setConversationId(agentId, createConversationId());
    }

    function flag(value, fallback) {
        if (value === "1") return true;
        if (value === "0") return false;
        return fallback;
    }

    function configFor(chat) {
        const defaults = (window.ecRagClient && ecRagClient.defaults) || {};
        let agentsCatalog = [];
        try {
            const parsed = JSON.parse(chat.dataset.ecRagAgentsCatalog || "[]");
            agentsCatalog = Array.isArray(parsed) ? parsed : [];
        } catch (_error) {
            agentsCatalog = [];
        }
        const agentMode = flag(
            chat.dataset.ecRagAgentMode,
            Boolean(defaults.enableChatAgentsMode)
        );
        const configuredAgentId = chat.dataset.ecRagAgentId || defaults.agentId || "";
        return {
            showSources: flag(chat.dataset.ecRagShowSources, Boolean(defaults.showSources)),
            enableTts: flag(chat.dataset.ecRagEnableTts, Boolean(defaults.enableTts)),
            mode: chat.dataset.ecRagMode || "inline",
            welcome: chat.dataset.ecRagWelcome || "",
            context: chat.dataset.ecRagContext || "",
            responseLanguage: chat.dataset.ecRagResponseLanguage || defaults.responseLanguage || "auto",
            pageTitle: chat.dataset.ecRagPageTitle || document.title || "",
            pageUrl: chat.dataset.ecRagPageUrl || window.location.href,
            postType: chat.dataset.ecRagPostType || "",
            locale: chat.dataset.ecRagLocale || (window.navigator && window.navigator.language) || "en",
            agentMode,
            agentId: agentsCatalog.some((agent) => agent.id === configuredAgentId)
                ? configuredAgentId
                : "",
            agentsCatalog,
        };
    }

    function appendMessage(container, role, text) {
        const item = document.createElement("div");
        item.className = "ec-rag-message ec-rag-message--" + role;
        const body = document.createElement("div");
        body.className = "ec-rag-message__body";
        body.textContent = text;
        item.appendChild(body);
        container.appendChild(item);
        container.scrollTop = container.scrollHeight;
        return item;
    }

    function renderSources(parent, sources, config) {
        if (!config.showSources || !Array.isArray(sources) || sources.length === 0) return;

        const details = document.createElement("details");
        details.className = "ec-rag-sources";
        const summary = document.createElement("summary");
        summary.textContent = "Sources (" + sources.length + ")";
        const list = document.createElement("ul");

        sources.forEach((source) => {
            const item = document.createElement("li");
            const title = document.createElement("strong");
            title.textContent = source.filename || "Document";
            item.appendChild(title);
            if (source.snippet) {
                const snippet = document.createElement("span");
                snippet.textContent = " - " + source.snippet;
                item.appendChild(snippet);
            }
            list.appendChild(item);
        });

        details.append(summary, list);
        parent.querySelector(".ec-rag-message__body").appendChild(details);
    }

    async function post(action, data) {
        const body = new URLSearchParams();
        body.set("action", action);
        body.set("nonce", ecRagClient.nonce);
        Object.keys(data || {}).forEach((key) => body.set(key, data[key] || ""));

        const response = await fetch(ecRagClient.ajaxUrl, {
            method: "POST",
            headers: {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            throw new Error((payload.data && payload.data.message) || "RAG request failed");
        }
        return payload.data;
    }

    async function postFormData(action, data) {
        const body = new FormData();
        body.set("action", action);
        body.set("nonce", ecRagClient.nonce);
        Object.keys(data || {}).forEach((key) => body.set(key, data[key]));

        const response = await fetch(ecRagClient.ajaxUrl, {
            method: "POST",
            body,
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) {
            throw new Error((payload.data && payload.data.message) || "RAG request failed");
        }
        return payload.data;
    }

    function setOpen(chat, open) {
        const panel = chat.querySelector("[data-ec-rag-panel]");
        const toggles = chat.querySelectorAll("[data-ec-rag-toggle]");
        chat.classList.toggle("is-open", open);
        if (panel) panel.setAttribute("aria-hidden", open ? "false" : "true");
        toggles.forEach((toggle) => toggle.setAttribute("aria-expanded", open ? "true" : "false"));
        if (chat.dataset.ecRagMode === "floating") {
            storageSet(OPEN_STATE_KEY, open ? "1" : "0");
        }
    }

    function appendListenButton(message, text) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "ec-rag-listen";
        button.textContent = "Listen";
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                const audio = await post("ec_rag_tts", {text});
                const player = new Audio("data:" + audio.contentType + ";base64," + audio.audio);
                await player.play();
            } catch (error) {
                button.textContent = error.message || "Audio unavailable";
            } finally {
                button.disabled = false;
            }
        });
        message.querySelector(".ec-rag-message__body").appendChild(button);
    }

    function record(transcript, role, text) {
        transcript.push({
            role,
            text,
            time: new Date().toISOString(),
        });
    }

    function downloadTranscript(config, transcript) {
        const lines = [
            config.pageTitle || "RAG conversation",
            config.pageUrl || "",
            "",
        ];
        transcript.forEach((entry) => {
            lines.push("[" + entry.time + "] " + entry.role.toUpperCase());
            lines.push(entry.text);
            lines.push("");
        });
        const blob = new Blob([lines.join("\n")], {type: "text/plain;charset=utf-8"});
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "rag-conversation-" + new Date().toISOString().slice(0, 10) + ".txt";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    }

    ready(function () {
        document.querySelectorAll("[data-ec-rag-chat]").forEach((chat) => {
            const config = configFor(chat);
            const form = chat.querySelector("[data-ec-rag-form]");
            const input = chat.querySelector("[data-ec-rag-input]");
            const messages = chat.querySelector("[data-ec-rag-messages]");
            const toggles = chat.querySelectorAll("[data-ec-rag-toggle]");
            const download = chat.querySelector("[data-ec-rag-download]");
            const audioInput = chat.querySelector("[data-ec-rag-audio]");
            const audioButton = chat.querySelector("[data-ec-rag-audio-button]");
            const agentSelect = chat.querySelector("[data-ec-rag-agent]");
            const transcript = [];
            if (!form || !input || !messages) return;

            toggles.forEach((toggle) => {
                toggle.addEventListener("click", () => setOpen(chat, !chat.classList.contains("is-open")));
            });
            if (config.mode === "floating" && storageGet(OPEN_STATE_KEY) === "1") {
                setOpen(chat, true);
            }
            if (config.welcome) {
                appendMessage(messages, "bot ec-rag-message--welcome", config.welcome);
            }
            if (agentSelect) {
                agentSelect.value = config.agentId;
                agentSelect.addEventListener("change", () => {
                    const selected = agentSelect.value;
                    config.agentId = config.agentsCatalog.some((agent) => agent.id === selected)
                        ? selected
                        : "";
                    rotateConversationId(config.agentId);
                    transcript.splice(0, transcript.length);
                    messages.replaceChildren();
                    if (config.welcome) {
                        appendMessage(messages, "bot ec-rag-message--welcome", config.welcome);
                    }
                    input.value = "";
                    input.focus();
                });
            }
            if (download) {
                download.addEventListener("click", () => downloadTranscript(config, transcript));
            }
            if (audioButton && audioInput) {
                audioButton.addEventListener("click", () => audioInput.click());
                audioInput.addEventListener("change", async () => {
                    const file = audioInput.files && audioInput.files[0];
                    if (!file) return;
                    appendMessage(messages, "user", "Audio upload: " + file.name);
                    record(transcript, "user", "Audio upload: " + file.name);
                    const pending = appendMessage(messages, "bot is-loading", "Uploading audio...");
                    audioButton.disabled = true;
                    try {
                        const result = await postFormData("ec_rag_audio_upload", {
                            audio: file,
                            conversation_id: conversationId(config.agentId),
                        });
                        pending.classList.remove("is-loading");
                        const label = result.job_id ? "Audio queued for transcription. Job: " + result.job_id : "Audio uploaded for transcription.";
                        pending.querySelector(".ec-rag-message__body").textContent = label;
                        record(transcript, "bot", label);
                    } catch (error) {
                        pending.classList.remove("is-loading");
                        pending.classList.add("ec-rag-message--error");
                        pending.querySelector(".ec-rag-message__body").textContent = error.message || "Audio upload failed";
                    } finally {
                        audioInput.value = "";
                        audioButton.disabled = false;
                    }
                });
            }

            form.addEventListener("submit", async (event) => {
                event.preventDefault();
                const query = input.value.trim();
                if (query.length < 3) return;
                if (config.agentMode && !config.agentId) {
                    appendMessage(messages, "bot ec-rag-message--error", "Choose an assistant before sending a message.");
                    if (agentSelect) agentSelect.focus();
                    return;
                }

                input.value = "";
                appendMessage(messages, "user", query);
                record(transcript, "user", query);
                const pending = appendMessage(messages, "bot is-loading", "Thinking...");
                const submit = form.querySelector("button[type='submit']");
                if (submit) submit.disabled = true;

                try {
                    const payload = {
                        query,
                        conversation_id: conversationId(config.agentId),
                        response_language: config.responseLanguage,
                        context: config.context,
                        page_title: config.pageTitle,
                        page_url: config.pageUrl,
                        post_type: config.postType,
                        locale: config.locale,
                    };
                    if (config.agentId) payload.agent_id = config.agentId;
                    const result = await post("ec_rag_query", payload);
                    pending.classList.remove("is-loading");
                    pending.querySelector(".ec-rag-message__body").textContent = result.answer || "No answer returned.";
                    renderSources(pending, result.sources || [], config);
                    record(transcript, "bot", result.answer || "No answer returned.");
                    if (config.enableTts && result.answer) {
                        appendListenButton(pending, result.answer);
                    }
                } catch (error) {
                    pending.classList.remove("is-loading");
                    pending.classList.add("ec-rag-message--error");
                    pending.querySelector(".ec-rag-message__body").textContent = error.message || "RAG request failed";
                } finally {
                    if (submit) submit.disabled = false;
                    input.focus();
                }
            });
        });
    });
})();
