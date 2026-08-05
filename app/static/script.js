document.addEventListener("DOMContentLoaded", () => {
    if (window.marked) {
        marked.use({
            breaks: true,
            gfm: true,
            headerIds: false,
            mangle: false
        });
    }

    const chatbox = document.getElementById("chatbox");
    const userInput = document.getElementById("userInput");
    const sendButton = document.getElementById("sendButton");
    const modelSelect = document.getElementById("modelSelect");
    const kbPicker = document.getElementById("kbPicker");
    const kbPickerButton = document.getElementById("kbPickerButton");
    const kbPickerSummary = document.getElementById("kbPickerSummary");
    const kbPickerPopover = document.getElementById("kbPickerPopover");
    const kbPickerSearch = document.getElementById("kbPickerSearch");
    const kbPickerOptions = document.getElementById("kbPickerOptions");
    const kbPickerError = document.getElementById("kbPickerError");
    const kbPickerLimit = document.getElementById("kbPickerLimit");
    const kbPickerApply = document.getElementById("kbPickerApply");
    const kbPickerCancel = document.getElementById("kbPickerCancel");
    const kbChips = document.getElementById("kbChips");
    const promptSelect = document.getElementById("promptSelect");
    const agentSelect = document.getElementById("agentSelect");
    const newChatLink = document.getElementById("newChatLink");
    const configurationLink = document.getElementById("configurationLink");
    const newChatModal = document.getElementById("newChatModal");
    const newChatModalTitle = document.getElementById("newChatModalTitle");
    const newChatModalDescription = document.getElementById("newChatModalDescription");
    const keepConversationButton = document.getElementById("keepConversationButton");
    const confirmNewChatButton = document.getElementById("confirmNewChatButton");
    const streamStatus = document.getElementById("streamStatus");
    const emptyState = document.getElementById("emptyState");
    const promptButtons = document.querySelectorAll("[data-prompt]");
    const uploadAudioButton = document.getElementById("uploadAudioButton");
    const uploadOcrButton = document.getElementById("uploadOcrButton");
    const ocrFileInput = document.getElementById("ocrFileInput");
    const uploadFileButton = document.getElementById("uploadFileButton");
    const codeInterpreterToggle = document.getElementById("codeInterpreterToggle");
    const attachedFilesDiv = document.getElementById("attachedFiles");

    if (!chatbox || !userInput || !sendButton || !modelSelect) {
        return;
    }

    const promptStorageKey = "ragSystemPromptId";
    const promptRefStorageKey = "ragSystemPromptRef";
    const agentStorageKey = "ragSelectedAgent";
    const knowledgeBaseStorageKey = "ragKnowledgeBaseIds";
    const legacyKnowledgeBaseStorageKey = "ragKnowledgeBaseId";
    let knowledgeBaseIds = loadKnowledgeBaseIds();
    let knowledgeBaseCatalog = [];
    let draftKnowledgeBaseIds = [...knowledgeBaseIds];
    let maxQueryKnowledgeBases = 5;
    const initialPromptRef = loadPromptRef();
    let systemPromptId = initialPromptRef.id;
    let systemPromptScope = initialPromptRef.scope;
    let codeInterpreterEnabled = false;
    const uploadedFiles = [];
    const askIdleTimeoutMs = 120000;

    let busy = false;
    let agentActive = false;
    let agentSelectionBlocked = false;
    let agentsCatalog = [];
    let selectedAgentId = "";
    let pendingConversationReset = null;
    let conversationResetTrigger = null;
    const conversationStorageKey = "ragConversationId";
    let conversationId = loadOrCreateConversationId();
    const lastTurnStorageKey = "ragLastTurnId";
    let lastTurnId = loadLastTurnId();
    let currentTurnId = null;

    function loadLastTurnId() {
        try {
            return (window.sessionStorage && sessionStorage.getItem(lastTurnStorageKey)) || null;
        } catch (error) {
            return null;
        }
    }

    function persistLastTurnId(value) {
        try {
            if (window.sessionStorage) {
                if (value) {
                    sessionStorage.setItem(lastTurnStorageKey, value);
                } else {
                    sessionStorage.removeItem(lastTurnStorageKey);
                }
            }
        } catch (error) {
            // Best effort only.
        }
    }

    function resetLastTurnId() {
        lastTurnId = null;
        currentTurnId = null;
        persistLastTurnId(null);
    }

    function setControlLabel(button, label) {
        if (!button) return;
        button.title = label;
        button.setAttribute("aria-label", label);
    }

    sendButton.addEventListener("click", sendMessage);
    userInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    userInput.addEventListener("input", resizeInput);
    modelSelect.addEventListener("change", () => {
        if (agentActive) switchToCustomChat();
        updateChatStatus();
    });
    if (kbPickerButton) kbPickerButton.addEventListener("click", toggleKnowledgeBasePicker);
    if (kbPickerApply) kbPickerApply.addEventListener("click", applyKnowledgeBaseDraft);
    if (kbPickerCancel) kbPickerCancel.addEventListener("click", closeKnowledgeBasePicker);
    if (kbPickerSearch) kbPickerSearch.addEventListener("input", renderKnowledgeBaseOptions);
    document.addEventListener("pointerdown", (event) => {
        if (
            kbPickerPopover
            && !kbPickerPopover.hidden
            && kbPicker
            && !kbPicker.contains(event.target)
        ) {
            closeKnowledgeBasePicker();
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && kbPickerPopover && !kbPickerPopover.hidden) {
            event.preventDefault();
            closeKnowledgeBasePicker();
        }
    });
    if (promptSelect) {
        promptSelect.addEventListener("change", () => {
            const ref = parsePromptValue(promptSelect.value);
            systemPromptId = ref.id;
            systemPromptScope = ref.scope;
            persistPromptRef(systemPromptScope, systemPromptId);
            if (agentActive) switchToCustomChat();
        });
    }
    if (agentSelect) {
        agentSelect.addEventListener("change", () => {
            const id = agentSelect.value;
            const previousKnowledgeBaseIds = [...knowledgeBaseIds];
            const agent = id ? agentsCatalog.find(item => item.id === id) : null;
            if (id && agent && agent.available) {
                const previousAgentId = selectedAgentId;
                agentSelect.value = previousAgentId;
                requestConversationReset(
                    () => applyAgentSelection(id, agent, previousKnowledgeBaseIds),
                    agentSelect
                );
                return;
            }
            applyAgentSelection(id, agent, previousKnowledgeBaseIds);
        });
    }
    if (newChatLink) {
        newChatLink.addEventListener("click", (event) => {
            event.preventDefault();
            requestConversationReset(newChat, newChatLink);
        });
    }
    if (configurationLink) {
        configurationLink.addEventListener("click", (event) => {
            event.preventDefault();
            requestConversationReset(
                () => window.location.assign(configurationLink.href),
                configurationLink,
                {
                    title: "Open configuration?",
                    description: "This conversation is not saved. If you open Configuration, you won’t be able to return to it.",
                    confirmLabel: "Open configuration"
                }
            );
        });
    }
    if (keepConversationButton) {
        keepConversationButton.addEventListener("click", closeConversationResetModal);
    }
    if (confirmNewChatButton) {
        confirmNewChatButton.addEventListener("click", confirmConversationReset);
    }
    if (newChatModal) {
        newChatModal.addEventListener("click", (event) => {
            if (event.target === newChatModal) closeConversationResetModal();
        });
        newChatModal.addEventListener("keydown", handleConversationResetModalKeydown);
    }
    promptButtons.forEach((button) => {
        button.addEventListener("click", () => {
            userInput.value = button.dataset.prompt || "";
            resizeInput();
            sendMessage();
        });
    });

    // Audio recording
    if (uploadAudioButton) {
        let mediaRecorder = null;
        let audioChunks = [];
        let isRecording = false;

        uploadAudioButton.addEventListener("click", async () => {
            if (!isRecording) {
                let stream = null;
                try {
                    audioChunks = [];
                    if (!navigator.mediaDevices || !window.MediaRecorder) {
                        throw new Error("Audio recording is not supported by this browser");
                    }

                    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    const mimeType = getSupportedRecordingMimeType();
                    const recorder = mimeType
                        ? new MediaRecorder(stream, { mimeType })
                        : new MediaRecorder(stream);
                    mediaRecorder = recorder;
                    recorder.ondataavailable = (e) => {
                        if (e.data.size > 0) {
                            audioChunks.push(e.data);
                        }
                    };
                    recorder.onstop = async () => {
                        stream.getTracks().forEach(track => track.stop());
                        isRecording = false;
                        uploadAudioButton.classList.remove("recording");
                        setControlLabel(uploadAudioButton, "Record audio for transcription");
                        await submitRecording(audioChunks, recorder.mimeType);
                    };
                    recorder.onerror = (event) => {
                        displayRecordingError(event.error || event);
                    };
                    recorder.start();
                    isRecording = true;
                    uploadAudioButton.classList.add("recording");
                    setControlLabel(uploadAudioButton, "Stop recording");
                } catch (err) {
                    if (stream) {
                        stream.getTracks().forEach(track => track.stop());
                    }
                    isRecording = false;
                    uploadAudioButton.classList.remove("recording");
                    setControlLabel(uploadAudioButton, "Record audio for transcription");
                    displayRecordingError(err);
                }
            } else if (mediaRecorder && mediaRecorder.state !== "inactive") {
                mediaRecorder.stop();
            }
        });
    }

    if (uploadOcrButton && ocrFileInput) {
        uploadOcrButton.addEventListener("click", () => ocrFileInput.click());
        ocrFileInput.addEventListener("change", handleOcrUpload);
    }

    // File upload for code interpreter
    if (uploadFileButton) {
        uploadFileButton.addEventListener("click", () => {
            const fileInput = document.createElement("input");
            fileInput.type = "file";
            fileInput.multiple = true;
            fileInput.accept = ".csv,.xlsx,.xls,.json,.parquet,.tsv,.zip,.png,.jpg,.jpeg,.gif,.pdf,.txt,.md";
            fileInput.addEventListener("change", handleFileUpload);
            fileInput.click();
        });
    }

    // Code interpreter toggle
    if (codeInterpreterToggle) {
        codeInterpreterToggle.addEventListener("change", () => {
            codeInterpreterEnabled = codeInterpreterToggle.checked;
            if (codeInterpreterEnabled) {
                uploadFileButton.hidden = false;
            }
        });
    }

    async function submitRecording(chunks, mimeType) {
        if (chunks.length === 0) {
            uploadAudioButton.disabled = false;
            setControlLabel(uploadAudioButton, "Record audio for transcription");
            return;
        }

        const recordingType = mimeType || chunks[0].type || "audio/webm";
        const blob = new Blob(chunks, { type: recordingType });
        const filename = `recording.${recordingExtension(recordingType)}`;
        const originalBtnText = uploadAudioButton.title;
        setControlLabel(uploadAudioButton, "Transcribing...");
        uploadAudioButton.disabled = true;

        hideEmptyState();
        const msgDiv = appendBotMessage("Transcribing audio...");

        try {
            const formData = new FormData();
            formData.append("file", blob, filename);

            const response = await fetch("/transcribe", {
                method: "POST",
                body: formData
            });

            if (!response.ok) {
                const data = await readErrorPayload(response);
                msgDiv.innerHTML = renderSafeMarkdown(
                    formatError(data, "Transcription failed")
                );
            } else {
                const data = await response.json();
                const transcript = data.transcript || "";
                if (transcript) {
                    msgDiv.innerHTML = renderSafeMarkdown(
                        "**Transcription**:\n\n" + escapeHtml(transcript)
                    );
                    userInput.value = transcript;
                    resizeInput();
                } else {
                    msgDiv.innerHTML = "**Transcription result:** empty (recording may contain no speech)";
                }
            }
            highlightCodeBlocks(msgDiv);
        } catch (error) {
            msgDiv.innerHTML = renderSafeMarkdown(
                "Transcription failed: " + error.message
            );
        } finally {
            uploadAudioButton.disabled = false;
            setControlLabel(uploadAudioButton, originalBtnText || "Record audio for transcription");
            chatbox.scrollTop = chatbox.scrollHeight;
        }
    }

    function displayRecordingError(err) {
        appendMessage("**Microphone not available.** " + formatError({ error: err.message || "Permission denied" }), "bot-message");
    }

    function getSupportedRecordingMimeType() {
        if (typeof MediaRecorder.isTypeSupported !== "function") {
            return "";
        }
        const candidates = [
            "audio/webm;codecs=opus",
            "audio/webm",
            "audio/ogg;codecs=opus",
            "audio/ogg"
        ];
        return candidates.find(type => MediaRecorder.isTypeSupported(type)) || "";
    }

    function recordingExtension(mimeType) {
        const type = String(mimeType || "").toLowerCase();
        if (type.includes("ogg")) return "ogg";
        if (type.includes("mp4") || type.includes("aac")) return "m4a";
        if (type.includes("wav")) return "wav";
        if (type.includes("flac")) return "flac";
        if (type.includes("mpeg") || type.includes("mp3")) return "mp3";
        return "webm";
    }

    async function loadModels() {
        try {
            const response = await fetch("/models");
            const data = await response.json();
            modelSelect.innerHTML = "";
            if (!response.ok || !data.models || data.models.length === 0) {
                modelSelect.appendChild(new Option("No models available", ""));
                modelSelect.disabled = true;
                updateChatStatus();
                return;
            }
            for (const model of data.models) {
                const option = new Option(model.name, model.value || model.id);
                option.dataset.provider = model.provider;
                option.dataset.model = model.id;
                if (model.is_default || option.value === data.default_value) {
                    option.selected = true;
                    option.dataset.default = "true";
                }
                modelSelect.appendChild(option);
            }
            modelSelect.disabled = false;
            updateChatStatus();
        } catch (error) {
            modelSelect.innerHTML = "";
            modelSelect.appendChild(new Option("Error loading models", ""));
            modelSelect.disabled = true;
            updateChatStatus();
        }
    }

    async function sendMessage() {
        const query = userInput.value.trim();
        if (!query) return;
        if (agentSelectionBlocked) {
            appendMessage(
                "**Select an available Agent or None before sending.**",
                "bot-message"
            );
            return;
        }
        if (!modelSelect.value) {
            appendMessage("**Error:** no model available.", "bot-message");
            return;
        }

        appendMessage(query, "user-message");
        userInput.value = "";
        resizeInput();
        setBusy(true);
        let askTimeout = null;

        try {
            const selected = modelSelect.selectedOptions[0];
            askTimeout = createAskTimeout();

            // Code interpreter mode
            if (codeInterpreterEnabled && uploadedFiles.length > 0) {
                const ciBody = applyQueryConfiguration({
                    query,
                    conversation_id: conversationId,
                    stream: true,
                    stream_format: "ndjson",
                    use_code_interpreter: true,
                    attached_files: uploadedFiles.map(f => ({
                        id: f.id,
                        file_id: f.id,
                        name: f.name,
                        type: f.type
                    }))
                }, selected);
                const response = await postAsk(ciBody, askTimeout);
                if (!response.ok) {
                    const data = await readErrorPayload(response);
                    reconcileTurnIdFromError(data);
                    const messageDiv = appendBotMessage(
                        formatError(data, response.statusText)
                    );
                    if (agentActive && selectedAgentId) {
                        await revalidateActiveAgent();
                    }
                    await handleUnavailableKnowledgeBase(data, messageDiv);
                } else {
                    const messageDiv = appendBotMessage("Preparing analysis...");
                    await renderCodeInterpreterStream(response, messageDiv, askTimeout);
                }
                // Clear uploaded files after sending
                uploadedFiles.length = 0;
                renderAttachedFiles();
                return;
            }

            const body = applyQueryConfiguration({
                query,
                conversation_id: conversationId,
                stream: true,
                stream_format: "ndjson",
                use_code_interpreter: false
            }, selected);
            if (uploadedFiles.length > 0) {
                body.attached_files = uploadedFiles.map(f => ({
                    id: f.id,
                    file_id: f.id,
                    name: f.name,
                    type: f.type
                }));
            }

            const response = await postAsk(body, askTimeout);

            if (!response.ok) {
                const data = await readErrorPayload(response);
                reconcileTurnIdFromError(data);
                const messageDiv = appendBotMessage(
                    formatError(data, response.statusText)
                );
                if (agentActive && selectedAgentId) {
                    await revalidateActiveAgent();
                }
                await handleUnavailableKnowledgeBase(data, messageDiv);
            } else {
                const messageDiv = appendBotMessage("");
                await renderStreamingResponse(response, messageDiv, askTimeout);
                if (uploadedFiles.length > 0) {
                    uploadedFiles.length = 0;
                    renderAttachedFiles();
                }
            }
        } catch (error) {
            appendMessage(formatConnectionError(error), "bot-message");
        } finally {
            if (askTimeout) askTimeout.clear();
            setBusy(false);
            userInput.focus();
        }
    }

    function applyQueryConfiguration(body, selectedModel) {
        body.persist_history = true;
        body.parent_turn_id = lastTurnId || undefined;
        body.turn_id = (window.crypto && typeof window.crypto.randomUUID === "function")
            ? window.crypto.randomUUID()
            : `turn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
        currentTurnId = body.turn_id;
        if (agentActive && selectedAgentId) {
            body.agent_id = selectedAgentId;
            return body;
        }
        body.model = selectedModel ? selectedModel.dataset.model : undefined;
        body.provider = selectedModel ? selectedModel.dataset.provider : undefined;
        body.system_prompt_id = systemPromptId || undefined;
        body.system_prompt_scope = systemPromptScope || undefined;
        body.knowledge_base_ids = [...knowledgeBaseIds];
        return body;
    }

    function renderCodeInterpreterResult(data) {
        const messageDiv = appendBotMessage("");
        const result = data.result || {};
        renderCodeInterpreterPayload(messageDiv, data.code || "", result);
        if (data.context) {
            appendSources(data.context);
        }
    }

    async function renderCodeInterpreterStream(response, messageDiv, timeout) {
        const state = {
            code: "",
            result: null,
            hasError: false,
            status: "Preparing analysis...",
            recoveryPromise: null
        };

        const onEvent = (event) => {
            if (!event || state.hasError) return;
            if (event.type === "meta") {
                updateConversationId(event.conversation_id);
                state.status = "Generating Python...";
                renderCodeInterpreterPayload(messageDiv, state.code, state.result, state.status);
            } else if (event.type === "code") {
                state.code = event.code || "";
                state.status = "Executing Python...";
                renderCodeInterpreterPayload(messageDiv, state.code, state.result, state.status);
            } else if (event.type === "execution") {
                state.result = event.result || {};
                state.status = "";
                renderCodeInterpreterPayload(messageDiv, state.code, state.result, state.status);
            } else if (event.type === "done") {
                updateConversationId(event.conversation_id);
                state.code = event.code || state.code;
                state.result = event.result || state.result || {};
                state.status = "";
                renderCodeInterpreterPayload(messageDiv, state.code, state.result, state.status);
                appendSources(event.context);
                if (currentTurnId) {
                    lastTurnId = currentTurnId;
                    persistLastTurnId(lastTurnId);
                }
            } else if (event.type === "error") {
                state.hasError = true;
                renderBotAnswer(messageDiv, formatError(event, "Code interpreter interrupted"));
                state.recoveryPromise = handleUnavailableKnowledgeBase(
                    event,
                    messageDiv
                );
            }
        };

        if (!response.body || !response.body.getReader) {
            const text = await response.text();
            parseNdjsonLines(text, onEvent);
            await waitForKnowledgeBaseRecovery(state);
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const {value, done} = await reader.read();
            if (done) break;
            if (timeout) timeout.reset();
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            parseNdjsonLines(lines.join("\n"), onEvent);
            if (state.hasError) {
                reader.cancel().catch(() => {});
                await waitForKnowledgeBaseRecovery(state);
                return;
            }
        }
        buffer += decoder.decode();
        parseNdjsonLines(buffer, onEvent);
        await waitForKnowledgeBaseRecovery(state);
    }

    function renderCodeInterpreterPayload(messageDiv, code, result, statusText) {
        result = result || {};

        let content = "";
        if (statusText) {
            content += `_${statusText}_\n\n`;
        }
        if (code) {
            content += "**Code:**\n\n```python\n" + escapeHtml(code) + "\n```\n\n";
        }
        if (result && Object.prototype.hasOwnProperty.call(result, "success")) {
            if (result.success) {
                if (result.text) {
                    content += "**Output:**\n\n" + result.text;
                }
                if (result.images && result.images.length > 0) {
                    content += "\n\n";
                    result.images.forEach(img => {
                        content += `![plot](${img})\n\n`;
                    });
                }
            } else {
                content += "**Execution error:**\n\n" + (result.error || "Unknown error");
            }
        }
        if (!content) {
            content = "_Preparing analysis..._";
        }

        messageDiv.innerHTML = renderSafeMarkdown(content);
        highlightCodeBlocks(messageDiv);
    }

    function setBusy(isBusy) {
        busy = isBusy;
        sendButton.disabled = isBusy || agentSelectionBlocked || !modelSelect.value;
        userInput.disabled = isBusy;
        if (newChatLink) {
            newChatLink.setAttribute("aria-disabled", String(isBusy));
        }
        if (configurationLink) {
            configurationLink.setAttribute("aria-disabled", String(isBusy));
        }
        if (kbPickerButton) kbPickerButton.disabled = isBusy || knowledgeBaseCatalog.length === 0;
        if (kbPickerApply) kbPickerApply.disabled = isBusy;
        if (isBusy) closeKnowledgeBasePicker();
        renderKnowledgeBaseSelection();
        sendButton.textContent = isBusy ? "Waiting" : "Send";
        if (streamStatus) {
            streamStatus.hidden = !isBusy;
        }
    }

    function createAskTimeout() {
        if (!window.AbortController) {
            return {signal: undefined, reset() {}, clear() {}};
        }

        const controller = new AbortController();
        let timeoutId = null;
        const reset = () => {
            clearTimeout(timeoutId);
            timeoutId = setTimeout(() => controller.abort(), askIdleTimeoutMs);
        };
        reset();
        return {
            signal: controller.signal,
            reset,
            clear() {
                clearTimeout(timeoutId);
            }
        };
    }

    async function postAsk(body, timeout) {
        const response = await fetch("/ask", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
            signal: timeout.signal
        });
        timeout.reset();
        return response;
    }

    async function readErrorPayload(response) {
        try {
            return await response.json();
        } catch (error) {
            return {error: response.statusText};
        }
    }

    function reconcileTurnIdFromError(data) {
        if (!data || typeof data !== "object") return;
        const expected = data.expected_parent_turn_id;
        if (typeof expected === "string" && expected) {
            lastTurnId = expected;
            persistLastTurnId(lastTurnId);
        } else if (data.code === "turn_id_conflict" || data.code === "continuity_error") {
            resetLastTurnId();
        }
    }

    async function renderStreamingResponse(response, messageDiv, timeout) {
        const state = {
            answerText: "",
            hasError: false,
            recoveryPromise: null
        };

        if (!response.body || !response.body.getReader) {
            const text = await response.text();
            parseNdjsonLines(text, (event) => handleStreamEvent(event, state, messageDiv));
            await waitForKnowledgeBaseRecovery(state);
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const {value, done} = await reader.read();
            if (done) break;
            if (timeout) timeout.reset();

            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            parseNdjsonLines(lines.join("\n"), (event) => handleStreamEvent(event, state, messageDiv));
            if (state.hasError) {
                reader.cancel().catch(() => {});
                await waitForKnowledgeBaseRecovery(state);
                return;
            }
        }

        buffer += decoder.decode();
        parseNdjsonLines(buffer, (event) => handleStreamEvent(event, state, messageDiv));
        await waitForKnowledgeBaseRecovery(state);
    }

    function parseNdjsonLines(text, onEvent) {
        text.split("\n").forEach((line) => {
            const trimmed = line.trim();
            if (!trimmed) return;

            try {
                onEvent(JSON.parse(trimmed));
            } catch (error) {
                onEvent({type: "token", text: line});
            }
        });
    }

    function handleStreamEvent(event, state, messageDiv) {
        if (!event || state.hasError) return;

        if (event.type === "token") {
            state.answerText += event.text || "";
            renderBotAnswer(messageDiv, state.answerText);
        } else if (event.type === "meta") {
            updateConversationId(event.conversation_id);
        } else if (event.type === "done") {
            updateConversationId(event.conversation_id);
            state.answerText = event.answer || state.answerText;
            renderBotAnswer(messageDiv, state.answerText);
            appendSources(event.context);
            if (currentTurnId) {
                lastTurnId = currentTurnId;
                persistLastTurnId(lastTurnId);
            }
        } else if (event.type === "error") {
            state.hasError = true;
            renderBotAnswer(messageDiv, formatError(event, "Streaming interrupted"));
            state.recoveryPromise = handleUnavailableKnowledgeBase(
                event,
                messageDiv
            );
        }
    }

    async function waitForKnowledgeBaseRecovery(state) {
        if (!state || !state.recoveryPromise) return;
        try {
            await state.recoveryPromise;
        } catch (error) {
            console.warn("Unable to refresh knowledge bases after stream failure", error);
        }
    }

    function appendMessage(message, className) {
        hideEmptyState();
        const messageDiv = document.createElement("div");
        messageDiv.classList.add("message", className);

        if (className === "bot-message") {
            let responseJson = null;
            try {
                responseJson = JSON.parse(message);
            } catch (e) {
                responseJson = null;
            }

            let answerText = message;
            let contextData = null;

            if (responseJson && responseJson.answer) {
                answerText = responseJson.answer;
                contextData = responseJson.context;
            }

            renderBotAnswer(messageDiv, answerText);
            chatbox.appendChild(messageDiv);
            appendSources(contextData);
        } else {
            messageDiv.textContent = message;
            chatbox.appendChild(messageDiv);
        }

        chatbox.scrollTop = chatbox.scrollHeight;
    }

    function appendBotMessage(answerText) {
        hideEmptyState();
        const messageDiv = document.createElement("div");
        messageDiv.classList.add("message", "bot-message");
        renderBotAnswer(messageDiv, answerText);
        chatbox.appendChild(messageDiv);
        chatbox.scrollTop = chatbox.scrollHeight;
        return messageDiv;
    }

    function renderBotAnswer(messageDiv, answerText) {
        messageDiv.innerHTML = renderSafeMarkdown(answerText);
        highlightCodeBlocks(messageDiv);
        chatbox.scrollTop = chatbox.scrollHeight;
    }

    function appendSources(contextData) {
        if (!contextData || !Array.isArray(contextData) || contextData.length === 0) {
            return;
        }

        const seenUrls = new Set();
        const uniqueSources = contextData.filter(ctx => {
            if (!ctx.download_url) return false;
            const metadata = ctx.metadata || {};
            const sourceKey = `${metadata.knowledge_base_id || ""}:${ctx.download_url}`;
            if (seenUrls.has(sourceKey)) return false;
            seenUrls.add(sourceKey);
            return true;
        });

        if (uniqueSources.length === 0) {
            return;
        }

        const contextDiv = document.createElement("details");
        contextDiv.classList.add("context-sources");
        const title = document.createElement("summary");
        title.textContent = `Sources (${uniqueSources.length})`;
        const list = document.createElement("div");
        list.className = "source-card-list";

        uniqueSources.forEach(ctx => {
            list.appendChild(renderSourceCard(ctx));
        });

        contextDiv.appendChild(title);
        contextDiv.appendChild(list);
        chatbox.appendChild(contextDiv);
        chatbox.scrollTop = chatbox.scrollHeight;
    }

    function renderSourceCard(ctx) {
        const card = document.createElement("article");
        card.className = "source-card";

        const header = document.createElement("div");
        header.className = "source-card-header";

        const link = document.createElement("a");
        link.href = ctx.download_url;
        link.textContent = sourceFilename(ctx);
        link.target = "_blank";

        const meta = document.createElement("span");
        meta.textContent = sourceMeta(ctx);

        const identity = document.createElement("div");
        identity.className = "source-card-identity";
        const knowledgeBaseName = ctx.metadata && ctx.metadata.knowledge_base_name;
        if (knowledgeBaseName) {
            const badge = document.createElement("span");
            badge.className = "source-kb-badge";
            badge.textContent = knowledgeBaseName;
            const origins = ctx.metadata.knowledge_base_origins || [];
            if (origins.length > 1) {
                badge.title = `Also found in ${origins.length - 1} other knowledge base(s)`;
            }
            identity.appendChild(badge);
        }
        identity.appendChild(link);
        header.append(identity, meta);

        const snippet = document.createElement("p");
        snippet.textContent = sourceSnippet(ctx.text || "");

        card.append(header, snippet);
        return card;
    }

    function sourceFilename(ctx) {
        if (ctx.download_url) {
            try {
                const url = new URL(ctx.download_url, window.location.origin);
                return decodeURIComponent(url.pathname.split("/").pop() || "Document");
            } catch (error) {
                return "Document";
            }
        }
        const source = ctx.metadata && ctx.metadata.source;
        return source ? source.split("/").pop() : "Document";
    }

    function sourceMeta(ctx) {
        const metadata = ctx.metadata || {};
        const parts = [];
        if (metadata.page !== undefined) {
            parts.push(`p. ${metadata.page}`);
        } else if (metadata.page_number !== undefined) {
            parts.push(`p. ${metadata.page_number}`);
        }
        if (metadata.chunk_id !== undefined) {
            parts.push(`chunk ${metadata.chunk_id}`);
        }
        if (metadata.reranker_score !== undefined) {
            parts.push(`score ${Number(metadata.reranker_score).toFixed(2)}`);
        }
        return parts.join(" | ") || "retrieved source";
    }

    function sourceSnippet(text) {
        const cleaned = String(text || "").replace(/\s+/g, " ").trim();
        if (!cleaned) return "Snippet not available.";
        return cleaned.length > 220 ? `${cleaned.slice(0, 217)}...` : cleaned;
    }

    function highlightCodeBlocks(messageDiv) {
        setTimeout(() => {
            messageDiv.querySelectorAll("pre code").forEach((block) => {
                if (window.hljs) {
                    hljs.highlightElement(block);
                }
            });
        }, 10);
    }

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value;
        return div.innerHTML;
    }

    function renderSafeMarkdown(value) {
        const text = String(value || "");
        if (!window.marked || !window.DOMPurify) {
            return escapeHtml(text);
        }
        return DOMPurify.sanitize(marked.parse(text));
    }

    function updateChatStatus() {
        if (!busy) {
            sendButton.disabled = agentSelectionBlocked || !modelSelect.value;
        }
    }

    function formatError(data, fallback) {
        const status = data && data.status ? `\n\nStatus: \`${data.status}\`` : "";
        const retry = data && data.retry_after ? ` Retry in ${data.retry_after} seconds.` : "";
        return `**Unable to complete the request.**\n\n${(data && data.error) || fallback || "Unknown error."}${retry}${status}`;
    }

    function formatConnectionError(error) {
        if (error && error.name === "AbortError") {
            return "**Request timed out.**\n\nThe response took too long, so the chat was unlocked. Please try again.";
        }
        return `**Connection error:** ${error.message}`;
    }

    function clearChat(targetKnowledgeBaseIds = knowledgeBaseIds) {
        if (busy) return;
        const previousConversationId = conversationId;
        conversationId = createConversationId();
        persistConversationId(conversationId);
        clearServerConversation(previousConversationId, targetKnowledgeBaseIds);
        resetLastTurnId();
        chatbox.replaceChildren();
        clearUploadedFiles();
        if (emptyState) {
            emptyState.hidden = false;
            chatbox.appendChild(emptyState);
        }
        userInput.focus();
    }

    function hasStartedConversation() {
        return Boolean(chatbox.querySelector(".user-message"));
    }

    function requestConversationReset(action, trigger, options = {}) {
        if (busy || typeof action !== "function") return;
        if (!hasStartedConversation() || !newChatModal) {
            action();
            return;
        }
        if (newChatModalTitle) {
            newChatModalTitle.textContent = options.title || "Start a new chat?";
        }
        if (newChatModalDescription) {
            newChatModalDescription.textContent = options.description
                || "This conversation is not saved. If you start a new chat, you won’t be able to return to it.";
        }
        if (confirmNewChatButton) {
            confirmNewChatButton.textContent = options.confirmLabel || "Start new chat";
        }
        pendingConversationReset = action;
        conversationResetTrigger = trigger || document.activeElement;
        newChatModal.hidden = false;
        if (keepConversationButton) keepConversationButton.focus();
    }

    function closeConversationResetModal() {
        if (!newChatModal || newChatModal.hidden) return;
        newChatModal.hidden = true;
        pendingConversationReset = null;
        const trigger = conversationResetTrigger;
        conversationResetTrigger = null;
        if (trigger && typeof trigger.focus === "function") trigger.focus();
    }

    function confirmConversationReset() {
        if (!pendingConversationReset || !newChatModal || newChatModal.hidden) return;
        const action = pendingConversationReset;
        pendingConversationReset = null;
        conversationResetTrigger = null;
        newChatModal.hidden = true;
        action();
    }

    function handleConversationResetModalKeydown(event) {
        if (!newChatModal || newChatModal.hidden) return;
        if (event.key === "Escape") {
            event.preventDefault();
            closeConversationResetModal();
            return;
        }
        if (event.key !== "Tab") return;
        const focusable = [keepConversationButton, confirmNewChatButton].filter(
            element => element && !element.disabled
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function loadOrCreateConversationId() {
        try {
            const stored = window.sessionStorage && sessionStorage.getItem(conversationStorageKey);
            if (stored) return stored;
        } catch (error) {
            // Session storage can be unavailable in restricted browser contexts.
        }

        const nextId = createConversationId();
        persistConversationId(nextId);
        return nextId;
    }

    function createConversationId() {
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            return window.crypto.randomUUID();
        }
        return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    }

    function persistConversationId(value) {
        try {
            if (window.sessionStorage) {
                sessionStorage.setItem(conversationStorageKey, value);
            }
        } catch (error) {
            // Best effort only; the in-memory variable still keeps this tab coherent.
        }
    }

    function updateConversationId(value) {
        if (!value || value === conversationId) return;
        conversationId = value;
        persistConversationId(value);
    }

    async function clearServerConversation(value, targetKnowledgeBaseIds = knowledgeBaseIds) {
        if (!value) return;
        try {
            const query = new URLSearchParams();
            (targetKnowledgeBaseIds || ["default"]).forEach(id => {
                query.append("knowledge_base_ids", id);
            });
            await fetch(
                `/conversation/${encodeURIComponent(value)}?${query.toString()}`,
                {method: "DELETE"}
            );
        } catch (error) {
            console.warn("Unable to clear conversation memory", error);
        }
    }

    function hideEmptyState() {
        if (emptyState) {
            emptyState.hidden = true;
        }
    }

    function resizeInput() {
        userInput.style.height = "auto";
        userInput.style.height = `${Math.min(userInput.scrollHeight, 160)}px`;
    }

    async function handleOcrUpload() {
        const file = ocrFileInput.files && ocrFileInput.files[0];
        if (!file) return;

        ocrFileInput.value = "";
        const originalBtnText = uploadOcrButton.title;
        setControlLabel(uploadOcrButton, "Extracting text...");
        uploadOcrButton.disabled = true;

        hideEmptyState();
        const msgDiv = appendBotMessage(`Extracting text from **${file.name}**...`);

        try {
            const formData = new FormData();
            formData.append("file", file);

            const response = await fetch("/ocr", {
                method: "POST",
                body: formData
            });

            if (!response.ok) {
                const data = await readErrorPayload(response);
                msgDiv.innerHTML = renderSafeMarkdown(
                    formatError(data, "OCR failed")
                );
            } else {
                const data = await response.json();
                const text = data.text || "";
                if (text) {
                    const method = data.ocr_used ? "OCR" : "PDF text parser";
                    msgDiv.innerHTML = renderSafeMarkdown(
                        `**Extracted text** (${data.filename || "document"}, ${method}):\n\n${escapeHtml(text)}`
                    );
                    userInput.value = text;
                    resizeInput();
                } else {
                    msgDiv.innerHTML = "**Extraction result:** empty";
                }
            }
            highlightCodeBlocks(msgDiv);
        } catch (error) {
            msgDiv.innerHTML = renderSafeMarkdown(
                `OCR failed: ${error.message}`
            );
        } finally {
            uploadOcrButton.disabled = false;
            setControlLabel(uploadOcrButton, originalBtnText || "Extract text from image or PDF");
            chatbox.scrollTop = chatbox.scrollHeight;
        }
    }

    async function loadPrompts() {
        if (!promptSelect) return;
        try {
            const response = await fetch("/api/prompts");
            const data = await response.json();
            promptSelect.innerHTML = "<option value=\"\">No system prompt</option>";
            const personal = data.personal || [];
            const personalGroup = document.createElement("optgroup");
            personalGroup.label = "My Prompts";
            personal.forEach(p => {
                const opt = new Option(`[personal] ${p.name}`, `personal::${p.id}`);
                personalGroup.appendChild(opt);
            });
            if (personal.length > 0) {
                promptSelect.appendChild(personalGroup);
            }

            const sharedResponse = await fetch("/api/prompts/shared");
            const sharedData = await sharedResponse.json();
            const shared = sharedData.prompts || [];
            const sharedGroup = document.createElement("optgroup");
            sharedGroup.label = "Shared (admin)";
            shared.forEach(p => {
                const opt = new Option(`[shared] ${p.name}`, `shared::${p.id}`);
                sharedGroup.appendChild(opt);
            });
            if (shared.length > 0) {
                promptSelect.appendChild(sharedGroup);
            }

            if (personal.length === 0 && shared.length === 0) {
                const opt = document.createElement("option");
                opt.value = "";
                opt.disabled = true;
                opt.textContent = "No prompts available";
                promptSelect.appendChild(opt);
            }

            const saved = loadPromptRef();
            if (saved.id) {
                const candidates = saved.scope
                    ? [`${saved.scope}::${saved.id}`]
                    : [`personal::${saved.id}`, `shared::${saved.id}`];
                for (const opt of promptSelect.options) {
                    if (candidates.includes(opt.value)) {
                        opt.selected = true;
                        const resolved = parsePromptValue(opt.value);
                        systemPromptId = resolved.id;
                        systemPromptScope = resolved.scope;
                        break;
                    }
                }
            }
        } catch (error) {
            console.warn("Prompts not available:", error);
        }
    }

    function parsePromptValue(raw) {
        const value = String(raw || "");
        if (!value) return {scope: "", id: ""};
        const sep = value.indexOf("::");
        if (sep === -1) return {scope: "", id: value};
        return {scope: value.slice(0, sep), id: value.slice(sep + 2)};
    }

    function loadPromptRef() {
        try {
            if (!window.sessionStorage) return {scope: "", id: ""};
            const composite = sessionStorage.getItem(promptRefStorageKey);
            if (composite !== null) return parsePromptValue(composite);
            const legacy = sessionStorage.getItem(promptStorageKey);
            return legacy ? {scope: "", id: legacy} : {scope: "", id: ""};
        } catch (e) {
            return {scope: "", id: ""};
        }
    }

    function persistPromptRef(scope, id) {
        try {
            if (!window.sessionStorage) return;
            const value = scope && id ? `${scope}::${id}` : (id || "");
            sessionStorage.setItem(promptRefStorageKey, value);
            sessionStorage.removeItem(promptStorageKey);
        } catch (e) {/* noop */}
    }

    async function loadKnowledgeBases() {
        if (!kbPickerButton) return;
        try {
            const response = await fetch("/api/knowledge-bases");
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || response.statusText);
            knowledgeBaseCatalog = (data.knowledge_bases || []).filter(
                item => item.status === "active"
            );
            maxQueryKnowledgeBases = Math.max(
                1,
                Number(data.limits && data.limits.max_query_knowledge_bases) || 5
            );
            const availableIds = new Set(knowledgeBaseCatalog.map(item => item.id));
            const reconciled = normalizeKnowledgeBaseIds(knowledgeBaseIds).filter(
                id => availableIds.has(id)
            );
            if (reconciled.length === 0 && knowledgeBaseCatalog.length > 0) {
                reconciled.push(
                    availableIds.has("default")
                        ? "default"
                        : knowledgeBaseCatalog[0].id
                );
            }
            const nextKnowledgeBaseIds = reconciled.slice(
                0,
                maxQueryKnowledgeBases
            );
            const selectionChanged = !sameKnowledgeBaseSelection(
                nextKnowledgeBaseIds,
                knowledgeBaseIds
            );
            knowledgeBaseIds = nextKnowledgeBaseIds;
            draftKnowledgeBaseIds = [...knowledgeBaseIds];
            persistKnowledgeBaseIds(knowledgeBaseIds);
            renderKnowledgeBaseSelection();
            renderKnowledgeBaseOptions();
            kbPickerButton.disabled = busy || knowledgeBaseCatalog.length === 0;
            if (selectionChanged && chatbox.querySelector(".message")) {
                appendKnowledgeBaseNotice(
                    "Knowledge base access changed. The active context was updated."
                );
            }
        } catch (error) {
            knowledgeBaseCatalog = [];
            kbPickerSummary.textContent = "Knowledge bases unavailable";
            kbPickerButton.disabled = true;
            console.warn("Knowledge bases not available:", error);
        }
    }

    function toggleKnowledgeBasePicker() {
        if (!kbPickerPopover || kbPickerButton.disabled) return;
        if (!kbPickerPopover.hidden) {
            closeKnowledgeBasePicker();
            return;
        }
        draftKnowledgeBaseIds = [...knowledgeBaseIds];
        kbPickerSearch.value = "";
        setKnowledgeBasePickerError("");
        renderKnowledgeBaseOptions();
        kbPickerPopover.hidden = false;
        kbPickerButton.setAttribute("aria-expanded", "true");
        requestAnimationFrame(() => kbPickerSearch.focus());
    }

    function closeKnowledgeBasePicker() {
        if (!kbPickerPopover) return;
        kbPickerPopover.hidden = true;
        kbPickerButton.setAttribute("aria-expanded", "false");
        draftKnowledgeBaseIds = [...knowledgeBaseIds];
        setKnowledgeBasePickerError("");
        kbPickerButton.focus({preventScroll: true});
    }

    function renderKnowledgeBaseOptions() {
        if (!kbPickerOptions) return;
        const term = String(kbPickerSearch && kbPickerSearch.value || "")
            .trim()
            .toLocaleLowerCase();
        kbPickerOptions.replaceChildren();
        const visible = knowledgeBaseCatalog.filter(item => {
            const haystack = `${item.name || ""} ${item.description || ""}`.toLocaleLowerCase();
            return !term || haystack.includes(term);
        });
        visible.forEach(item => {
            const label = document.createElement("label");
            label.className = "kb-picker-option";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.value = item.id;
            checkbox.checked = draftKnowledgeBaseIds.includes(item.id);
            checkbox.disabled = busy;
            checkbox.addEventListener("change", () => {
                if (checkbox.checked) {
                    if (draftKnowledgeBaseIds.length >= maxQueryKnowledgeBases) {
                        checkbox.checked = false;
                        setKnowledgeBasePickerError(
                            `Select up to ${maxQueryKnowledgeBases} knowledge bases.`
                        );
                        return;
                    }
                    draftKnowledgeBaseIds.push(item.id);
                } else {
                    draftKnowledgeBaseIds = draftKnowledgeBaseIds.filter(
                        id => id !== item.id
                    );
                }
                setKnowledgeBasePickerError("");
                updateKnowledgeBaseDraftStatus();
            });
            const text = document.createElement("span");
            const title = document.createElement("strong");
            title.textContent = item.is_default
                ? `${item.name} (Default)`
                : item.name;
            const description = document.createElement("small");
            description.textContent = item.description || "No description";
            text.append(title, description);
            label.append(checkbox, text);
            kbPickerOptions.appendChild(label);
        });
        if (visible.length === 0) {
            const empty = document.createElement("p");
            empty.className = "kb-picker-empty";
            empty.textContent = "No matching knowledge bases.";
            kbPickerOptions.appendChild(empty);
        }
        updateKnowledgeBaseDraftStatus();
    }

    function updateKnowledgeBaseDraftStatus() {
        if (kbPickerLimit) {
            kbPickerLimit.textContent =
                `${draftKnowledgeBaseIds.length}/${maxQueryKnowledgeBases} selected`;
        }
        if (kbPickerApply) {
            kbPickerApply.disabled = busy || draftKnowledgeBaseIds.length === 0;
        }
    }

    function applyKnowledgeBaseDraft() {
        if (draftKnowledgeBaseIds.length === 0) {
            setKnowledgeBasePickerError("Select at least one knowledge base.");
            return;
        }
        commitKnowledgeBaseSelection(draftKnowledgeBaseIds);
        closeKnowledgeBasePicker();
        userInput.focus();
    }

    function commitKnowledgeBaseSelection(nextIds) {
        const normalized = normalizeKnowledgeBaseIds(nextIds).slice(
            0,
            maxQueryKnowledgeBases
        );
        if (normalized.length === 0 || sameKnowledgeBaseSelection(normalized, knowledgeBaseIds)) {
            return;
        }
        knowledgeBaseIds = normalized;
        draftKnowledgeBaseIds = [...normalized];
        persistKnowledgeBaseIds(knowledgeBaseIds);
        if (agentActive) switchToCustomChat();
        renderKnowledgeBaseSelection();
        appendKnowledgeBaseNotice(
            `Knowledge base context updated: ${knowledgeBaseSelectionLabel(knowledgeBaseIds)}.`
        );
    }

    function renderKnowledgeBaseSelection() {
        if (!kbPickerSummary || !kbChips) return;
        kbPickerSummary.textContent = knowledgeBaseSelectionLabel(knowledgeBaseIds);
        kbChips.replaceChildren();
        knowledgeBaseIds.forEach((id, index) => {
            const item = knowledgeBaseCatalog.find(candidate => candidate.id === id);
            if (!item) return;
            const chip = document.createElement("span");
            chip.className = "kb-chip";
            const label = document.createElement("span");
            label.textContent = item.name;
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "×";
            remove.disabled = busy || knowledgeBaseIds.length === 1;
            remove.setAttribute("aria-label", `Remove ${item.name}`);
            remove.addEventListener("click", () => {
                if (knowledgeBaseIds.length === 1) return;
                commitKnowledgeBaseSelection(
                    knowledgeBaseIds.filter(value => value !== id)
                );
                requestAnimationFrame(
                    () => focusKnowledgeBaseControlAfterRemoval(index)
                );
            });
            chip.append(label, remove);
            kbChips.appendChild(chip);
        });
    }

    function focusKnowledgeBaseControlAfterRemoval(removedIndex) {
        const removeButtons = kbChips
            ? [...kbChips.querySelectorAll(".kb-chip button")]
            : [];
        const nextButton = removeButtons[
            Math.min(removedIndex, removeButtons.length - 1)
        ];
        if (nextButton && !nextButton.disabled) {
            nextButton.focus({preventScroll: true});
            return;
        }
        if (kbPickerButton) {
            kbPickerButton.focus({preventScroll: true});
        }
    }

    function knowledgeBaseSelectionLabel(ids) {
        const names = ids.map(id => {
            const item = knowledgeBaseCatalog.find(candidate => candidate.id === id);
            return item ? item.name : id;
        });
        if (names.length <= 2) return names.join(", ") || "Select knowledge bases";
        return `${names[0]}, ${names[1]} +${names.length - 2}`;
    }

    function appendKnowledgeBaseNotice(text) {
        if (!text || !chatbox.querySelector(".message")) return;
        const notice = document.createElement("div");
        notice.className = "kb-context-notice";
        notice.textContent = text;
        chatbox.appendChild(notice);
        chatbox.scrollTop = chatbox.scrollHeight;
    }

    function setKnowledgeBasePickerError(message) {
        if (!kbPickerError) return;
        kbPickerError.textContent = message || "";
        kbPickerError.hidden = !message;
    }

    function sameKnowledgeBaseSelection(left, right) {
        return left.length === right.length
            && left.every((value, index) => value === right[index]);
    }

    function normalizeKnowledgeBaseIds(values) {
        if (!Array.isArray(values)) return [];
        return [...new Set(
            values
                .filter(value => typeof value === "string")
                .map(value => value.trim())
                .filter(Boolean)
        )];
    }

    function loadKnowledgeBaseIds() {
        try {
            if (!window.sessionStorage) return ["default"];
            const stored = sessionStorage.getItem(knowledgeBaseStorageKey);
            if (stored) {
                const parsed = JSON.parse(stored);
                if (Array.isArray(parsed) && parsed.length > 0) {
                    const normalized = normalizeKnowledgeBaseIds(parsed);
                    return normalized.length > 0 ? normalized : ["default"];
                }
            }
            const legacy = sessionStorage.getItem(legacyKnowledgeBaseStorageKey);
            const normalized = normalizeKnowledgeBaseIds([legacy || "default"]);
            return normalized.length > 0 ? normalized : ["default"];
        } catch (error) {
            return ["default"];
        }
    }

    function persistKnowledgeBaseIds(values) {
        try {
            if (window.sessionStorage) {
                const normalized = normalizeKnowledgeBaseIds(values);
                sessionStorage.setItem(
                    knowledgeBaseStorageKey,
                    JSON.stringify(normalized.length > 0 ? normalized : ["default"])
                );
                sessionStorage.removeItem(legacyKnowledgeBaseStorageKey);
            }
        } catch (error) {/* best effort */}
    }

    async function handleUnavailableKnowledgeBase(data, preservedMessage = null) {
        if (
            !data
            || !["knowledge_base_not_found", "knowledge_base_deleting", "knowledge_base_delete_failed"].includes(data.status)
        ) {
            return false;
        }

        await loadKnowledgeBases();
        if (preservedMessage) {
            appendKnowledgeBaseNotice(
                "One or more knowledge bases are no longer available. Server memory was revalidated."
            );
        }
        return true;
    }

    async function handleFileUpload(event) {
        const input = event.target;
        const files = input.files;
        if (!files || files.length === 0) return;

        for (const file of files) {
            const formData = new FormData();
            formData.append("file", file);

            try {
                const response = await fetch("/upload-to-chat", {
                    method: "POST",
                    body: formData
                });

                if (!response.ok) {
                    const data = await response.json();
                    appendMessage(`**Upload failed:** ${data.error || "Unknown error"}`, "bot-message");
                    continue;
                }

                const data = await response.json();
                uploadedFiles.push({
                    id: data.file_id || data.id,
                    name: data.filename,
                    type: data.type
                });
            } catch (err) {
                appendMessage(`**Upload error:** ${err.message}`, "bot-message");
            }
        }
        renderAttachedFiles();
        input.value = "";
    }

    function renderAttachedFiles() {
        if (!attachedFilesDiv) return;
        attachedFilesDiv.innerHTML = "";
        if (uploadedFiles.length === 0) return;

        uploadedFiles.forEach((file, idx) => {
            const chip = document.createElement("div");
            chip.className = "file-chip";
            chip.innerHTML = `
                <span class="file-chip-name">${escapeHtml(file.name)}</span>
                <button type="button" class="file-chip-remove" data-idx="${idx}" aria-label="Remove">&times;</button>
            `;
            chip.querySelector(".file-chip-remove").addEventListener("click", () => {
                uploadedFiles.splice(idx, 1);
                renderAttachedFiles();
            });
            attachedFilesDiv.appendChild(chip);
        });
    }

    function clearUploadedFiles() {
        uploadedFiles.length = 0;
        if (attachedFilesDiv) {
            attachedFilesDiv.innerHTML = "";
        }
    }

    async function loadAgents() {
        if (!agentSelect) return;
        const pending = readPendingAgent();
        try {
            const response = await fetch("/api/agents");
            if (!response.ok) throw new Error("Agent catalog unavailable");
            const data = await response.json();
            agentsCatalog = data.agents || [];
            agentSelect.innerHTML = '<option value="">None</option>';
            agentsCatalog.forEach(agent => {
                const opt = new Option(agent.name, agent.id);
                if (!agent.available) {
                    opt.disabled = true;
                    opt.textContent = `${agent.name} (unavailable)`;
                }
                agentSelect.appendChild(opt);
            });
            if (pending) {
                const agent = agentsCatalog.find(a => a.id === pending);
                if (agent && agent.available) {
                    const previousKnowledgeBaseIds = [...knowledgeBaseIds];
                    agentSelect.value = pending;
                    selectedAgentId = pending;
                    agentSelectionBlocked = false;
                    applyAgentConfig(agent);
                    setAgentActive(true);
                    startAgentConversation(previousKnowledgeBaseIds);
                } else {
                    handleAgentUnavailable(pending, agent);
                }
            }
        } catch (error) {
            console.warn("Agents not available:", error);
            if (pending) {
                handleAgentUnavailable(pending);
            }
        }
    }

    function applyAgentConfig(agent) {
        if (!agent) return;
        if (modelSelect) {
            for (const opt of modelSelect.options) {
                if (opt.dataset.provider === agent.provider_id
                    && opt.dataset.model === agent.model_id) {
                    opt.selected = true;
                    break;
                }
            }
        }
        const kbIds = Array.isArray(agent.knowledge_base_ids)
            ? agent.knowledge_base_ids
            : [];
        const availableIds = new Set(knowledgeBaseCatalog.map(item => item.id));
        const reconciled = kbIds.filter(id => availableIds.has(id));
        if (reconciled.length > 0) {
            knowledgeBaseIds = reconciled.slice(0, maxQueryKnowledgeBases);
            draftKnowledgeBaseIds = [...knowledgeBaseIds];
            persistKnowledgeBaseIds(knowledgeBaseIds);
        }
        renderKnowledgeBaseSelection();
        if (promptSelect) {
            const ref = agent.prompt_ref || {};
            const target = ref && ref.id && ref.scope
                ? `${ref.scope}::${ref.id}`
                : "";
            let matched = false;
            for (const opt of promptSelect.options) {
                if (opt.value === target) {
                    opt.selected = true;
                    matched = true;
                    break;
                }
            }
            if (!matched && promptSelect.options.length > 0) {
                promptSelect.options[0].selected = true;
            }
            const resolved = parsePromptValue(promptSelect.value);
            systemPromptId = resolved.id;
            systemPromptScope = resolved.scope;
            persistPromptRef(systemPromptScope, systemPromptId);
        }
        updateChatStatus();
    }

    function setAgentActive(active) {
        agentActive = active;
        renderKnowledgeBaseSelection();
        updateChatStatus();
    }

    function applyAgentSelection(id, agent, previousKnowledgeBaseIds) {
        selectedAgentId = id;
        persistSelectedAgent(id);
        if (agentSelect) agentSelect.value = id;
        if (id) {
            if (agent && agent.available) {
                agentSelectionBlocked = false;
                applyAgentConfig(agent);
                setAgentActive(true);
                startAgentConversation(previousKnowledgeBaseIds);
            } else {
                handleAgentUnavailable(id, agent);
            }
            return;
        }
        switchToCustomChat();
        resetConfigToDefaults();
    }

    function switchToCustomChat() {
        agentActive = false;
        agentSelectionBlocked = false;
        selectedAgentId = "";
        persistSelectedAgent("");
        if (agentSelect) agentSelect.value = "";
        renderKnowledgeBaseSelection();
    }

    function resetConfigToDefaults() {
        if (modelSelect && modelSelect.options.length > 0) {
            const defaultOpt = Array.from(modelSelect.options).find(
                opt => opt.dataset.default === "true"
            );
            if (defaultOpt) {
                defaultOpt.selected = true;
            } else {
                modelSelect.options[0].selected = true;
            }
        }
        const availableIds = knowledgeBaseCatalog.map(item => item.id);
        const defaultKbId = availableIds.includes("default")
            ? "default"
            : (availableIds[0] || "default");
        knowledgeBaseIds = [defaultKbId];
        draftKnowledgeBaseIds = [...knowledgeBaseIds];
        persistKnowledgeBaseIds(knowledgeBaseIds);
        if (promptSelect) {
            promptSelect.value = "";
            systemPromptId = "";
            systemPromptScope = "";
            persistPromptRef("", "");
        }
        renderKnowledgeBaseSelection();
        updateChatStatus();
    }

    function startAgentConversation(previousKnowledgeBaseIds = knowledgeBaseIds) {
        clearChat(previousKnowledgeBaseIds);
    }

    async function newChat() {
        if (busy) return;
        clearChat();
        if (selectedAgentId) {
            setBusy(true);
            try {
                await revalidateActiveAgent();
            } finally {
                setBusy(false);
            }
        }
    }

    async function revalidateActiveAgent() {
        const agentId = selectedAgentId;
        if (!agentId) return;
        try {
            const response = await fetch(
                `/api/agents/${encodeURIComponent(agentId)}`
            );
            if (!response.ok) {
                handleAgentUnavailable(agentId);
                return;
            }
            const agent = await response.json();
            if (!agent || !agent.available) {
                handleAgentUnavailable(agentId, agent);
                return;
            }
            applyAgentConfig(agent);
        } catch (error) {
            handleAgentUnavailable(agentId);
        }
    }

    function handleAgentUnavailable(agentId, agent) {
        agentActive = false;
        agentSelectionBlocked = true;
        selectedAgentId = agentId;
        persistSelectedAgent(agentId);
        if (agentSelect) {
            let option = Array.from(agentSelect.options).find(
                candidate => candidate.value === agentId
            );
            if (!option) {
                option = new Option("Unavailable Agent", agentId);
                agentSelect.appendChild(option);
            }
            option.disabled = true;
            option.textContent = `${(agent && agent.name) || "Agent"} (unavailable)`;
            agentSelect.value = agentId;
        }
        updateChatStatus();
        const issues = agent && Array.isArray(agent.issues) ? agent.issues : [];
        const detail = issues.length
            ? issues.map(issue => issue.message).join(" ")
            : "non più disponibile";
        appendBotMessage(
            `**Agent non più disponibile.**\n\nL'agent selezionato non è più disponibile (${detail}). ` +
            "Seleziona esplicitamente un altro Agent o None per continuare."
        );
    }

    function readPendingAgent() {
        try {
            const params = new URLSearchParams(window.location.search);
            const fromUrl = params.get("agent");
            if (fromUrl) {
                persistSelectedAgent(fromUrl);
                const cleanUrl = window.location.pathname;
                window.history.replaceState({}, "", cleanUrl);
                return fromUrl;
            }
            if (window.sessionStorage) {
                return sessionStorage.getItem(agentStorageKey) || "";
            }
        } catch (e) {/* noop */}
        return "";
    }

    function persistSelectedAgent(id) {
        try {
            if (!window.sessionStorage) return;
            if (id) {
                sessionStorage.setItem(agentStorageKey, id);
            } else {
                sessionStorage.removeItem(agentStorageKey);
            }
        } catch (e) {/* noop */}
    }

// History drawer
    const historyToggle = document.getElementById("historyToggle");
    const historyDrawer = document.getElementById("historyDrawer");
    const closeHistoryDrawer = document.getElementById("closeHistoryDrawer");
    const toggleHistoryDrawer = document.getElementById("toggleHistoryDrawer");
    const searchHistoryInput = document.getElementById("searchHistoryInput");
    const historyList = document.getElementById("historyList");
    const historyEmptyState = document.getElementById("historyEmptyState");
    
    let conversationHistory = [];
    let filteredHistory = [];
    let historyActionInProgress = false;
    
    if (historyToggle) {
        historyToggle.addEventListener("click", openHistoryDrawer);
    }
    if (closeHistoryDrawer) {
        closeHistoryDrawer.addEventListener("click", closeHistoryDrawerHandler);
    }
    if (toggleHistoryDrawer) {
        toggleHistoryDrawer.addEventListener("click", openHistoryDrawer);
    }
    if (searchHistoryInput) {
        searchHistoryInput.addEventListener("input", filterHistory);
    }
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && historyDrawer && !historyDrawer.hidden) {
            closeHistoryDrawerHandler();
        }
    });
    
    let lastFocusedElement = null;
    
    async function openHistoryDrawer() {
        if (!historyDrawer) return;
        lastFocusedElement = document.activeElement;
        historyDrawer.hidden = false;
        await loadConversationHistory();
        renderHistoryList();
        if (searchHistoryInput) {
            searchHistoryInput.focus();
        }
        document.addEventListener("keydown", handleDrawerKeydown);
    }
    
    function closeHistoryDrawerHandler() {
        if (!historyDrawer) return;
        historyDrawer.hidden = true;
        document.removeEventListener("keydown", handleDrawerKeydown);
        if (lastFocusedElement) {
            lastFocusedElement.focus();
            lastFocusedElement = null;
        }
    }
    
    function handleDrawerKeydown(e) {
        if (!historyDrawer || historyDrawer.hidden) return;
        if (e.key !== "Tab") return;
        
        const focusableElements = historyDrawer.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];
        
        if (e.shiftKey && document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
        } else if (!e.shiftKey && document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
        }
    }
    
    async function loadConversationHistory() {
        if (!historyList) return;
        historyList.innerHTML = '<div class="history-loading">Loading...</div>';
        try {
            const response = await fetch("/api/conversations?status=active");
            if (!response.ok) {
                conversationHistory = [];
                filteredHistory = [];
                historyList.innerHTML = "";
                if (historyEmptyState) historyEmptyState.hidden = false;
                return;
            }
            const data = await response.json();
            conversationHistory = data.conversations || [];
            filteredHistory = [...conversationHistory];
            historyList.innerHTML = "";
        } catch (error) {
            console.error("Failed to load conversation history:", error);
            conversationHistory = [];
            filteredHistory = [];
            historyList.innerHTML = '<div class="history-error">Failed to load conversations</div>';
        }
    }
    
    function filterHistory() {
        const query = (searchHistoryInput?.value || "").toLowerCase().trim();
        if (!query) {
            filteredHistory = [...conversationHistory];
        } else {
            filteredHistory = conversationHistory.filter(conv => 
                (conv.title || "").toLowerCase().includes(query)
            );
        }
        renderHistoryList();
    }
    
    function renderHistoryList() {
        if (!historyList) return;
        
        if (filteredHistory.length === 0) {
            historyList.innerHTML = "";
            if (historyEmptyState) historyEmptyState.hidden = false;
            return;
        }
        
        if (historyEmptyState) historyEmptyState.hidden = true;
        
        historyList.innerHTML = filteredHistory.map(conv => {
            const date = new Date(conv.updated_at * 1000).toLocaleString("it-IT");
            const isArchived = conv.status === "archived";
            const archivedClass = isArchived ? "archived" : "";
            const archiveText = isArchived ? "Unarchive" : "Archive";
            return `
                <div class="history-item" data-id="${conv.id}" role="listitem" tabindex="0" aria-label="Conversation: ${escapeHtml(conv.title || "Untitled")}">
                    <h4 class="history-item-title">${escapeHtml(conv.title || "Untitled")}</h4>
                    <div class="history-item-meta">
                        <span>${date}</span>
                        <div class="history-item-actions">
                            <button type="button" class="rename-btn" data-id="${conv.id}" aria-label="Rename conversation">Rename</button>
                            <button type="button" class="archive-btn ${archivedClass}" data-id="${conv.id}" aria-label="${archiveText} conversation">${archiveText}</button>
                            <button type="button" class="delete-btn" data-id="${conv.id}" aria-label="Delete conversation">Delete</button>
                        </div>
                    </div>
                </div>
            `;
        }).join("");
        
        historyList.querySelectorAll(".history-item").forEach(item => {
            item.addEventListener("click", (e) => {
                if (e.target.tagName === "BUTTON") return;
                const id = item.dataset.id;
                loadConversation(id);
            });
            item.addEventListener("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    const id = item.dataset.id;
                    loadConversation(id);
                }
            });
        });
        
        historyList.querySelectorAll(".rename-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                const conv = conversationHistory.find(c => c.id === id);
                if (conv) {
                    openRenameModal(id, conv.title);
                }
            });
        });
        
        historyList.querySelectorAll(".archive-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                toggleArchive(id);
            });
        });
        
        historyList.querySelectorAll(".delete-btn").forEach(btn => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                deleteConversation(id);
            });
        });
    }
    
    async function loadConversation(id) {
        if (busy) {
            showHistoryStatus("Please wait for the current response to complete", 3000);
            return;
        }
        try {
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}/messages`);
            if (!response.ok) {
                showHistoryStatus("Failed to load conversation", 5000);
                return;
            }
            const data = await response.json();

            // Release the warm server-side conversation for the previous chat so
            // loading a conversation does not leak ephemeral state. This is best
            // effort and intentionally not awaited (matches clearChat behaviour).
            clearServerConversation(conversationId);

            // Restore conversation continuity: adopt the loaded conversation's IDs
            // so subsequent messages continue the same thread instead of starting
            // a fresh conversation. clearChat() must NOT be used here because it
            // generates a brand-new conversationId and wipes lastTurnId.
            const conv = conversationHistory.find(c => c.id === id) || {};
            const nextConversationId = conv.client_conversation_id || conversationId;
            const nextTurnId = conv.last_turn_id || null;

            conversationId = nextConversationId;
            persistConversationId(conversationId);
            currentTurnId = null;
            if (nextTurnId) {
                lastTurnId = nextTurnId;
                persistLastTurnId(nextTurnId);
            } else {
                resetLastTurnId();
            }

            chatbox.replaceChildren();
            clearUploadedFiles();
            if (emptyState) emptyState.hidden = true;

            const messages = data.messages || [];
            for (const msg of messages) {
                if (msg.role === "user") {
                    appendMessage(msg.content, "user-message");
                } else if (msg.role === "assistant") {
                    appendBotMessage(msg.content);
                }
            }

            closeHistoryDrawerHandler();
            showHistoryStatus("Conversation loaded");
        } catch (error) {
            console.error("Failed to load conversation:", error);
            showHistoryStatus("Failed to load conversation", 5000);
        }
    }
    
    async function toggleArchive(id) {
        if (historyActionInProgress) return;
        historyActionInProgress = true;
        try {
            const conv = conversationHistory.find(c => c.id === id);
            if (!conv) return;

            const isArchived = conv.status === "archived";
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ archived: !isArchived })
            });

            if (!response.ok) {
                showHistoryStatus("Failed to update conversation", 5000);
                return;
            }

            await loadConversationHistory();
            renderHistoryList();
            const action = !isArchived ? "Archived" : "Unarchived";
            showHistoryStatus(`${action} conversation`);
        } catch (error) {
            console.error("Failed to archive conversation:", error);
            showHistoryStatus("Failed to update conversation", 5000);
        } finally {
            historyActionInProgress = false;
        }
    }
    
    async function deleteConversation(id) {
        if (historyActionInProgress) return;
        if (!confirm("Delete this conversation? This cannot be undone.")) return;
        historyActionInProgress = true;
        try {
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
                method: "DELETE"
            });

            if (!response.ok) {
                showHistoryStatus("Failed to delete conversation", 5000);
                return;
            }

            await loadConversationHistory();
            renderHistoryList();
            showHistoryStatus("Conversation deleted");
        } catch (error) {
            console.error("Failed to delete conversation:", error);
            showHistoryStatus("Failed to delete conversation", 5000);
        } finally {
            historyActionInProgress = false;
        }
    }
    
    const historyStatus = document.getElementById("historyStatus");
    const renameModal = document.getElementById("renameModal");
    const renameInput = document.getElementById("renameInput");
    const cancelRenameButton = document.getElementById("cancelRenameButton");
    const confirmRenameButton = document.getElementById("confirmRenameButton");
    let pendingRenameId = null;
    
    let historyStatusTimer = null;
    function showHistoryStatus(message, duration = 3000) {
        if (!historyStatus) return;
        if (historyStatusTimer) {
            clearTimeout(historyStatusTimer);
            historyStatusTimer = null;
        }
        historyStatus.textContent = message;
        historyStatus.hidden = false;
        historyStatusTimer = setTimeout(() => {
            historyStatus.hidden = true;
            historyStatus.textContent = "";
            historyStatusTimer = null;
        }, duration);
    }
    
    if (renameModal) {
        renameModal.addEventListener("click", (event) => {
            if (event.target === renameModal) closeRenameModal();
        });
    }
    if (cancelRenameButton) {
        cancelRenameButton.addEventListener("click", closeRenameModal);
    }
    if (confirmRenameButton) {
        confirmRenameButton.addEventListener("click", confirmRename);
    }
    if (renameInput) {
        renameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                confirmRename();
            } else if (e.key === "Escape") {
                e.preventDefault();
                closeRenameModal();
            }
        });
    }
    
    function openRenameModal(id, currentTitle) {
        if (!renameModal || !renameInput) return;
        pendingRenameId = id;
        renameInput.value = currentTitle || "";
        renameModal.hidden = false;
        renameInput.focus();
    }
    
    function closeRenameModal() {
        if (!renameModal) return;
        renameModal.hidden = true;
        pendingRenameId = null;
        if (renameInput) renameInput.value = "";
    }
    
    async function confirmRename() {
        if (!pendingRenameId || !renameInput) return;
        if (historyActionInProgress) return;
        const newTitle = renameInput.value.trim();
        if (!newTitle) {
            showHistoryStatus("Title cannot be empty", 3000);
            return;
        }

        historyActionInProgress = true;
        if (confirmRenameButton) confirmRenameButton.disabled = true;
        try {
            const response = await fetch(`/api/conversations/${encodeURIComponent(pendingRenameId)}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: newTitle })
            });

            if (!response.ok) {
                showHistoryStatus("Failed to rename conversation", 5000);
                closeRenameModal();
                return;
            }

            await loadConversationHistory();
            renderHistoryList();
            closeRenameModal();
            showHistoryStatus("Conversation renamed");
        } catch (error) {
            console.error("Failed to rename conversation:", error);
            showHistoryStatus("Failed to rename conversation", 5000);
            closeRenameModal();
        } finally {
            historyActionInProgress = false;
            if (confirmRenameButton) confirmRenameButton.disabled = false;
        }
    }

    (async () => {
        await Promise.all([loadKnowledgeBases(), loadModels(), loadPrompts()]);
        await loadAgents();
        resizeInput();
    })();
});
