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
    const promptSelect = document.getElementById("promptSelect");
    const clearChatButton = document.getElementById("clearChatButton");
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
    let systemPromptId = loadSystemPromptId();
    let codeInterpreterEnabled = false;
    const uploadedFiles = [];
    const askIdleTimeoutMs = 120000;

    let busy = false;
    const conversationStorageKey = "ragConversationId";
    let conversationId = loadOrCreateConversationId();

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
    modelSelect.addEventListener("change", () => updateChatStatus());
    if (promptSelect) {
        promptSelect.addEventListener("change", () => {
            const selValue = promptSelect.value;
            systemPromptId = selValue || "";
            persistSystemPromptId(systemPromptId);
        });
    }
    if (clearChatButton) {
        clearChatButton.addEventListener("click", clearChat);
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
                msgDiv.innerHTML = window.marked
                    ? DOMPurify.sanitize(marked.parse(formatError(data, "Transcription failed")))
                    : escapeHtml("Transcription failed: " + (data.error || response.statusText));
            } else {
                const data = await response.json();
                const transcript = data.transcript || "";
                if (transcript) {
                    msgDiv.innerHTML = window.marked
                        ? DOMPurify.sanitize(marked.parse("**Transcription**:\n\n" + escapeHtml(transcript)))
                        : escapeHtml("Transcription:\n\n" + transcript);
                    userInput.value = transcript;
                    resizeInput();
                } else {
                    msgDiv.innerHTML = "**Transcription result:** empty (recording may contain no speech)";
                }
            }
            highlightCodeBlocks(msgDiv);
        } catch (error) {
            msgDiv.innerHTML = window.marked
                ? DOMPurify.sanitize(marked.parse("Transcription failed: " + error.message))
                : escapeHtml("Transcription failed: " + error.message);
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
                const ciBody = {
                    query,
                    model: selected ? selected.dataset.model : undefined,
                    provider: selected ? selected.dataset.provider : undefined,
                    conversation_id: conversationId,
                    stream: true,
                    stream_format: "ndjson",
                    system_prompt_id: systemPromptId || undefined,
                    use_code_interpreter: true,
                    attached_files: uploadedFiles.map(f => ({
                        id: f.id,
                        file_id: f.id,
                        name: f.name,
                        type: f.type
                    }))
                };
                const response = await postAsk(ciBody, askTimeout);
                if (!response.ok) {
                    const data = await readErrorPayload(response);
                    appendMessage(formatError(data, response.statusText), "bot-message");
                } else {
                    const messageDiv = appendBotMessage("Preparing analysis...");
                    await renderCodeInterpreterStream(response, messageDiv, askTimeout);
                }
                // Clear uploaded files after sending
                uploadedFiles.length = 0;
                renderAttachedFiles();
                return;
            }

            const body = {
                query,
                model: selected ? selected.dataset.model : undefined,
                provider: selected ? selected.dataset.provider : undefined,
                conversation_id: conversationId,
                stream: true,
                stream_format: "ndjson",
                system_prompt_id: systemPromptId || undefined,
                use_code_interpreter: false
            };
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
                appendMessage(formatError(data, response.statusText), "bot-message");
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

    function renderCodeInterpreterResult(data) {
        const messageDiv = appendBotMessage("");
        const result = data.result || {};
        renderCodeInterpreterPayload(messageDiv, data.code || "", result);
        if (data.context) {
            appendSources(data.context);
        }
    }

    async function renderCodeInterpreterStream(response, messageDiv, timeout) {
        const state = {code: "", result: null, hasError: false, status: "Preparing analysis..."};

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
            } else if (event.type === "error") {
                state.hasError = true;
                renderBotAnswer(messageDiv, formatError(event, "Code interpreter interrupted"));
            }
        };

        if (!response.body || !response.body.getReader) {
            const text = await response.text();
            parseNdjsonLines(text, onEvent);
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
                return;
            }
        }
        buffer += decoder.decode();
        parseNdjsonLines(buffer, onEvent);
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

        messageDiv.innerHTML = window.marked
            ? DOMPurify.sanitize(marked.parse(content))
            : escapeHtml(content);
        highlightCodeBlocks(messageDiv);
    }

    function setBusy(isBusy) {
        busy = isBusy;
        sendButton.disabled = isBusy || !modelSelect.value;
        userInput.disabled = isBusy;
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

    async function renderStreamingResponse(response, messageDiv, timeout) {
        const state = {answerText: "", hasError: false};

        if (!response.body || !response.body.getReader) {
            const text = await response.text();
            parseNdjsonLines(text, (event) => handleStreamEvent(event, state, messageDiv));
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
                return;
            }
        }

        buffer += decoder.decode();
        parseNdjsonLines(buffer, (event) => handleStreamEvent(event, state, messageDiv));
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
        } else if (event.type === "error") {
            state.hasError = true;
            renderBotAnswer(messageDiv, formatError(event, "Streaming interrupted"));
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
        const html = window.marked ? marked.parse(answerText || "") : escapeHtml(answerText || "");
        const sanitizedHtml = window.DOMPurify ? DOMPurify.sanitize(html) : html;

        messageDiv.innerHTML = sanitizedHtml;
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
            if (seenUrls.has(ctx.download_url)) return false;
            seenUrls.add(ctx.download_url);
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

        header.append(link, meta);

        const snippet = document.createElement("p");
        snippet.textContent = sourceSnippet(ctx.text || "");

        card.append(header, snippet);
        return card;
    }

    function sourceFilename(ctx) {
        if (ctx.download_url) {
            return decodeURIComponent(ctx.download_url.split("/").pop() || "Document");
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

    function updateChatStatus() {
        if (!busy) {
            sendButton.disabled = !modelSelect.value;
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

    function clearChat() {
        const previousConversationId = conversationId;
        conversationId = createConversationId();
        persistConversationId(conversationId);
        clearServerConversation(previousConversationId);
        chatbox.replaceChildren();
        clearUploadedFiles();
        if (emptyState) {
            emptyState.hidden = false;
            chatbox.appendChild(emptyState);
        }
        userInput.focus();
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

    async function clearServerConversation(value) {
        if (!value) return;
        try {
            await fetch(`/conversation/${encodeURIComponent(value)}`, {method: "DELETE"});
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
                msgDiv.innerHTML = window.marked
                    ? DOMPurify.sanitize(marked.parse(formatError(data, "OCR failed")))
                    : escapeHtml(`OCR failed: ${data.error || response.statusText}`);
            } else {
                const data = await response.json();
                const text = data.text || "";
                if (text) {
                    const method = data.ocr_used ? "OCR" : "PDF text parser";
                    msgDiv.innerHTML = window.marked
                        ? DOMPurify.sanitize(marked.parse(`**Extracted text** (${data.filename || "document"}, ${method}):\n\n${escapeHtml(text)}`))
                        : escapeHtml(`Extracted text:\n\n${text}`);
                    userInput.value = text;
                    resizeInput();
                } else {
                    msgDiv.innerHTML = "**Extraction result:** empty";
                }
            }
            highlightCodeBlocks(msgDiv);
        } catch (error) {
            msgDiv.innerHTML = window.marked
                ? DOMPurify.sanitize(marked.parse(`OCR failed: ${error.message}`))
                : escapeHtml(`OCR failed: ${error.message}`);
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
                const opt = new Option(`[personal] ${p.name}`, p.id);
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
                const opt = new Option(`[shared] ${p.name}`, p.id);
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

            const saved = loadSystemPromptId();
            if (saved) {
                for (const opt of promptSelect.options) {
                    if (opt.value === saved) {
                        opt.selected = true;
                        systemPromptId = saved;
                        break;
                    }
                }
            }
        } catch (error) {
            console.warn("Prompts not available:", error);
        }
    }

    function loadSystemPromptId() {
        try {
            return window.sessionStorage
                ? (sessionStorage.getItem(promptStorageKey) || "")
                : "";
        } catch (e) {
            return "";
        }
    }

    function persistSystemPromptId(value) {
        try {
            if (window.sessionStorage) {
                sessionStorage.setItem(promptStorageKey, value);
            }
        } catch (e) {/* noop */}
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

    loadModels();
    loadPrompts();
    resizeInput();
});
