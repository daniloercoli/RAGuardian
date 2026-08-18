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
    let retryableTurnRequest = null;
    let activeHistoryId = null;
    let activeConversationArchived = false;
    let activeConversationHistoryState = lastTurnId ? "saved" : "empty";

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

    function applyHistoryOutcome(event) {
        const historySaved = Boolean(event && event.history_saved === true);

        if (historySaved && currentTurnId) {
            lastTurnId = currentTurnId;
            persistLastTurnId(lastTurnId);
            activeConversationHistoryState = "saved";
        } else if (event && event.type === "done") {
            const historyStatus = String(event.history_status || "error");
            const volatileStatuses = new Set([
                "disabled",
                "not_requested",
                "client_turn_id_required"
            ]);
            activeConversationHistoryState = volatileStatuses.has(historyStatus)
                ? "volatile"
                : "draft";
            if (activeConversationHistoryState === "draft") {
                showHistoryStatus(
                    "This reply was not saved. Start a new chat before continuing.",
                    8000
                );
            }
        }
        currentTurnId = null;
        return historySaved;
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
                    description: activeConversationHistoryState === "saved"
                        ? "This conversation is saved and will remain available in History."
                        : "This conversation has unsaved messages that may not be available after opening Configuration.",
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
        if (activeConversationArchived) {
            showHistoryStatus("Unarchive this conversation before continuing", 4000);
            return;
        }
        if (activeConversationHistoryState === "draft") {
            showHistoryStatus(
                retryableTurnRequest
                    ? "Retry the interrupted turn or start a new chat before continuing."
                    : "The previous reply was not saved. Start a new chat before continuing.",
                6000
            );
            return;
        }
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
        const selected = modelSelect.selectedOptions[0];
        const useCodeInterpreter = codeInterpreterEnabled && uploadedFiles.length > 0;
        const attachments = uploadedFiles.map(file => ({
            id: file.id,
            file_id: file.id,
            name: file.name,
            type: file.type
        }));
        const body = applyQueryConfiguration({
            query,
            conversation_id: conversationId,
            stream: true,
            stream_format: "ndjson",
            use_code_interpreter: useCodeInterpreter
        }, selected);
        if (attachments.length > 0) {
            body.attached_files = attachments;
        }

        const turnRequest = {
            body,
            useCodeInterpreter,
            messageDiv: appendBotMessage(
                useCodeInterpreter ? "Preparing analysis..." : ""
            )
        };
        retryableTurnRequest = turnRequest;
        await executeTurnRequest(turnRequest);
    }

    async function executeTurnRequest(turnRequest, isRetry = false) {
        if (!turnRequest || !turnRequest.body || busy) return;

        activeConversationHistoryState = "pending";
        currentTurnId = turnRequest.body.turn_id;
        clearTurnRetryUi(turnRequest.messageDiv);
        if (isRetry && turnRequest.useCodeInterpreter) {
            renderBotAnswer(turnRequest.messageDiv, "_Retrying analysis..._");
        }
        setBusy(true);
        const askTimeout = createAskTimeout();
        let terminalOutcome = false;

        try {
            const response = await postAsk(turnRequest.body, askTimeout);
            if (!response.ok) {
                const data = await readErrorPayload(response);
                const errorCode = turnErrorCode(data);
                const lostVolatileResult = errorCode === "volatile_result_lost";
                terminalOutcome = false;
                if (!lostVolatileResult && !isRetryableTurnError(response, data)) {
                    terminalOutcome = true;
                }
                reconcileTurnIdFromError(data);
                renderBotAnswer(
                    turnRequest.messageDiv,
                    formatError(data, response.statusText)
                );
                if (agentActive && selectedAgentId) {
                    await revalidateActiveAgent();
                }
                await handleUnavailableKnowledgeBase(data, turnRequest.messageDiv);
                if (lostVolatileResult) {
                    prepareLostResultRecovery(turnRequest);
                    markTurnRetryable(
                        turnRequest,
                        "The saved draft expired. Regenerate this turn explicitly; the previous draft will be replaced.",
                        "Regenerate and replace draft"
                    );
                } else if (!terminalOutcome) {
                    markTurnRetryable(
                        turnRequest,
                        "The server has not finalized this turn. Retry with the same request."
                    );
                }
            } else {
                const terminalType = turnRequest.useCodeInterpreter
                    ? await renderCodeInterpreterStream(
                        response,
                        turnRequest.messageDiv,
                        askTimeout
                    )
                    : await renderStreamingResponse(
                        response,
                        turnRequest.messageDiv,
                        askTimeout
                    );
                const lostVolatileResult = terminalType === "volatile_result_lost";
                terminalOutcome = false;
                if (
                    !lostVolatileResult
                    && (terminalType === "done" || terminalType === "error")
                ) {
                    terminalOutcome = true;
                }
                if (
                    terminalType === "done"
                    && activeConversationHistoryState === "draft"
                ) {
                    terminalOutcome = false;
                    markTurnRetryable(
                        turnRequest,
                        "The reply was received but history was not committed. Retry this exact turn."
                    );
                } else if (lostVolatileResult) {
                    prepareLostResultRecovery(turnRequest);
                    markTurnRetryable(
                        turnRequest,
                        "The saved draft expired. Regenerate this turn explicitly; the previous draft will be replaced.",
                        "Regenerate and replace draft"
                    );
                } else if (!terminalOutcome) {
                    markTurnRetryable(
                        turnRequest,
                        "The response ended before the server confirmed completion."
                    );
                }
            }
        } catch (error) {
            if (!terminalOutcome) {
                renderBotAnswer(turnRequest.messageDiv, formatConnectionError(error));
                markTurnRetryable(
                    turnRequest,
                    "The server did not confirm this turn. Retry with the same request."
                );
            }
        } finally {
            askTimeout.clear();
            if (terminalOutcome) {
                if (retryableTurnRequest === turnRequest) {
                    retryableTurnRequest = null;
                }
                clearTurnRetryUi(turnRequest.messageDiv);
                currentTurnId = null;
                if (turnRequest.body.attached_files) {
                    uploadedFiles.length = 0;
                    renderAttachedFiles();
                }
            }
            setBusy(false);
            userInput.focus();
        }
    }

    function prepareLostResultRecovery(turnRequest) {
        turnRequest.body = {
            ...turnRequest.body,
            regenerate_lost_result: true
        };
    }

    function markTurnRetryable(turnRequest, message, actionLabel = "Retry this turn") {
        retryableTurnRequest = turnRequest;
        currentTurnId = turnRequest.body.turn_id;
        activeConversationHistoryState = "draft";
        clearTurnRetryUi(turnRequest.messageDiv);

        const status = document.createElement("p");
        status.className = "turn-retry-status";
        status.textContent = String(message || "The request was interrupted.");
        turnRequest.messageDiv.appendChild(status);

        const action = document.createElement("button");
        action.type = "button";
        action.className = "turn-retry-action";
        action.textContent = actionLabel;
        action.addEventListener("click", retryPendingTurn);
        turnRequest.messageDiv.appendChild(action);
        showHistoryStatus(
            "This turn was not confirmed. Retry it or start a new chat before continuing.",
            8000
        );
    }

    function clearTurnRetryUi(messageDiv) {
        if (!messageDiv) return;
        messageDiv.querySelectorAll(".turn-retry-status, .turn-retry-action")
            .forEach(element => element.remove());
    }

    async function retryPendingTurn() {
        if (busy || !retryableTurnRequest) return;
        const turnRequest = retryableTurnRequest;
        // Transport retries reuse the exact request.  The only allowed
        // mutation is the explicit recovery flag set after a lost staged
        // result; the idempotency key and fingerprint inputs stay unchanged.
        await executeTurnRequest(turnRequest, true);
    }

    function applyQueryConfiguration(body, selectedModel) {
        body.persist_history = true;
        body.parent_turn_id = lastTurnId || undefined;
        if (window.crypto && typeof window.crypto.randomUUID === "function") {
            body.turn_id = window.crypto.randomUUID();
        } else {
            body.turn_id = `turn-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
        }
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
        appendSources(preferredSources(data.context, data.sources));
    }

    async function renderCodeInterpreterStream(response, messageDiv, timeout) {
        const state = {
            code: "",
            result: null,
            hasError: false,
            terminalType: null,
            retryableError: false,
            status: "Preparing analysis...",
            recoveryPromise: null
        };

        const onEvent = (event) => {
            if (!event || state.terminalType) return;
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
                state.terminalType = "done";
                updateConversationId(event.conversation_id);
                state.code = event.code || state.code;
                state.result = event.result || state.result || {};
                state.status = "";
                renderCodeInterpreterPayload(messageDiv, state.code, state.result, state.status);
                appendSources(preferredSources(event.context, event.sources));
                applyHistoryOutcome(event);
            } else if (event.type === "error") {
                state.hasError = true;
                state.terminalType = "error";
                state.retryableError = isRetryableStreamError(event);
                state.errorCode = turnErrorCode(event);
                reconcileTurnIdFromError(event);
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
            return streamTerminalType(state);
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
            if (state.terminalType) {
                reader.cancel().catch(() => {});
                await waitForKnowledgeBaseRecovery(state);
                return streamTerminalType(state);
            }
        }
        buffer += decoder.decode();
        parseNdjsonLines(buffer, onEvent);
        await waitForKnowledgeBaseRecovery(state);
        return streamTerminalType(state);
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

    function isRetryableTurnError(response, data) {
        const code = turnErrorCode(data);
        if ([
            "turn_generating",
            "turn_in_progress",
            "timeout",
            "rate_limited"
        ].includes(code)) {
            return true;
        }
        const statusCode = Number(response && response.status) || 0;
        return statusCode === 408
            || statusCode === 425
            || statusCode === 429
            || statusCode >= 500;
    }

    function turnErrorCode(data) {
        return String(
            (data && (data.code || data.status)) || ""
        ).toLowerCase();
    }

    function reconcileTurnIdFromError(data) {
        if (!data || typeof data !== "object") return;
        activeConversationHistoryState = lastTurnId ? "saved" : "empty";
        const code = String(data.code || data.status || "").toLowerCase();
        const hasExpectedParent = Object.prototype.hasOwnProperty.call(
            data,
            "expected_parent_turn_id"
        );
        if (hasExpectedParent) {
            const expected = data.expected_parent_turn_id;
            if (typeof expected === "string" && expected) {
                lastTurnId = expected;
                persistLastTurnId(lastTurnId);
            } else if (expected === null) {
                resetLastTurnId();
            }
        } else if (code === "continuity_error") {
            resetLastTurnId();
        }
        if (code === "continuity_error" || code === "turn_id_conflict") {
            currentTurnId = null;
        }
    }

    async function renderStreamingResponse(response, messageDiv, timeout) {
        const state = {
            answerText: "",
            hasError: false,
            terminalType: null,
            retryableError: false,
            recoveryPromise: null
        };

        if (!response.body || !response.body.getReader) {
            const text = await response.text();
            parseNdjsonLines(text, (event) => handleStreamEvent(event, state, messageDiv));
            await waitForKnowledgeBaseRecovery(state);
            return streamTerminalType(state);
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
            if (state.terminalType) {
                reader.cancel().catch(() => {});
                await waitForKnowledgeBaseRecovery(state);
                return streamTerminalType(state);
            }
        }

        buffer += decoder.decode();
        parseNdjsonLines(buffer, (event) => handleStreamEvent(event, state, messageDiv));
        await waitForKnowledgeBaseRecovery(state);
        return streamTerminalType(state);
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
        if (!event || state.terminalType) return;

        if (event.type === "token") {
            state.answerText += event.text || "";
            renderBotAnswer(messageDiv, state.answerText);
        } else if (event.type === "meta") {
            updateConversationId(event.conversation_id);
        } else if (event.type === "done") {
            state.terminalType = "done";
            updateConversationId(event.conversation_id);
            state.answerText = event.answer || state.answerText;
            renderBotAnswer(messageDiv, state.answerText);
            appendSources(preferredSources(event.context, event.sources));
            applyHistoryOutcome(event);
        } else if (event.type === "error") {
            state.hasError = true;
            state.terminalType = "error";
            state.retryableError = isRetryableStreamError(event);
            state.errorCode = turnErrorCode(event);
            reconcileTurnIdFromError(event);
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

    function isRetryableStreamError(event) {
        const code = String(
            (event && (event.code || event.status)) || ""
        ).toLowerCase();
        const terminalCodes = new Set([
            "turn_id_conflict",
            "continuity_error",
            "volatile_result_lost",
            "conversation_archived",
            "knowledge_base_not_found",
            "knowledge_base_deleting",
            "knowledge_base_delete_failed",
            "validation_error"
        ]);
        return !terminalCodes.has(code);
    }

    function streamTerminalType(state) {
        if (state && state.errorCode === "volatile_result_lost") {
            return "volatile_result_lost";
        }
        if (state && state.terminalType === "error" && state.retryableError) {
            return null;
        }
        return state ? state.terminalType : null;
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
            const metadata = ctx.metadata || {};
            const sourceKey = [
                metadata.knowledge_base_id || ctx.knowledge_base_id || "",
                ctx.download_url || ctx.url || ctx.filename || ctx.title || "",
                metadata.chunk_id ?? ctx.chunk_id ?? "",
                metadata.page ?? ctx.page ?? ""
            ].join(":");
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

    function preferredSources(context, storedSources) {
        if (Array.isArray(context) && context.length > 0) {
            return context;
        }
        return storedSources;
    }

    function renderSourceCard(ctx) {
        const card = document.createElement("article");
        card.className = "source-card";

        const header = document.createElement("div");
        header.className = "source-card-header";

        const safeUrl = safeHistorySourceUrl(ctx.download_url || ctx.url);
        const link = document.createElement(safeUrl ? "a" : "strong");
        if (safeUrl) {
            link.href = safeUrl;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
        }
        link.textContent = sourceFilename(ctx);

        const meta = document.createElement("span");
        meta.textContent = sourceMeta(ctx);

        const identity = document.createElement("div");
        identity.className = "source-card-identity";
        const knowledgeBaseName = (ctx.metadata && ctx.metadata.knowledge_base_name)
            || ctx.knowledge_base_name;
        if (knowledgeBaseName) {
            const badge = document.createElement("span");
            badge.className = "source-kb-badge";
            badge.textContent = knowledgeBaseName;
            const origins = (ctx.metadata && ctx.metadata.knowledge_base_origins)
                || ctx.knowledge_base_origins
                || [];
            if (origins.length > 1) {
                badge.title = `Also found in ${origins.length - 1} other knowledge base(s)`;
            }
            identity.appendChild(badge);
        }
        identity.appendChild(link);
        header.append(identity, meta);

        const snippet = document.createElement("p");
        snippet.textContent = sourceSnippet(ctx.text || ctx.snippet || "");

        card.append(header, snippet);
        return card;
    }

    function sourceFilename(ctx) {
        if (ctx.filename || ctx.title || ctx.name) {
            return String(ctx.filename || ctx.title || ctx.name).split(/[\\/]/).pop();
        }
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
        const metadata = ctx.metadata || ctx || {};
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
        } else if (metadata.score !== undefined && Number.isFinite(Number(metadata.score))) {
            parts.push(`score ${Number(metadata.score).toFixed(2)}`);
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
            return "**Request timed out.**\n\nThe server did not confirm the turn. Retry it with the same request.";
        }
        return `**Connection error:** ${error.message}`;
    }

    function clearChat(targetKnowledgeBaseIds = knowledgeBaseIds) {
        if (busy) return;
        cancelConversationLoad();
        retryableTurnRequest = null;
        const previousConversationId = conversationId;
        conversationId = createConversationId();
        persistConversationId(conversationId);
        clearServerConversation(previousConversationId, targetKnowledgeBaseIds);
        resetLastTurnId();
        activeHistoryId = null;
        activeConversationArchived = false;
        activeConversationHistoryState = "empty";
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
                || conversationResetDescription();
        }
        if (confirmNewChatButton) {
            confirmNewChatButton.textContent = options.confirmLabel || "Start new chat";
        }
        pendingConversationReset = action;
        conversationResetTrigger = trigger || document.activeElement;
        newChatModal.hidden = false;
        if (keepConversationButton) keepConversationButton.focus();
    }

    function conversationResetDescription() {
        if (activeConversationHistoryState === "saved") {
            return "This conversation is saved and will remain available in History.";
        }
        return "This conversation has unsaved messages. If you continue, those messages may not be available in History.";
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

    // Conversation history drawer and paging state.
    const historyToggle = document.getElementById("historyToggle");
    const historyDrawer = document.getElementById("historyDrawer");
    const closeHistoryDrawer = document.getElementById("closeHistoryDrawer");
    const searchHistoryInput = document.getElementById("searchHistoryInput");
    const historyList = document.getElementById("historyList");
    const historyEmptyState = document.getElementById("historyEmptyState");
    const historyEmptyMessage = document.getElementById("historyEmptyMessage");
    const historyLoadMore = document.getElementById("historyLoadMore");
    const historyFilterButtons = [
        ...document.querySelectorAll("[data-history-status]")
    ];
    const HISTORY_PAGE_SIZE = 20;
    const HISTORY_MESSAGE_PAGE_SIZE = 50;

    let conversationHistory = [];
    let filteredHistory = [];
    let historyStatusFilter = "active";
    let historyPage = 0;
    let historyHasNext = false;
    let historyActionInProgress = false;
    let lastFocusedElement = null;
    let drawerOpenVersion = 0;
    let historyListRequestVersion = 0;
    let historyListAbortController = null;
    let historySearchVersion = 0;
    let conversationLoadVersion = 0;
    let conversationLoadAbortController = null;
    let olderMessagesAbortController = null;
    let activeMessageHistoryId = null;
    let activeMessageCursor = null;
    let loadedMessageSequences = new Set();
    let olderMessagesLoading = false;

    if (historyToggle) historyToggle.addEventListener("click", openHistoryDrawer);
    if (closeHistoryDrawer) {
        closeHistoryDrawer.addEventListener("click", () => closeHistoryDrawerHandler());
    }
    if (historyLoadMore) {
        historyLoadMore.addEventListener("click", () => {
            loadConversationHistory({reset: false});
        });
    }
    if (searchHistoryInput) {
        searchHistoryInput.addEventListener("input", () => {
            filterHistory();
            if (searchHistoryInput.value.trim() && historyHasNext) {
                loadRemainingHistoryForSearch();
            }
        });
    }
    historyFilterButtons.forEach(button => {
        button.addEventListener("click", () => {
            const nextStatus = button.dataset.historyStatus;
            if (!nextStatus || nextStatus === historyStatusFilter) return;
            historyStatusFilter = nextStatus;
            historySearchVersion += 1;
            historyFilterButtons.forEach(candidate => {
                const selected = candidate.dataset.historyStatus === historyStatusFilter;
                candidate.setAttribute("aria-pressed", selected ? "true" : "false");
            });
            if (searchHistoryInput) searchHistoryInput.value = "";
            loadConversationHistory({reset: true});
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && historyDrawer && !historyDrawer.hidden) {
            closeHistoryDrawerHandler();
        }
    });

    async function openHistoryDrawer() {
        if (!historyDrawer) return;
        const openVersion = ++drawerOpenVersion;
        if (historyDrawer.hidden) lastFocusedElement = document.activeElement;
        historyDrawer.hidden = false;
        if (historyToggle) historyToggle.setAttribute("aria-expanded", "true");
        document.addEventListener("keydown", handleDrawerKeydown);
        await loadConversationHistory({reset: true});
        if (openVersion !== drawerOpenVersion || historyDrawer.hidden) return;
        if (searchHistoryInput && searchHistoryInput.value.trim() && historyHasNext) {
            await loadRemainingHistoryForSearch();
            if (openVersion !== drawerOpenVersion || historyDrawer.hidden) return;
        }
        if (searchHistoryInput) searchHistoryInput.focus();
    }

    function closeHistoryDrawerHandler({cancelConversation = true} = {}) {
        if (!historyDrawer) return;
        drawerOpenVersion += 1;
        historyDrawer.hidden = true;
        if (historyToggle) historyToggle.setAttribute("aria-expanded", "false");
        if (historyListAbortController) historyListAbortController.abort();
        if (cancelConversation) cancelConversationLoad();
        document.removeEventListener("keydown", handleDrawerKeydown);
        if (lastFocusedElement) {
            lastFocusedElement.focus();
            lastFocusedElement = null;
        }
    }

    function handleDrawerKeydown(event) {
        if (!historyDrawer || historyDrawer.hidden || event.key !== "Tab") return;
        const focusableElements = [...historyDrawer.querySelectorAll(
            'button:not([disabled]):not([hidden]), [href], input:not([disabled]), '
            + 'select:not([disabled]), textarea:not([disabled]), '
            + '[tabindex]:not([tabindex="-1"])'
        )].filter(element => !element.hidden);
        if (focusableElements.length === 0) return;
        const firstElement = focusableElements[0];
        const lastElement = focusableElements[focusableElements.length - 1];
        if (event.shiftKey && document.activeElement === firstElement) {
            event.preventDefault();
            lastElement.focus();
        } else if (!event.shiftKey && document.activeElement === lastElement) {
            event.preventDefault();
            firstElement.focus();
        }
    }

    function renderHistoryNotice(className, message) {
        if (!historyList) return;
        const notice = document.createElement("div");
        notice.className = className;
        notice.textContent = message;
        historyList.replaceChildren(notice);
    }

    async function loadConversationHistory({reset = true} = {}) {
        if (!historyList) return false;
        if (historyListAbortController) historyListAbortController.abort();
        historyListAbortController = new AbortController();
        const requestVersion = ++historyListRequestVersion;
        const requestedStatus = historyStatusFilter;
        const targetPage = reset ? 1 : historyPage + 1;
        if (reset) {
            historyPage = 0;
            historyHasNext = false;
            conversationHistory = [];
            filteredHistory = [];
            renderHistoryNotice("history-loading", "Loading...");
        }
        updateHistoryLoadMoreButton(true);
        try {
            const query = new URLSearchParams({
                status: requestedStatus,
                page: String(targetPage),
                per_page: String(HISTORY_PAGE_SIZE)
            });
            const response = await fetch(`/api/conversations?${query}`, {
                signal: historyListAbortController.signal
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (requestVersion !== historyListRequestVersion
                || requestedStatus !== historyStatusFilter) return false;

            const received = Array.isArray(data.conversations) ? data.conversations : [];
            if (reset) {
                conversationHistory = received;
            } else {
                const knownIds = new Set(conversationHistory.map(item => item.id));
                conversationHistory.push(...received.filter(item => !knownIds.has(item.id)));
            }
            historyPage = targetPage;
            historyHasNext = Boolean(data.pagination && data.pagination.has_next);
            applyHistoryFilter();
            updateHistoryLoadMoreButton(false);
            return true;
        } catch (error) {
            if (error.name === "AbortError") return false;
            console.error("Failed to load conversation history:", error);
            if (requestVersion !== historyListRequestVersion) return false;
            if (reset) {
                conversationHistory = [];
                filteredHistory = [];
                renderHistoryNotice("history-error", "Failed to load conversations");
                if (historyEmptyState) historyEmptyState.hidden = true;
            }
            updateHistoryLoadMoreButton(false);
            return false;
        }
    }

    function filterHistory() {
        historySearchVersion += 1;
        applyHistoryFilter();
    }

    function applyHistoryFilter() {
        const query = String(searchHistoryInput && searchHistoryInput.value || "")
            .toLocaleLowerCase().trim();
        filteredHistory = query
            ? conversationHistory.filter(conv => String(conv.title || "")
                .toLocaleLowerCase().includes(query))
            : [...conversationHistory];
        renderHistoryList();
        updateHistoryLoadMoreButton(false);
    }

    async function loadRemainingHistoryForSearch() {
        const searchVersion = ++historySearchVersion;
        const requestedStatus = historyStatusFilter;
        const query = String(searchHistoryInput && searchHistoryInput.value || "").trim();
        while (query && historyHasNext
            && searchVersion === historySearchVersion
            && requestedStatus === historyStatusFilter) {
            const loaded = await loadConversationHistory({reset: false});
            if (!loaded) break;
        }
    }

    function updateHistoryLoadMoreButton(loading) {
        if (!historyLoadMore) return;
        const searching = Boolean(searchHistoryInput && searchHistoryInput.value.trim());
        historyLoadMore.hidden = searching || (!historyHasNext && !loading);
        historyLoadMore.disabled = loading;
        historyLoadMore.textContent = loading ? "Loading..." : "Load more";
    }

    function renderHistoryList() {
        if (!historyList) return;
        historyList.replaceChildren();
        if (filteredHistory.length === 0) {
            if (historyEmptyMessage) {
                const kind = historyStatusFilter === "archived" ? "archived" : "active";
                historyEmptyMessage.textContent = searchHistoryInput && searchHistoryInput.value.trim()
                    ? `No matching ${kind} conversations`
                    : `No ${kind} conversations yet`;
            }
            if (historyEmptyState) historyEmptyState.hidden = false;
            return;
        }
        if (historyEmptyState) historyEmptyState.hidden = true;
        const fragment = document.createDocumentFragment();
        filteredHistory.forEach(conv => fragment.appendChild(renderHistoryItem(conv)));
        historyList.appendChild(fragment);
    }

    function renderHistoryItem(conv) {
        const item = document.createElement("div");
        item.className = "history-item";
        item.dataset.id = String(conv.id || "");
        item.setAttribute("role", "listitem");
        item.tabIndex = 0;
        const titleText = String(conv.title || "Untitled");
        item.setAttribute("aria-label", `Conversation: ${titleText}`);

        const title = document.createElement("h4");
        title.className = "history-item-title";
        title.textContent = titleText;
        const meta = document.createElement("div");
        meta.className = "history-item-meta";
        const date = document.createElement("span");
        const timestamp = Number(conv.updated_at) * 1000;
        date.textContent = Number.isFinite(timestamp)
            ? new Date(timestamp).toLocaleString("it-IT")
            : "";
        const actions = document.createElement("div");
        actions.className = "history-item-actions";
        const isArchived = conv.status === "archived";
        actions.append(
            historyActionButton("rename-btn", "Rename", "Rename conversation", () => {
                openRenameModal(conv.id, titleText);
            }),
            historyActionButton(
                `archive-btn${isArchived ? " archived" : ""}`,
                isArchived ? "Unarchive" : "Archive",
                `${isArchived ? "Unarchive" : "Archive"} conversation`,
                () => toggleArchive(conv.id)
            ),
            historyActionButton("delete-btn", "Delete", "Delete conversation", () => {
                deleteConversation(conv.id);
            })
        );
        meta.append(date, actions);
        item.append(title, meta);
        item.addEventListener("click", event => {
            if (event.target.closest("button")) return;
            loadConversation(conv.id);
        });
        item.addEventListener("keydown", event => {
            if (event.target !== item || (event.key !== "Enter" && event.key !== " ")) return;
            event.preventDefault();
            loadConversation(conv.id);
        });
        return item;
    }

    function historyActionButton(className, textValue, label, handler) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = className;
        button.textContent = textValue;
        button.setAttribute("aria-label", label);
        button.addEventListener("click", event => {
            event.stopPropagation();
            handler();
        });
        return button;
    }
    
    function cancelConversationLoad() {
        conversationLoadVersion += 1;
        if (conversationLoadAbortController) conversationLoadAbortController.abort();
        if (olderMessagesAbortController) olderMessagesAbortController.abort();
        conversationLoadAbortController = null;
        olderMessagesAbortController = null;
        activeMessageHistoryId = null;
        activeMessageCursor = null;
        loadedMessageSequences = new Set();
        olderMessagesLoading = false;
    }

    async function loadConversation(id) {
        if (busy) {
            showHistoryStatus("Please wait for the current response to complete", 3000);
            return;
        }
        cancelConversationLoad();
        conversationLoadAbortController = new AbortController();
        const signal = conversationLoadAbortController.signal;
        const loadVersion = conversationLoadVersion;
        const historyId = String(id || "");
        const previousConversationId = conversationId;
        const previousKnowledgeBaseIds = [...knowledgeBaseIds];
        showHistoryStatus("Loading conversation...", 10000);
        try {
            const encodedId = encodeURIComponent(historyId);
            const [recordResponse, messagesResponse] = await Promise.all([
                fetch(`/api/conversations/${encodedId}`, {signal}),
                fetch(
                    `/api/conversations/${encodedId}/messages?limit=${HISTORY_MESSAGE_PAGE_SIZE}`,
                    {signal}
                )
            ]);
            if (!recordResponse.ok || !messagesResponse.ok) {
                throw new Error(`HTTP ${recordResponse.status}/${messagesResponse.status}`);
            }
            const [record, messagePage] = await Promise.all([
                recordResponse.json(),
                messagesResponse.json()
            ]);
            if (loadVersion !== conversationLoadVersion) return;

            // Clear the previous warm context before adopting the restored one.
            // Waiting prevents a late DELETE from clearing the newly hydrated chat.
            if (previousConversationId
                && previousConversationId !== record.client_conversation_id) {
                await clearServerConversation(
                    previousConversationId,
                    previousKnowledgeBaseIds
                );
            }
            if (loadVersion !== conversationLoadVersion) return;

            const warnings = await restoreConversationConfiguration(record, signal);
            if (loadVersion !== conversationLoadVersion) return;

            conversationId = record.client_conversation_id || conversationId;
            persistConversationId(conversationId);
            retryableTurnRequest = null;
            currentTurnId = null;
            activeHistoryId = historyId;
            activeConversationArchived = record.status === "archived";
            activeConversationHistoryState = "saved";
            if (record.last_turn_id) {
                lastTurnId = record.last_turn_id;
                persistLastTurnId(lastTurnId);
            } else {
                resetLastTurnId();
            }

            renderConversationTranscript(
                Array.isArray(messagePage.messages) ? messagePage.messages : [],
                messagePage.next_cursor,
                historyId
            );
            closeHistoryDrawerHandler({cancelConversation: false});
            const archivedWarning = activeConversationArchived
                ? " This conversation is archived; unarchive it to continue."
                : "";
            const configWarning = warnings.length > 0
                ? ` ${warnings.join(" ")}`
                : "";
            showHistoryStatus(
                `Conversation loaded.${archivedWarning}${configWarning}`,
                archivedWarning || configWarning ? 8000 : 3000
            );
        } catch (error) {
            if (error.name === "AbortError") return;
            console.error("Failed to load conversation:", error);
            showHistoryStatus("Failed to load conversation", 5000);
        }
    }

    async function restoreConversationConfiguration(record, signal) {
        const warnings = [];
        restoreConversationModel(record, warnings);
        restoreConversationKnowledgeBases(record, warnings);
        restoreConversationPrompt(record, warnings);

        const agentId = String(record.agent_id || "").trim();
        if (!agentId) {
            switchToCustomChat();
            updateChatStatus();
            return warnings;
        }
        try {
            const response = await fetch(`/api/agents/${encodeURIComponent(agentId)}`, {signal});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const agent = await response.json();
            if (!agent.available) throw new Error("Agent unavailable");
            const existingIndex = agentsCatalog.findIndex(item => item.id === agent.id);
            if (existingIndex === -1) agentsCatalog.push(agent);
            else agentsCatalog[existingIndex] = agent;
            ensureAgentOption(agent);
            selectedAgentId = agent.id;
            agentSelectionBlocked = false;
            if (agentSelect) agentSelect.value = agent.id;
            persistSelectedAgent(agent.id);
            applyAgentConfig(agent);
            setAgentActive(true);
        } catch (error) {
            if (error.name === "AbortError") throw error;
            switchToCustomChat();
            warnings.push("Its saved Agent is no longer available; custom chat was restored.");
        }
        updateChatStatus();
        return warnings;
    }

    function restoreConversationModel(record, warnings) {
        const providerId = String(record.provider_id || "");
        const modelId = String(record.model_id || "");
        if (!providerId && !modelId) return;
        const option = [...modelSelect.options].find(candidate =>
            candidate.dataset.provider === providerId
            && candidate.dataset.model === modelId
        );
        if (option) {
            option.selected = true;
        } else {
            const fallback = [...modelSelect.options].find(
                candidate => candidate.dataset.default === "true"
            ) || modelSelect.options[0];
            if (fallback) fallback.selected = true;
            warnings.push("Its saved model is no longer available; the default was selected.");
        }
    }

    function restoreConversationKnowledgeBases(record, warnings) {
        const rawIds = Array.isArray(record.knowledge_base_ids)
            ? record.knowledge_base_ids
            : [];
        const savedIds = rawIds.map(item => {
            if (typeof item === "string") return item;
            if (!item || typeof item !== "object") return "";
            if (item.is_selected === false) return "";
            return item.knowledge_base_id || item.id || "";
        }).filter(Boolean);
        const availableIds = new Set(knowledgeBaseCatalog.map(item => item.id));
        const restoredIds = normalizeKnowledgeBaseIds(savedIds)
            .filter(id => availableIds.has(id))
            .slice(0, maxQueryKnowledgeBases);
        const fallbackId = availableIds.has("default")
            ? "default"
            : (knowledgeBaseCatalog[0] && knowledgeBaseCatalog[0].id);
        const nextIds = restoredIds.length > 0
            ? restoredIds
            : (fallbackId ? [fallbackId] : []);
        if (nextIds.length > 0) {
            knowledgeBaseIds = nextIds;
            draftKnowledgeBaseIds = [...nextIds];
            persistKnowledgeBaseIds(nextIds);
            renderKnowledgeBaseSelection();
            renderKnowledgeBaseOptions();
        }
        if (savedIds.length > 0 && restoredIds.length !== savedIds.length) {
            warnings.push("One or more saved knowledge bases are no longer available.");
        }
    }

    function restoreConversationPrompt(record, warnings) {
        if (!promptSelect) return;
        const promptRef = record.prompt_ref && typeof record.prompt_ref === "object"
            ? record.prompt_ref
            : {};
        const promptId = String(promptRef.id || "");
        const promptScope = String(promptRef.scope || "");
        const target = promptId && promptScope ? `${promptScope}::${promptId}` : "";
        const option = [...promptSelect.options].find(candidate => candidate.value === target);
        if (option) {
            option.selected = true;
            systemPromptId = promptId;
            systemPromptScope = promptScope;
            persistPromptRef(systemPromptScope, systemPromptId);
        } else {
            promptSelect.value = "";
            systemPromptId = "";
            systemPromptScope = "";
            persistPromptRef("", "");
            if (promptId) warnings.push("Its saved system prompt is no longer available.");
        }
    }

    function ensureAgentOption(agent) {
        if (!agentSelect) return;
        let option = [...agentSelect.options].find(candidate => candidate.value === agent.id);
        if (!option) {
            option = new Option(agent.name || agent.id, agent.id);
            agentSelect.appendChild(option);
        }
        option.disabled = false;
        option.textContent = agent.name || agent.id;
    }

    function renderConversationTranscript(messages, nextCursor, historyId) {
        chatbox.replaceChildren();
        clearUploadedFiles();
        if (emptyState) emptyState.hidden = true;
        activeMessageHistoryId = historyId;
        activeMessageCursor = normalizeMessageCursor(nextCursor);
        loadedMessageSequences = new Set();
        const fragment = document.createDocumentFragment();
        messages.forEach(message => {
            const sequence = Number(message.sequence);
            if (Number.isFinite(sequence)) loadedMessageSequences.add(sequence);
            persistedMessageNodes(message).forEach(node => fragment.appendChild(node));
        });
        chatbox.appendChild(fragment);
        renderOlderMessagesControl();
        chatbox.scrollTop = chatbox.scrollHeight;
    }

    function persistedMessageNodes(message) {
        const nodes = [];
        if (!message || !["user", "assistant"].includes(message.role)) return nodes;
        const messageDiv = document.createElement("div");
        messageDiv.classList.add(
            "message",
            message.role === "user" ? "user-message" : "bot-message"
        );
        if (message.role === "assistant") {
            const responsePayload = message.metadata
                && message.metadata.response_payload
                && typeof message.metadata.response_payload === "object"
                ? message.metadata.response_payload
                : null;
            if (message.message_type === "code_interpreter" && responsePayload) {
                renderCodeInterpreterPayload(
                    messageDiv,
                    responsePayload.code || "",
                    responsePayload.result || {},
                    ""
                );
            } else {
                messageDiv.innerHTML = renderSafeMarkdown(message.content || "");
                highlightCodeBlocks(messageDiv);
            }
        } else {
            messageDiv.textContent = String(message.content || "");
        }
        nodes.push(messageDiv);
        if (message.role === "assistant" && Array.isArray(message.sources)
            && message.sources.length > 0) {
            nodes.push(renderPersistedSources(message.sources));
        }
        return nodes;
    }

    function renderPersistedSources(sources) {
        const details = document.createElement("details");
        details.className = "context-sources";
        const summary = document.createElement("summary");
        summary.textContent = `Sources (${sources.length})`;
        const list = document.createElement("div");
        list.className = "source-card-list";
        sources.forEach(source => list.appendChild(renderPersistedSourceCard(source)));
        details.append(summary, list);
        return details;
    }

    function renderPersistedSourceCard(source) {
        const value = source && typeof source === "object" ? source : {};
        const metadata = value.metadata && typeof value.metadata === "object"
            ? value.metadata
            : value;
        const card = document.createElement("article");
        card.className = "source-card";
        const header = document.createElement("div");
        header.className = "source-card-header";
        const identity = document.createElement("div");
        identity.className = "source-card-identity";
        const kbName = metadata.knowledge_base_name;
        if (kbName) {
            const badge = document.createElement("span");
            badge.className = "source-kb-badge";
            badge.textContent = String(kbName);
            identity.appendChild(badge);
        }
        const filename = String(
            value.filename || value.title || value.name || metadata.source || "Document"
        ).split(/[\\/]/).pop();
        const safeUrl = safeHistorySourceUrl(value.download_url || value.url);
        const sourceLabel = document.createElement(safeUrl ? "a" : "strong");
        sourceLabel.textContent = filename || "Document";
        if (safeUrl) {
            sourceLabel.href = safeUrl;
            sourceLabel.target = "_blank";
            sourceLabel.rel = "noopener noreferrer";
        }
        identity.appendChild(sourceLabel);
        const meta = document.createElement("span");
        const metaParts = [];
        if (metadata.page !== undefined) metaParts.push(`p. ${metadata.page}`);
        if (metadata.chunk_id !== undefined) metaParts.push(`chunk ${metadata.chunk_id}`);
        if (metadata.score !== undefined) {
            const score = Number(metadata.score);
            if (Number.isFinite(score)) metaParts.push(`score ${score.toFixed(2)}`);
        }
        meta.textContent = metaParts.join(" | ") || String(value.source_type || "saved source");
        const snippet = document.createElement("p");
        snippet.textContent = sourceSnippet(value.snippet || value.text || "");
        header.append(identity, meta);
        card.append(header, snippet);
        return card;
    }

    function safeHistorySourceUrl(rawUrl) {
        if (!rawUrl) return "";
        try {
            const url = new URL(String(rawUrl), window.location.origin);
            return ["http:", "https:"].includes(url.protocol) ? url.href : "";
        } catch (error) {
            return "";
        }
    }

    function renderOlderMessagesControl() {
        const existing = chatbox.querySelector(".history-load-older");
        if (existing) existing.remove();
        if (activeMessageCursor === null) return;
        const wrapper = document.createElement("div");
        wrapper.className = "history-load-older";
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = olderMessagesLoading ? "Loading..." : "Load older messages";
        button.disabled = olderMessagesLoading;
        button.addEventListener("click", loadOlderMessages);
        wrapper.appendChild(button);
        chatbox.prepend(wrapper);
    }

    function normalizeMessageCursor(value) {
        if (value === null || value === undefined) return null;
        const cursor = Number(value);
        return Number.isInteger(cursor) && cursor >= 0 ? cursor : null;
    }

    async function loadOlderMessages() {
        if (olderMessagesLoading || activeMessageCursor === null
            || !activeMessageHistoryId) return;
        olderMessagesLoading = true;
        renderOlderMessagesControl();
        if (olderMessagesAbortController) olderMessagesAbortController.abort();
        olderMessagesAbortController = new AbortController();
        const signal = olderMessagesAbortController.signal;
        const loadVersion = conversationLoadVersion;
        const historyId = activeMessageHistoryId;
        const cursor = activeMessageCursor;
        try {
            const query = new URLSearchParams({
                limit: String(HISTORY_MESSAGE_PAGE_SIZE),
                before_sequence: String(cursor)
            });
            const response = await fetch(
                `/api/conversations/${encodeURIComponent(historyId)}/messages?${query}`,
                {signal}
            );
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (loadVersion !== conversationLoadVersion
                || historyId !== activeMessageHistoryId) return;
            const previousHeight = chatbox.scrollHeight;
            const previousTop = chatbox.scrollTop;
            const fragment = document.createDocumentFragment();
            (Array.isArray(data.messages) ? data.messages : []).forEach(message => {
                const sequence = Number(message.sequence);
                if (Number.isFinite(sequence) && loadedMessageSequences.has(sequence)) return;
                if (Number.isFinite(sequence)) loadedMessageSequences.add(sequence);
                persistedMessageNodes(message).forEach(node => fragment.appendChild(node));
            });
            const control = chatbox.querySelector(".history-load-older");
            if (control) control.after(fragment);
            else chatbox.prepend(fragment);
            activeMessageCursor = normalizeMessageCursor(data.next_cursor);
            chatbox.scrollTop = previousTop + (chatbox.scrollHeight - previousHeight);
        } catch (error) {
            if (error.name !== "AbortError") {
                console.error("Failed to load older messages:", error);
                showHistoryStatus("Failed to load older messages", 5000);
            }
        } finally {
            if (loadVersion === conversationLoadVersion
                && historyId === activeMessageHistoryId) {
                olderMessagesLoading = false;
                renderOlderMessagesControl();
            }
        }
    }
    
    async function toggleArchive(id) {
        if (busy || historyActionInProgress) return;
        historyActionInProgress = true;
        try {
            const conv = conversationHistory.find(c => c.id === id);
            if (!conv) return;

            const isArchived = conv.status === "archived";
            const isCurrent = id === activeHistoryId
                || conv.client_conversation_id === conversationId;
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ archived: !isArchived })
            });

            if (!response.ok) {
                showHistoryStatus("Failed to update conversation", 5000);
                return;
            }

            if (isCurrent) {
                activeConversationArchived = !isArchived;
            }
            await loadConversationHistory({reset: true});
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
        if (busy || historyActionInProgress) return;
        if (!confirm("Delete this conversation? This cannot be undone.")) return;
        historyActionInProgress = true;
        try {
            const conv = conversationHistory.find(c => c.id === id) || {};
            const isCurrent = id === activeHistoryId
                || conv.client_conversation_id === conversationId;
            const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
                method: "DELETE"
            });

            if (!response.ok) {
                showHistoryStatus("Failed to delete conversation", 5000);
                return;
            }

            if (isCurrent) clearChat();
            await loadConversationHistory({reset: true});
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

            await loadConversationHistory({reset: true});
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
