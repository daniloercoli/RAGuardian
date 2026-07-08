document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("ragUploadForm");
    const fileInput = document.getElementById("ragFileInput");
    const folderInput = document.getElementById("ragFolderInput");
    const startButton = document.getElementById("startUploadButton");
    const cancelButton = document.getElementById("cancelUploadButton");
    const progressPanel = document.getElementById("uploadProgressPanel");
    const progressTitle = document.getElementById("uploadProgressTitle");
    const progressCount = document.getElementById("uploadProgressCount");
    const progressBar = document.getElementById("uploadProgressBar");
    const queueList = document.getElementById("uploadQueueList");
    const errorPanel = document.getElementById("uploadErrorPanel");
    const errorList = document.getElementById("uploadErrorList");
    const finalStatus = document.getElementById("uploadFinalStatus");
    const rebuildActions = document.querySelector("[data-rebuild-url]");
    const rebuildButtons = Array.from(document.querySelectorAll("[data-rebuild-trigger]"));
    const rebuildModal = document.getElementById("rebuildModal");
    const rebuildModalMessage = document.getElementById("rebuildModalMessage");
    const rebuildCurrentFile = document.getElementById("rebuildCurrentFile");
    const rebuildProgressCount = document.getElementById("rebuildProgressCount");
    const rebuildProgressBar = document.getElementById("rebuildProgressBar");
    const rebuildErrorPanel = document.getElementById("rebuildErrorPanel");
    const rebuildErrorList = document.getElementById("rebuildErrorList");
    const closeRebuildModalButton = document.getElementById("closeRebuildModalButton");
    const fileErrorModal = document.getElementById("fileErrorModal");
    const fileErrorModalMessage = document.getElementById("fileErrorModalMessage");
    const closeFileErrorModalButton = document.getElementById("closeFileErrorModalButton");

    if (!form || !fileInput || !folderInput || !startButton || !cancelButton || !progressPanel || !queueList) {
        return;
    }

    let running = false;
    let cancelRequested = false;
    let queueItems = [];

    [fileInput, folderInput].forEach((input) => input.addEventListener("change", () => {
        if (running) return;
        const files = selectedUploadItems();
        resetOutput();
        if (files.length > 0) {
            progressPanel.hidden = false;
            queueItems = renderQueue(files);
            updateProgress(0, files.length, "Ready to upload");
        }
    }));

    cancelButton.addEventListener("click", () => {
        if (!running) return;
        cancelRequested = true;
        cancelButton.disabled = true;
        updateProgress(countFinished(), queueItems.length, "Cancellation requested");
        finalStatus.hidden = false;
        finalStatus.className = "upload-final-status warning";
        finalStatus.textContent = "Completing current file and then stopping the queue.";
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (running) return;

        const files = selectedUploadItems();
        resetOutput();
        if (files.length === 0) {
            finalStatus.hidden = false;
            finalStatus.className = "upload-final-status error";
            finalStatus.textContent = "Select at least one supported file.";
            return;
        }

        running = true;
        cancelRequested = false;
        setControlsBusy(true);
        progressPanel.hidden = false;
        queueItems = renderQueue(files);
        const errors = [];

        for (let index = 0; index < files.length; index += 1) {
            if (cancelRequested) {
                markRemainingAsCancelled(index);
                break;
            }

            const uploadItem = files[index];
            setQueueStatus(index, "uploading", "Uploading and indexing");
            updateProgress(index, files.length, `Processing ${uploadItem.displayName}`);

            try {
                const result = await uploadFile(uploadItem);
                if (result.status === "duplicate") {
                    setQueueStatus(index, "duplicate", "Duplicate, not reindexed");
                } else {
                    const chunks = Number.isFinite(Number(result.chunks)) ? Number(result.chunks) : 0;
                    setQueueStatus(index, "done", `Indexed (${chunks} chunks)`);
                }
            } catch (error) {
                const message = error.message || "Unknown error";
                errors.push({name: uploadItem.displayName, message});
                setQueueStatus(index, "error", message);
            }

            updateProgress(index + 1, files.length, cancelRequested ? "Cancellation requested" : "Uploading");
        }

        renderErrors(errors);
        finishRun(files.length, errors.length);
    });

    function selectedUploadItems() {
        const allowedExtensions = [".pdf", ".txt", ".md", ".csv", ".mp3", ".wav", ".m4a", ".webm", ".ogg", ".flac"];
        return [...filesFromInput(fileInput), ...filesFromInput(folderInput)].filter(({file}) => {
            const name = file.name.toLowerCase();
            return allowedExtensions.some((extension) => name.endsWith(extension));
        });
    }

    function filesFromInput(input) {
        return Array.from(input.files || []).map((file) => {
            const relativePath = file.webkitRelativePath || file.name;
            return {
                file,
                relativePath,
                displayName: relativePath || file.name
            };
        });
    }

    function resetOutput() {
        progressPanel.hidden = true;
        queueList.replaceChildren();
        cancelButton.hidden = true;
        cancelButton.disabled = true;
        errorPanel.hidden = true;
        errorList.replaceChildren();
        finalStatus.hidden = true;
        finalStatus.textContent = "";
        finalStatus.className = "upload-final-status";
    }

    if (rebuildButtons.length && rebuildActions && rebuildModal) {
        rebuildButtons.forEach((button) => {
            button.addEventListener("click", startRebuild);
        });
    }

    if (closeRebuildModalButton) {
        closeRebuildModalButton.addEventListener("click", () => {
            rebuildModal.hidden = true;
            window.location.reload();
        });
    }

    document.querySelectorAll("[data-file-error]").forEach((button) => {
        button.addEventListener("click", () => {
            if (!fileErrorModal || !fileErrorModalMessage) return;
            fileErrorModalMessage.textContent = button.dataset.fileError || "Unknown error";
            fileErrorModal.hidden = false;
        });
    });

    if (closeFileErrorModalButton) {
        closeFileErrorModalButton.addEventListener("click", closeFileErrorModal);
    }
    if (fileErrorModal) {
        fileErrorModal.addEventListener("click", (event) => {
            if (event.target === fileErrorModal) {
                closeFileErrorModal();
            }
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !fileErrorModal.hidden) {
                closeFileErrorModal();
            }
        });
    }

    function closeFileErrorModal() {
        if (fileErrorModal) {
            fileErrorModal.hidden = true;
        }
    }

    async function startRebuild() {
        if (running) return;

        const confirmed = window.confirm(
            "Are you sure? This operation will delete the current vector index and recreate it from all tracked document/audio files on disk. If a file fails, the index may remain partial until a new rebuild."
        );
        if (!confirmed) return;

        const rebuildUrl = rebuildActions.dataset.rebuildUrl;
        showRebuildModal();
        setRebuildButtonsDisabled(true);

        try {
            const response = await fetch(rebuildUrl, {
                method: "POST",
                headers: {"Accept": "application/json"},
                credentials: "same-origin"
            });
            const payload = await parseJsonResponse(response);
            if (!response.ok && response.status !== 409) {
                throw new Error(payload.error || response.statusText);
            }
            if (!payload.job_id) {
                throw new Error(payload.error || "Job not started");
            }
            await pollRebuild(payload.job_id);
        } catch (error) {
            renderRebuildFailure(error.message || "Unknown error");
        } finally {
            setRebuildButtonsDisabled(false);
        }
    }

    function setRebuildButtonsDisabled(isDisabled) {
        rebuildButtons.forEach((button) => {
            button.disabled = isDisabled;
        });
    }

    function showRebuildModal() {
        rebuildModal.hidden = false;
        rebuildModalMessage.textContent = "Starting rebuild...";
        rebuildCurrentFile.textContent = "Preparing";
        rebuildProgressCount.textContent = "0/0";
        rebuildProgressBar.value = 0;
        rebuildErrorPanel.hidden = true;
        rebuildErrorList.replaceChildren();
        closeRebuildModalButton.disabled = true;
    }

    async function pollRebuild(jobId) {
        let done = false;
        while (!done) {
            const response = await fetch(`/admin/files/rebuild/${encodeURIComponent(jobId)}`, {
                method: "GET",
                headers: {"Accept": "application/json"},
                credentials: "same-origin"
            });
            const job = await parseJsonResponse(response);
            if (!response.ok) {
                throw new Error(job.error || response.statusText);
            }

            renderRebuildJob(job);
            done = ["completed", "completed_with_errors", "failed"].includes(job.status);
            if (!done) {
                await wait(900);
            }
        }
        closeRebuildModalButton.disabled = false;
    }

    function renderRebuildJob(job) {
        const total = Number(job.total || 0);
        const processed = Number(job.processed || 0);
        const percent = total > 0 ? Math.round((processed / total) * 100) : 100;

        rebuildModalMessage.textContent = job.message || "Rebuild in progress";
        rebuildCurrentFile.textContent = job.current_file || terminalRebuildLabel(job.status);
        rebuildProgressCount.textContent = `${processed}/${total}`;
        rebuildProgressBar.value = percent;
        renderRebuildErrors(job.errors || []);
    }

    function terminalRebuildLabel(status) {
        if (status === "completed") return "Completed";
        if (status === "completed_with_errors") return "Completed with errors";
        if (status === "failed") return "Failed";
        return "In progress";
    }

    function renderRebuildErrors(errors) {
        rebuildErrorList.replaceChildren();
        if (!errors.length) {
            rebuildErrorPanel.hidden = true;
            return;
        }

        rebuildErrorPanel.hidden = false;
        errors.forEach((error) => {
            const item = document.createElement("li");
            const name = document.createElement("strong");
            name.textContent = error.filename || "file";
            const message = document.createElement("span");
            message.textContent = ` ${error.error || "Unknown error"}`;
            item.append(name, message);
            rebuildErrorList.appendChild(item);
        });
    }

    function renderRebuildFailure(message) {
        rebuildModal.hidden = false;
        rebuildModalMessage.textContent = message;
        rebuildCurrentFile.textContent = "Failed";
        rebuildProgressCount.textContent = "0/0";
        rebuildProgressBar.value = 0;
        renderRebuildErrors([{filename: "index", error: message}]);
        closeRebuildModalButton.disabled = false;
    }

    function wait(ms) {
        return new Promise((resolve) => {
            window.setTimeout(resolve, ms);
        });
    }

    function setControlsBusy(isBusy) {
        fileInput.disabled = isBusy;
        folderInput.disabled = isBusy;
        startButton.disabled = isBusy;
        cancelButton.disabled = !isBusy;
        cancelButton.hidden = !isBusy;
    }

    function renderQueue(files) {
        queueList.replaceChildren();
        return files.map((uploadItem) => {
            const item = document.createElement("li");
            item.className = "upload-queue-item pending";

            const name = document.createElement("span");
            name.className = "upload-file-name";
            name.textContent = uploadItem.displayName;

            const status = document.createElement("span");
            status.className = "upload-file-status";
            status.textContent = "Waiting";

            item.append(name, status);
            queueList.appendChild(item);
            return {item, status};
        });
    }

    function setQueueStatus(index, state, text) {
        const entry = queueItems[index];
        if (!entry) return;
        entry.item.className = `upload-queue-item ${state}`;
        entry.status.textContent = text;
    }

    function markRemainingAsCancelled(startIndex) {
        for (let index = startIndex; index < queueItems.length; index += 1) {
            setQueueStatus(index, "cancelled", "Cancelled");
        }
    }

    function countFinished() {
        return queueItems.filter(({item}) => (
            item.classList.contains("done") ||
            item.classList.contains("duplicate") ||
            item.classList.contains("error")
        )).length;
    }

    function updateProgress(done, total, title) {
        progressTitle.textContent = title;
        progressCount.textContent = `${done}/${total}`;
        progressBar.value = total > 0 ? Math.round((done / total) * 100) : 0;
    }

    async function uploadFile(uploadItem) {
        const file = uploadItem.file;
        const formData = new FormData();
        formData.append("file", file, file.name);
        formData.append("relative_path", uploadItem.relativePath || file.name);
        const endpoint = isAudioFile(file.name) ? "/api/v1/audio" : "/api/v1/files";

        const response = await fetch(endpoint, {
            method: "POST",
            headers: {"Accept": "application/json"},
            body: formData,
            credentials: "same-origin"
        });

        const payload = await parseJsonResponse(response);
        if (!response.ok || payload.error) {
            throw new Error(payload.error || response.statusText);
        }
        return payload;
    }

    function isAudioFile(filename) {
        const audioExtensions = [".mp3", ".wav", ".m4a", ".webm", ".ogg", ".flac"];
        const name = filename.toLowerCase();
        return audioExtensions.some((extension) => name.endsWith(extension));
    }

    async function parseJsonResponse(response) {
        try {
            return await response.json();
        } catch (error) {
            return {error: `Invalid response (${response.status})`};
        }
    }

    function renderErrors(errors) {
        if (errors.length === 0) return;

        errorPanel.hidden = false;
        errorList.replaceChildren();
        errors.forEach((error) => {
            const item = document.createElement("li");
            const name = document.createElement("strong");
            name.textContent = error.name;
            const message = document.createElement("span");
            message.textContent = ` ${error.message}`;
            item.append(name, message);
            errorList.appendChild(item);
        });
    }

    function finishRun(total, errorCount) {
        const completed = countFinished();
        const cancelled = cancelRequested;

        running = false;
        setControlsBusy(false);
        cancelButton.hidden = true;
        fileInput.value = "";
        folderInput.value = "";

        finalStatus.hidden = false;
        if (cancelled) {
            finalStatus.className = "upload-final-status warning";
            finalStatus.textContent = `Queue cancelled. Files completed: ${completed}/${total}.`;
        } else if (errorCount > 0) {
            finalStatus.className = "upload-final-status error";
            finalStatus.textContent = `Process finished with ${errorCount} error(s).`;
        } else {
            finalStatus.className = "upload-final-status success";
            finalStatus.textContent = `Process completed. Files processed: ${completed}/${total}.`;
        }
    }
});

