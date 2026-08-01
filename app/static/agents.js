document.addEventListener("DOMContentLoaded", () => {
    const list = document.getElementById("agentList");
    const notice = document.getElementById("agentNotice");
    const createForm = document.getElementById("createAgentForm");
    const showCreate = document.getElementById("showCreateAgentButton");
    const cancelCreate = document.getElementById("cancelCreateAgentButton");

    let limits = {max_chat_agents: 20, max_query_knowledge_bases: 5};
    let modelsData = [];
    let knowledgeBases = [];
    let personalPrompts = [];
    let sharedPrompts = [];
    let agentsData = [];

    showCreate.addEventListener("click", () => {
        createForm.hidden = false;
        createForm.elements.name.focus();
    });
    cancelCreate.addEventListener("click", () => {
        createForm.reset();
        createForm.hidden = true;
    });
    createForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        try {
            const payload = collectFormPayload(createForm);
            await jsonRequest("/api/agents", {
                method: "POST",
                body: JSON.stringify(payload)
            });
            createForm.reset();
            createForm.hidden = true;
            showNotice("Agent created.", "success");
            await loadAgents();
        } catch (error) {
            showNotice(error.message, "error");
        }
    });

    async function loadAgents(renderNow = true) {
        try {
            const payload = await jsonRequest("/api/agents");
            limits = payload.limits || limits;
            agentsData = payload.agents || [];
            if (renderNow) render(agentsData);
        } catch (error) {
            list.innerHTML = `<p class="notice error">${escapeHtml(error.message)}</p>`;
            throw error;
        }
    }

    function render(items) {
        list.replaceChildren();
        if (items.length === 0) {
            const empty = document.createElement("p");
            empty.className = "muted";
            empty.textContent = "No chat agents yet. Create one to pre-configure a provider, model, knowledge bases, and system prompt.";
            list.appendChild(empty);
            return;
        }
        items.forEach(item => list.appendChild(renderCard(item, items.length)));
    }

    function renderCard(item, total) {
        const card = document.createElement("article");
        card.className = "panel knowledge-base-card";
        const availabilityBadge = item.available
            ? '<span class="status-pill indexed">Available</span>'
            : '<span class="status-pill empty">Unavailable</span>';
        const issuesHtml = (item.issues && item.issues.length > 0)
            ? `<ul class="muted">${item.issues.map(i => `<li>${escapeHtml(i.message)}</li>`).join("")}</ul>`
            : "";
        const kbBadges = (item.knowledge_base_ids || [])
            .map(id => `<span class="badge">${escapeHtml(id)}</span>`)
            .join(" ");
        const promptLabel = item.prompt_ref && item.prompt_ref.id
            ? `[${escapeHtml(item.prompt_ref.scope)}] ${escapeHtml(item.prompt_ref.id)}`
            : '<span class="muted">None</span>';
        card.innerHTML = `
            <div class="panel-heading">
                <div>
                    <h3>${escapeHtml(item.name)} ${availabilityBadge}</h3>
                    <p>${escapeHtml(item.description || "No description")}</p>
                </div>
                <span class="count-badge">${total} / ${limits.max_chat_agents}</span>
            </div>
            <dl class="kb-stats">
                <div><dt>Provider</dt><dd>${escapeHtml(item.provider_id || "—")}</dd></div>
                <div><dt>Model</dt><dd>${escapeHtml(item.model_id || "—")}</dd></div>
                <div><dt>Knowledge bases</dt><dd>${kbBadges || '<span class="muted">none</span>'}</dd></div>
                <div><dt>System prompt</dt><dd>${promptLabel}</dd></div>
            </dl>
            ${issuesHtml}
            <div class="table-actions">
                <button type="button" data-action="start">Start chat</button>
                <button type="button" class="secondary" data-action="edit">Edit</button>
                <button type="button" class="danger" data-action="delete">Delete</button>
            </div>
            <form class="form-grid compact" data-editor hidden>
                <label>Name<input name="name" value="${escapeAttribute(item.name)}" maxlength="120" required></label>
                <label>Description<textarea name="description" maxlength="500" rows="3">${escapeHtml(item.description || "")}</textarea></label>
                <label>Provider<select name="provider_id" data-provider-select required></select></label>
                <label>Model<select name="model_id" data-model-select required></select></label>
                <fieldset class="scope-field">
                    <legend>Knowledge bases</legend>
                    <div class="scope-options" data-kb-list></div>
                </fieldset>
                <label>System prompt<select name="prompt_ref" data-prompt-select required></select></label>
                <div class="modal-actions">
                    <button type="submit">Save</button>
                    <button type="button" class="secondary" data-action="cancel-edit">Cancel</button>
                </div>
            </form>
        `;
        const editor = card.querySelector("[data-editor]");
        const providerSelect = editor.querySelector("[data-provider-select]");
        const modelSelect = editor.querySelector("[data-model-select]");
        const kbList = editor.querySelector("[data-kb-list]");
        const promptSelect = editor.querySelector("[data-prompt-select]");
        populateProviderSelect(providerSelect, item.provider_id);
        populateModelSelect(modelSelect, providerSelect.value, item.model_id);
        providerSelect.addEventListener("change", () => {
            populateModelSelect(modelSelect, providerSelect.value, null);
        });
        populateKbCheckboxes(kbList, item.knowledge_base_ids || []);
        populatePromptSelect(promptSelect, item.prompt_ref);
        card.querySelector('[data-action="edit"]').addEventListener("click", () => {
            editor.hidden = false;
            editor.elements.name.focus();
        });
        const startButton = card.querySelector('[data-action="start"]');
        if (!item.available) {
            startButton.disabled = true;
        }
        startButton.addEventListener("click", () => {
            window.location.href = `/?agent=${encodeURIComponent(item.id)}`;
        });
        card.querySelector('[data-action="cancel-edit"]').addEventListener("click", () => {
            editor.hidden = true;
        });
        editor.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                const payload = collectFormPayload(editor);
                await jsonRequest(`/api/agents/${encodeURIComponent(item.id)}`, {
                    method: "PATCH",
                    body: JSON.stringify(payload)
                });
                showNotice("Agent updated.", "success");
                await loadAgents();
            } catch (error) {
                showNotice(error.message, "error");
            }
        });
        card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
            if (!window.confirm(`Delete agent "${item.name}"?`)) return;
            try {
                await jsonRequest(`/api/agents/${encodeURIComponent(item.id)}`, {
                    method: "DELETE"
                });
                showNotice("Agent deleted.", "success");
                await loadAgents();
            } catch (error) {
                showNotice(error.message, "error");
            }
        });
        return card;
    }

    function collectFormPayload(form) {
        const providerId = form.elements.provider_id.value;
        const modelId = form.elements.model_id.value;
        const knowledgeBaseIds = Array.from(
            form.querySelectorAll("[data-kb-list] input:checked, #createAgentKnowledgeBases input:checked")
        ).map(cb => cb.value);
        const promptValue = form.elements.prompt_ref.value;
        if (!promptValue) {
            throw new Error("Select one system prompt.");
        }
        const [scope, id] = promptValue.split("::", 2);
        const promptRef = {id, scope};
        if (knowledgeBaseIds.length === 0) {
            throw new Error("Select at least one knowledge base.");
        }
        if (knowledgeBaseIds.length > limits.max_query_knowledge_bases) {
            throw new Error(`Select at most ${limits.max_query_knowledge_bases} knowledge bases.`);
        }
        return {
            name: form.elements.name.value,
            description: form.elements.description.value,
            provider_id: providerId,
            model_id: modelId,
            knowledge_base_ids: knowledgeBaseIds,
            prompt_ref: promptRef
        };
    }

    function populateProviderSelect(select, selectedValue) {
        select.innerHTML = "";
        const providers = {};
        modelsData.forEach(m => {
            if (!providers[m.provider]) {
                providers[m.provider] = m.provider_name || m.provider;
            }
        });
        Object.keys(providers).sort().forEach(pid => {
            const opt = new Option(providers[pid], pid);
            if (pid === selectedValue) opt.selected = true;
            select.appendChild(opt);
        });
        if (selectedValue && !Array.from(select.options).some(opt => opt.value === selectedValue)) {
            select.appendChild(new Option(`${selectedValue} (unavailable)`, selectedValue, true, true));
        }
    }

    function populateModelSelect(select, providerId, selectedValue) {
        select.innerHTML = "";
        const models = modelsData.filter(m => m.provider === providerId);
        if (models.length === 0) {
            if (selectedValue) {
                select.appendChild(new Option(
                    `${selectedValue} (unavailable)`, selectedValue, true, true
                ));
            } else {
                select.appendChild(new Option("No models", ""));
            }
            return;
        }
        models.forEach(m => {
            const opt = new Option(m.name, m.id);
            if (m.id === selectedValue || (m.is_default && !selectedValue)) opt.selected = true;
            select.appendChild(opt);
        });
        if (selectedValue && !Array.from(select.options).some(opt => opt.value === selectedValue)) {
            select.appendChild(new Option(`${selectedValue} (unavailable)`, selectedValue, true, true));
        }
    }

    function populateKbCheckboxes(container, selectedIds) {
        container.innerHTML = "";
        const selected = new Set(selectedIds || []);
        knowledgeBases.forEach(kb => {
            const label = document.createElement("label");
            label.className = "checkbox-label";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = kb.id;
            if (selected.has(kb.id)) cb.checked = true;
            label.appendChild(cb);
            label.appendChild(document.createTextNode(` ${kb.name}`));
            container.appendChild(label);
        });
        selected.forEach(id => {
            if (knowledgeBases.some(kb => kb.id === id)) return;
            const label = document.createElement("label");
            label.className = "checkbox-label";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = id;
            cb.checked = true;
            cb.disabled = true;
            label.appendChild(cb);
            label.appendChild(document.createTextNode(` ${id} (unavailable)`));
            container.appendChild(label);
        });
        if (knowledgeBases.length === 0) {
            const p = document.createElement("span");
            p.className = "muted";
            p.textContent = "No active knowledge bases.";
            container.appendChild(p);
        }
    }

    function populatePromptSelect(select, promptRef) {
        select.innerHTML = '<option value="" disabled>Select a system prompt</option>';
        if (personalPrompts.length > 0) {
            const group = document.createElement("optgroup");
            group.label = "My Prompts";
            personalPrompts.forEach(p => {
                const opt = new Option(`[personal] ${p.name}`, `personal::${p.id}`);
                if (promptRef && promptRef.scope === "personal" && promptRef.id === p.id) opt.selected = true;
                group.appendChild(opt);
            });
            select.appendChild(group);
        }
        if (sharedPrompts.length > 0) {
            const group = document.createElement("optgroup");
            group.label = "Shared (admin)";
            sharedPrompts.forEach(p => {
                const opt = new Option(`[shared] ${p.name}`, `shared::${p.id}`);
                if (promptRef && promptRef.scope === "shared" && promptRef.id === p.id) opt.selected = true;
                group.appendChild(opt);
            });
            select.appendChild(group);
        }
        if (personalPrompts.length === 0 && sharedPrompts.length === 0) {
            const opt = new Option("No prompts available", "");
            opt.disabled = true;
            select.appendChild(opt);
        }
        const target = promptRef && promptRef.id && promptRef.scope
            ? `${promptRef.scope}::${promptRef.id}`
            : "";
        if (target && !Array.from(select.options).some(opt => opt.value === target)) {
            select.appendChild(new Option(
                `[${promptRef.scope}] ${promptRef.id} (unavailable)`,
                target,
                true,
                true
            ));
        }
    }

    async function loadFormData() {
        try {
            const [modelsRes, kbRes, personalRes, sharedRes] = await Promise.all([
                fetch("/models").then(r => r.json()),
                fetch("/api/knowledge-bases").then(r => r.json()),
                fetch("/api/prompts").then(r => r.json()),
                fetch("/api/prompts/shared").then(r => r.json())
            ]);
            modelsData = modelsRes.models || [];
            knowledgeBases = (kbRes.knowledge_bases || []).filter(kb => kb.status === "active");
            personalPrompts = personalRes.personal || [];
            sharedPrompts = sharedRes.prompts || [];

            const createProvider = document.getElementById("createAgentProvider");
            const createModel = document.getElementById("createAgentModel");
            const createKb = document.getElementById("createAgentKnowledgeBases");
            const createPrompt = document.getElementById("createAgentPrompt");
            const createKbLimit = document.getElementById("createAgentKbLimit");
            if (createKbLimit) {
                createKbLimit.textContent = `(up to ${limits.max_query_knowledge_bases})`;
            }
            populateProviderSelect(createProvider, null);
            populateModelSelect(createModel, createProvider.value, null);
            createProvider.addEventListener("change", () => {
                populateModelSelect(createModel, createProvider.value, null);
            });
            populateKbCheckboxes(createKb, []);
            populatePromptSelect(createPrompt, null);
            const hasCreationOptions = Boolean(
                createProvider.value
                && createModel.value
                && knowledgeBases.length
                && (personalPrompts.length || sharedPrompts.length)
            );
            showCreate.disabled = !hasCreationOptions;
            if (!hasCreationOptions) {
                showCreate.title = "Configure at least one model, knowledge base, and system prompt first.";
            }
        } catch (error) {
            showNotice("Failed to load form data: " + error.message, "error");
        }
    }

    async function jsonRequest(url, options = {}) {
        const requestOptions = {...options};
        requestOptions.headers = {
            "Accept": "application/json",
            ...(requestOptions.body ? {"Content-Type": "application/json"} : {}),
            ...(requestOptions.headers || {})
        };
        const response = await fetch(url, requestOptions);
        let payload = {};
        try {
            payload = await response.json();
        } catch (error) {
            payload = {error: response.statusText};
        }
        if (!response.ok) throw new Error(payload.error || response.statusText);
        return payload;
    }

    function showNotice(message, type) {
        notice.hidden = false;
        notice.className = `notice ${type || ""}`;
        notice.textContent = message;
    }

    function escapeHtml(value) {
        const element = document.createElement("div");
        element.textContent = String(value || "");
        return element.innerHTML;
    }

    function escapeAttribute(value) {
        return escapeHtml(value).replace(/"/g, "&quot;");
    }

    (async () => {
        try {
            await loadAgents(false);
            await loadFormData();
            render(agentsData);
        } catch (error) {
            // The individual loaders already expose the actionable error.
        }
    })();
});
