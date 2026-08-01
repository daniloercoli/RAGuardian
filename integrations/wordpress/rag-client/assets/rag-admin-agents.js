(function () {
    "use strict";

    function parseJson(value, fallback) {
        try {
            const parsed = JSON.parse(value || "");
            return parsed && typeof parsed === "object" ? parsed : fallback;
        } catch (_error) {
            return fallback;
        }
    }

    async function request(root, action, payload, agentId) {
        const body = new URLSearchParams();
        body.set("action", action);
        body.set("nonce", root.dataset.nonce || "");
        if (payload) body.set("payload", JSON.stringify(payload));
        if (agentId) body.set("agent_id", agentId);
        const response = await fetch(root.dataset.ajaxUrl, {
            method: "POST",
            headers: {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error((result.data && result.data.message) || "Agent request failed");
        }
        return result.data;
    }

    function addOption(select, label, value, selected) {
        const option = new Option(label, value);
        option.selected = Boolean(selected);
        select.appendChild(option);
    }

    function editor(root, options, agent) {
        const panel = document.createElement("div");
        panel.className = "notice notice-info";
        panel.style.padding = "12px";

        const heading = document.createElement("h4");
        heading.textContent = agent ? "Edit Agent" : "Create Agent";
        panel.appendChild(heading);

        const fields = document.createElement("div");
        fields.style.display = "grid";
        fields.style.gap = "10px";
        fields.style.maxWidth = "720px";
        panel.appendChild(fields);

        function textField(labelText, value, maxLength, multiline) {
            const label = document.createElement("label");
            const title = document.createElement("strong");
            title.textContent = labelText;
            const input = multiline ? document.createElement("textarea") : document.createElement("input");
            input.value = value || "";
            input.maxLength = maxLength;
            if (multiline) input.rows = 3;
            label.append(title, document.createElement("br"), input);
            fields.appendChild(label);
            return input;
        }

        const name = textField("Name", agent && agent.name, 120, false);
        const description = textField("Description", agent && agent.description, 500, true);

        const providerLabel = document.createElement("label");
        providerLabel.appendChild(document.createTextNode("Provider "));
        const provider = document.createElement("select");
        providerLabel.appendChild(provider);
        fields.appendChild(providerLabel);

        const modelLabel = document.createElement("label");
        modelLabel.appendChild(document.createTextNode("Model "));
        const model = document.createElement("select");
        modelLabel.appendChild(model);
        fields.appendChild(modelLabel);

        const models = Array.isArray(options.models) ? options.models : [];
        const providers = [];
        models.forEach((item) => {
            if (!providers.some((entry) => entry.id === item.provider)) {
                providers.push({id: item.provider, name: item.provider_name || item.provider});
            }
        });
        providers.forEach((item) => addOption(
            provider, item.name, item.id, agent && agent.provider_id === item.id
        ));
        if (agent && agent.provider_id && !providers.some((item) => item.id === agent.provider_id)) {
            addOption(provider, agent.provider_id + " (unavailable)", agent.provider_id, true);
        }

        function renderModels() {
            model.replaceChildren();
            const matches = models.filter((item) => item.provider === provider.value);
            matches.forEach((item) => addOption(
                model,
                item.name || item.id,
                item.id,
                agent ? agent.model_id === item.id : Boolean(item.is_default)
            ));
            if (agent && agent.model_id && !matches.some((item) => item.id === agent.model_id)) {
                addOption(model, agent.model_id + " (unavailable)", agent.model_id, true);
            }
        }
        provider.addEventListener("change", renderModels);
        renderModels();

        const kbGroup = document.createElement("fieldset");
        const kbLegend = document.createElement("legend");
        kbLegend.textContent = "Knowledge bases";
        kbGroup.appendChild(kbLegend);
        const selectedKbs = new Set((agent && agent.knowledge_base_ids) || []);
        const kbs = Array.isArray(options.knowledge_bases) ? options.knowledge_bases : [];
        kbs.forEach((kb) => {
            const label = document.createElement("label");
            label.style.display = "block";
            const input = document.createElement("input");
            input.type = "checkbox";
            input.value = kb.id;
            input.checked = selectedKbs.has(kb.id);
            label.append(input, document.createTextNode(" " + (kb.name || kb.id)));
            kbGroup.appendChild(label);
        });
        selectedKbs.forEach((id) => {
            if (kbs.some((kb) => kb.id === id)) return;
            const label = document.createElement("label");
            label.style.display = "block";
            const input = document.createElement("input");
            input.type = "checkbox";
            input.value = id;
            input.checked = true;
            input.disabled = true;
            label.append(input, document.createTextNode(" " + id + " (unavailable)"));
            kbGroup.appendChild(label);
        });
        fields.appendChild(kbGroup);

        const promptLabel = document.createElement("label");
        promptLabel.appendChild(document.createTextNode("System prompt "));
        const prompt = document.createElement("select");
        addOption(prompt, "Select a system prompt", "", false);
        (Array.isArray(options.prompts) ? options.prompts : []).forEach((item) => {
            const value = item.scope + "::" + item.id;
            const selected = agent && agent.prompt_ref
                && agent.prompt_ref.scope === item.scope
                && agent.prompt_ref.id === item.id;
            addOption(prompt, "[" + item.scope + "] " + (item.name || item.id), value, selected);
        });
        if (agent && agent.prompt_ref && agent.prompt_ref.id) {
            const value = agent.prompt_ref.scope + "::" + agent.prompt_ref.id;
            if (!Array.from(prompt.options).some((item) => item.value === value)) {
                addOption(prompt, value + " (unavailable)", value, true);
            }
        }
        promptLabel.appendChild(prompt);
        fields.appendChild(promptLabel);

        const actions = document.createElement("p");
        const save = document.createElement("button");
        save.type = "button";
        save.className = "button button-primary";
        save.textContent = "Save";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "button";
        cancel.style.marginLeft = "8px";
        cancel.textContent = "Cancel";
        actions.append(save, cancel);
        panel.appendChild(actions);

        cancel.addEventListener("click", () => panel.remove());
        save.addEventListener("click", async () => {
            const knowledgeBaseIds = Array.from(kbGroup.querySelectorAll("input:checked"))
                .map((item) => item.value);
            const parts = prompt.value.split("::");
            if (!name.value.trim() || !provider.value || !model.value || !knowledgeBaseIds.length || parts.length !== 2) {
                throwNotice(root, "Complete all required Agent fields.");
                return;
            }
            const payload = {
                name: name.value.trim(),
                description: description.value.trim(),
                provider_id: provider.value,
                model_id: model.value,
                knowledge_base_ids: knowledgeBaseIds,
                prompt_ref: {scope: parts[0], id: parts[1]},
            };
            save.disabled = true;
            try {
                await request(
                    root,
                    agent ? "ec_rag_update_agent" : "ec_rag_create_agent",
                    payload,
                    agent && agent.id
                );
                window.location.reload();
            } catch (error) {
                throwNotice(root, error.message);
                save.disabled = false;
            }
        });
        return panel;
    }

    function throwNotice(root, message) {
        const notice = root.querySelector("[data-ec-rag-agent-notice]");
        if (notice) notice.textContent = message;
    }

    function initialize(root) {
        const agents = parseJson(root.dataset.agents, []);
        const options = parseJson(root.dataset.options, {});
        const canManage = root.dataset.canManage === "1";
        const list = root.querySelector("[data-ec-rag-agent-list]");
        const create = root.querySelector("[data-ec-rag-agent-create]");
        if (!Array.isArray(agents) || !list) return;

        if (!agents.length) {
            const empty = document.createElement("p");
            empty.textContent = "No Agents created yet.";
            list.appendChild(empty);
        }
        agents.forEach((agent) => {
            const row = document.createElement("div");
            row.style.marginBottom = "10px";
            const name = document.createElement("strong");
            name.textContent = agent.name || agent.id;
            row.appendChild(name);
            if (!agent.available) row.appendChild(document.createTextNode(" — unavailable"));
            if (canManage) {
                const edit = document.createElement("button");
                edit.type = "button";
                edit.className = "button button-small";
                edit.style.marginLeft = "8px";
                edit.textContent = "Edit";
                edit.addEventListener("click", () => row.after(editor(root, options, agent)));
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "button button-small";
                remove.style.marginLeft = "6px";
                remove.textContent = "Delete";
                remove.addEventListener("click", async () => {
                    if (!window.confirm("Delete Agent ‘" + (agent.name || agent.id) + "’?")) return;
                    try {
                        await request(root, "ec_rag_delete_agent", null, agent.id);
                        window.location.reload();
                    } catch (error) {
                        throwNotice(root, error.message);
                    }
                });
                row.append(edit, remove);
            }
            list.appendChild(row);
        });
        if (create) {
            create.addEventListener("click", () => create.before(editor(root, options, null)));
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("[data-ec-rag-agent-admin]").forEach(initialize);
    });
})();