/* ── Backup & Recovery ──────────────────────────────────────────── */
(async function initBackup() {
    const tbody = document.getElementById("backupTableBody");
    if (!tbody) return;

    async function refreshBackupList() {
        try {
            const res = await fetch("/admin/backup/list");
            const backups = await res.json();
            renderBackups(backups);
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="5" class="error">Failed to load backups: ${e.message}</td></tr>`;
        }
    }

    function renderBackups(backups) {
        if (backups.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="muted">No backups available.</td></tr>`;
            return;
        }
        const rows = backups
            .map((b) => {
                const size = b.size_bytes
                    ? `${(b.size_bytes / (1024 * 1024)).toFixed(1)} MB`
                    : "—";
                const docs = b.document_count ?? "—";
                const created = formatBackupDate(b.created_at);
                const id = String(b.id || "");
                const safeId = escapeHtml(id);
                return `<tr>
                    <td><code>${safeId}</code></td>
                    <td>${docs}</td>
                    <td>${size}</td>
                    <td>${created}</td>
                    <td class="table-actions">
                        <button class="small secondary" data-action="verify" data-id="${safeId}">Verify</button>
                        <button class="small" data-action="restore" data-id="${safeId}">Restore</button>
                        <button class="small danger" data-action="delete" data-id="${safeId}">Delete</button>
                    </td>
                </tr>`;
            })
            .join("");
        tbody.innerHTML = rows;

        tbody.querySelectorAll("[data-action]").forEach((btn) => {
            btn.addEventListener("click", handleBackupAction);
        });
    }

    function handleBackupAction(e) {
        const action = e.target.dataset.action;
        const id = e.target.dataset.id;

        if (action === "delete") {
            if (!confirm(`Delete backup ${id}?`)) return;
            fetch(`/admin/backup/delete/${encodeURIComponent(id)}`, {
                method: "POST",
                credentials: "same-origin"
            })
                .then(() => refreshBackupList())
                .catch((err) => alert("Delete failed: " + err.message));
        } else if (action === "restore") {
            if (!confirm(`Restore from backup ${id}? This will REPLACE the current vector index.`)) return;
            fetch(`/admin/backup/restore/${encodeURIComponent(id)}`, {
                method: "POST",
                credentials: "same-origin"
            })
                .then(() => {
                    alert("Restore complete. Browser will reload.");
                    location.reload();
                })
                .catch((err) => alert("Restore failed: " + err.message));
        } else if (action === "verify") {
            fetch(`/admin/backup/verify/${encodeURIComponent(id)}`, {
                credentials: "same-origin"
            })
                .then((res) => res.json())
                .then((data) => {
                    const status = data.status === "ok" ? "Verified OK" : `${data.error || "Mismatch"}`;
                    alert(status);
                })
                .catch((err) => alert("Verify failed: " + err.message));
        }
    }

    function formatBackupDate(value) {
        if (!value) return "—";
        const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
        return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
    }

    function escapeHtml(value) {
        return value.replace(/[&<>"']/g, (char) => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#039;"
        })[char]);
    }

    // Refresh backup list on page load
    refreshBackupList();
})();
