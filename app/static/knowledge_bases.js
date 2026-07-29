document.addEventListener("DOMContentLoaded", () => {
    const list = document.getElementById("knowledgeBaseList");
    const notice = document.getElementById("kbNotice");
    const createForm = document.getElementById("createKbForm");
    const showCreate = document.getElementById("showCreateKbButton");
    const cancelCreate = document.getElementById("cancelCreateKbButton");
    const terminalStatuses = new Set(["completed", "completed_with_errors", "failed"]);

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
        const formData = new FormData(createForm);
        try {
            await jsonRequest("/api/knowledge-bases", {
                method: "POST",
                body: JSON.stringify({
                    name: formData.get("name"),
                    description: formData.get("description")
                })
            });
            createForm.reset();
            createForm.hidden = true;
            showNotice("Knowledge base created.", "success");
            await loadKnowledgeBases();
        } catch (error) {
            showNotice(error.message, "error");
        }
    });

    async function loadKnowledgeBases() {
        try {
            const payload = await jsonRequest("/api/knowledge-bases");
            render(payload.knowledge_bases || []);
        } catch (error) {
            list.innerHTML = `<p class="notice error">${escapeHtml(error.message)}</p>`;
        }
    }

    function render(items) {
        list.replaceChildren();
        items.forEach(item => list.appendChild(renderCard(item)));
    }

    function renderCard(item) {
        const card = document.createElement("article");
        card.className = "panel knowledge-base-card";
        const badge = item.is_default
            ? '<span class="badge">Default</span>'
            : `<span class="status-pill ${escapeHtml(item.status)}">${escapeHtml(item.status)}</span>`;
        card.innerHTML = `
            <div class="panel-heading">
                <div>
                    <h3>${escapeHtml(item.name)} ${badge}</h3>
                    <p>${escapeHtml(item.description || "No description")}</p>
                </div>
            </div>
            <dl class="kb-stats">
                <div><dt>Files</dt><dd>${Number(item.stats.tracked_files || 0)}</dd></div>
                <div><dt>Indexed</dt><dd>${Number(item.stats.indexed_files || 0)}</dd></div>
                <div><dt>Chunks</dt><dd>${Number(item.stats.chunks || 0)}</dd></div>
                <div><dt>Sources</dt><dd>${Number(item.stats.data_sources || 0)}</dd></div>
            </dl>
            ${item.delete_error ? `<p class="notice error">${escapeHtml(item.delete_error)}</p>` : ""}
            <div class="table-actions">
                <a class="secondary-link" href="/admin/files?knowledge_base_id=${encodeURIComponent(item.id)}">Open files</a>
                <a class="secondary-link" href="/admin/data-sources?knowledge_base_id=${encodeURIComponent(item.id)}">Open sources</a>
                <button type="button" class="secondary" data-action="edit">Rename</button>
                <button type="button" class="danger" data-action="delete" ${item.is_default ? "disabled" : ""}>
                    ${item.status === "deleting" ? "Resume delete" : item.status === "delete_failed" ? "Retry delete" : "Delete"}
                </button>
            </div>
            <form class="form-grid compact kb-editor" data-editor hidden>
                <label>Name<input name="name" value="${escapeAttribute(item.name)}" maxlength="120" required></label>
                <label>Description<textarea name="description" maxlength="1000" rows="3">${escapeHtml(item.description || "")}</textarea></label>
                <div class="modal-actions">
                    <button type="submit">Save</button>
                    <button type="button" class="secondary" data-action="cancel-edit">Cancel</button>
                </div>
            </form>
            <p class="muted" data-delete-progress hidden></p>
        `;
        const editor = card.querySelector("[data-editor]");
        card.querySelector('[data-action="edit"]').addEventListener("click", () => {
            editor.hidden = false;
            editor.elements.name.focus();
        });
        card.querySelector('[data-action="cancel-edit"]').addEventListener("click", () => {
            editor.hidden = true;
        });
        editor.addEventListener("submit", async (event) => {
            event.preventDefault();
            try {
                await jsonRequest(`/api/knowledge-bases/${encodeURIComponent(item.id)}`, {
                    method: "PATCH",
                    body: JSON.stringify({
                        name: editor.elements.name.value,
                        description: editor.elements.description.value
                    })
                });
                showNotice("Knowledge base updated.", "success");
                await loadKnowledgeBases();
            } catch (error) {
                showNotice(error.message, "error");
            }
        });
        card.querySelector('[data-action="delete"]').addEventListener("click", async () => {
            const confirmation = window.prompt(
                `Type "${item.name}" to permanently delete this knowledge base.`
            );
            if (confirmation !== item.name) return;
            const progress = card.querySelector("[data-delete-progress]");
            progress.hidden = false;
            progress.textContent = "Starting deletion...";
            try {
                const job = await jsonRequest(
                    `/api/knowledge-bases/${encodeURIComponent(item.id)}`,
                    {method: "DELETE"}
                );
                await pollDeleteJob(job.job_id, progress);
                showNotice("Knowledge base deleted.", "success");
                await loadKnowledgeBases();
            } catch (error) {
                progress.textContent = error.message;
                showNotice(error.message, "error");
                await loadKnowledgeBases();
            }
        });
        return card;
    }

    async function pollDeleteJob(jobId, progress) {
        while (true) {
            const job = await jsonRequest(
                `/api/knowledge-bases/jobs/${encodeURIComponent(jobId)}`
            );
            progress.textContent = `${job.message || "Deleting"} (${job.processed || 0}/${job.total || 0})`;
            if (terminalStatuses.has(job.status)) {
                if (job.status !== "completed") {
                    throw new Error(job.message || "Deletion failed");
                }
                return;
            }
            await new Promise(resolve => window.setTimeout(resolve, 700));
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

    loadKnowledgeBases();
});
