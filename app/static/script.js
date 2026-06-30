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
    const chatStatus = document.getElementById("chatStatus");
    const clearChatButton = document.getElementById("clearChatButton");
    const streamStatus = document.getElementById("streamStatus");
    const emptyState = document.getElementById("emptyState");
    const readinessStatus = document.getElementById("readinessStatus");
    const readinessDocs = document.getElementById("readinessDocs");
    const readinessIndex = document.getElementById("readinessIndex");
    const readinessModel = document.getElementById("readinessModel");
    const promptButtons = document.querySelectorAll("[data-prompt]");
    const uploadAudioButton = document.getElementById("uploadAudioButton");
    const uploadOcrButton = document.getElementById("uploadOcrButton");
    const ocrFileInput = document.getElementById("ocrFileInput");

    if (!chatbox || !userInput || !sendButton || !modelSelect) {
        return;
    }

    const promptStorageKey = "ragSystemPromptId";
    let systemPromptId = loadSystemPromptId();

    let healthState = null;
    let busy = false;
    const conversationStorageKey = "ragConversationId";
    let conversationId = loadOrCreateConversationId();

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
                        uploadAudioButton.title = "Record audio for transcription";
                        await submitRecording(audioChunks, recorder.mimeType);
                    };
                    recorder.onerror = (event) => {
                        displayRecordingError(event.error || event);
                    };
                    recorder.start();
                    isRecording = true;
                    uploadAudioButton.classList.add("recording");
                    uploadAudioButton.title = "Stop recording";
                } catch (err) {
                    if (stream) {
                        stream.getTracks().forEach(track => track.stop());
                    }
                    isRecording = false;
                    uploadAudioButton.classList.remove("recording");
                    uploadAudioButton.title = "Record audio for transcription";
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

    async function submitRecording(chunks, mimeType) {
        if (chunks.length === 0) {
            uploadAudioButton.disabled = false;
            uploadAudioButton.title = "Record audio for transcription";
            return;
        }

        const recordingType = mimeType || chunks[0].type || "audio/webm";
        const blob = new Blob(chunks, { type: recordingType });
        const filename = `recording.${recordingExtension(recordingType)}`;
        const originalBtnText = uploadAudioButton.title;
        uploadAudioButton.title = "Transcribing...";
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
            uploadAudioButton.title = originalBtnText || "Record audio for transcription";
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
            updateChatStatus("Models not available");
        }
    }

    async function loadHealth() {
        try {
            const response = await fetch("/health");
            healthState = await response.json();
        } catch (error) {
            healthState = {status: "unreachable"};
        }
        updateChatStatus();
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

        try {
            const selected = modelSelect.selectedOptions[0];
            const body = {
                query,
                model: selected ? selected.dataset.model : undefined,
                provider: selected ? selected.dataset.provider : undefined,
                conversation_id: conversationId,
                stream: true,
                stream_format: "ndjson",
                system_prompt_id: systemPromptId || undefined
            };

            const response = await fetch("/ask", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(body)
            });

            if (!response.ok) {
                const data = await readErrorPayload(response);
                appendMessage(formatError(data, response.statusText), "bot-message");
            } else {
                const messageDiv = appendBotMessage("");
                await renderStreamingResponse(response, messageDiv);
            }
        } catch (error) {
            appendMessage(`**Connection error:** ${error.message}`, "bot-message");
        } finally {
            setBusy(false);
            userInput.focus();
        }
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

    async function readErrorPayload(response) {
        try {
            return await response.json();
        } catch (error) {
            return {error: response.statusText};
        }
    }

    async function renderStreamingResponse(response, messageDiv) {
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

            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            parseNdjsonLines(lines.join("\n"), (event) => handleStreamEvent(event, state, messageDiv));
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

    function updateChatStatus(overrideText) {
        if (!chatStatus) return;
        if (!busy) {
            sendButton.disabled = !modelSelect.value;
        }
        if (overrideText) {
            chatStatus.textContent = overrideText;
            return;
        }

        const selected = modelSelect.selectedOptions[0];
        const modelName = selected && selected.value ? selected.textContent : "model not selected";
        const parts = [`Model: ${modelName}`];

        if (healthState) {
            const status = healthState.status || "n/a";
            const docs = healthState.documents_count;
            const docsLabel = Number.isFinite(Number(docs)) ? `${docs} chunks` : "index n/a";
            parts.push(`Status: ${status}`);
            parts.push(`Knowledge base: ${docsLabel}`);
        }

        chatStatus.textContent = parts.join(" | ");
        updateReadinessCards();
    }

    function updateReadinessCards() {
        const selected = modelSelect.selectedOptions[0];
        const modelName = selected && selected.value ? selected.textContent : "n/a";
        if (readinessModel) {
            readinessModel.textContent = modelName;
        }
        if (!healthState) {
            return;
        }

        const docs = Number(healthState.documents_count || 0);
        const files = Number(healthState.indexed_files_count || 0);
        const stale = Number(healthState.stale_index_files_count || 0);

        if (readinessStatus) {
            if (healthState.system_ready) {
                readinessStatus.textContent = "Ready";
                readinessStatus.dataset.state = "ready";
            } else if (healthState.status === "healthy") {
                readinessStatus.textContent = "To complete";
                readinessStatus.dataset.state = "warning";
            } else {
                readinessStatus.textContent = "Warning";
                readinessStatus.dataset.state = "error";
            }
        }
        if (readinessDocs) {
            readinessDocs.textContent = `${files} files | ${docs} chunks`;
        }
        if (readinessIndex) {
            readinessIndex.textContent = healthState.needs_rebuild ? `${stale} files to rebuild` : "Aligned";
        }
    }

    function formatError(data, fallback) {
        const status = data && data.status ? `\n\nStatus: \`${data.status}\`` : "";
        const retry = data && data.retry_after ? ` Retry in ${data.retry_after} seconds.` : "";
        return `**Unable to complete the request.**\n\n${(data && data.error) || fallback || "Unknown error."}${retry}${status}`;
    }

    function clearChat() {
        const previousConversationId = conversationId;
        conversationId = createConversationId();
        persistConversationId(conversationId);
        clearServerConversation(previousConversationId);
        chatbox.replaceChildren();
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
        uploadOcrButton.title = "Extracting text...";
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
            uploadOcrButton.title = originalBtnText || "Extract text from image or PDF";
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

    loadModels();
    loadPrompts();
    loadHealth();
    resizeInput();
});
