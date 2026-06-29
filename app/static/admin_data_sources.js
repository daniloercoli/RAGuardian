document.addEventListener("DOMContentLoaded", () => {
    const statusBox = document.getElementById("dataSourceJobStatus");
    const syncButtons = Array.from(document.querySelectorAll("[data-sync-source]"));
    const toggleButtons = Array.from(document.querySelectorAll("[data-toggle-source]"));

    syncButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            const url = button.dataset.syncUrl;
            if (!url) return;

            setBusy(true);
            renderStatus("Starting sync...", "warning");
            try {
                const response = await fetch(url, {
                    method: "POST",
                    headers: {"Accept": "application/json"},
                    credentials: "same-origin"
                });
                const payload = await parseJson(response);
                if (!response.ok) {
                    throw new Error(payload.error || response.statusText);
                }
                await pollJob(payload.job_id);
            } catch (error) {
                renderStatus(error.message || "Sync failed", "error");
            } finally {
                setBusy(false);
            }
        });
    });

    async function pollJob(jobId) {
        let done = false;
        while (!done) {
            const response = await fetch(`/admin/data-sources/jobs/${encodeURIComponent(jobId)}`, {
                method: "GET",
                headers: {"Accept": "application/json"},
                credentials: "same-origin"
            });
            const job = await parseJson(response);
            if (!response.ok) {
                throw new Error(job.error || response.statusText);
            }
            renderJob(job);
            done = ["completed", "completed_with_errors", "failed"].includes(job.status);
            if (!done) {
                await wait(900);
            }
        }
        window.setTimeout(() => window.location.reload(), 900);
    }

    function renderJob(job) {
        const total = Number(job.total || 0);
        const processed = Number(job.processed || 0);
        const file = job.current_file ? ` (${job.current_file})` : "";
        const message = `${job.message || job.status}: ${processed}/${total}${file}`;
        renderStatus(message, job.status === "failed" ? "error" : "warning");
    }

    function renderStatus(message, type) {
        if (!statusBox) return;
        statusBox.hidden = false;
        statusBox.className = `notice ${type}`;
        statusBox.textContent = message;
    }

    function setBusy(isBusy) {
        syncButtons.forEach((button) => {
            button.disabled = isBusy || button.dataset.syncDisabled === "true";
        });
        toggleButtons.forEach((button) => {
            button.disabled = isBusy;
        });
    }

    toggleButtons.forEach((button) => {
        button.addEventListener("click", async () => {
            const url = button.dataset.toggleUrl;
            if (!url) return;
            const enabled = button.dataset.toggleEnabled === "true";
            button.disabled = true;
            try {
                const response = await fetch(url, {
                    method: "POST",
                    headers: {
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    },
                    credentials: "same-origin",
                    body: JSON.stringify({ enabled })
                });
                const result = await parseJson(response);
                if (!response.ok) {
                    throw new Error(result.error || response.statusText);
                }
                window.location.reload();
            } catch (error) {
                renderStatus(error.message || "Toggle failed", "error");
                button.disabled = false;
            }
        });
    });

    async function parseJson(response) {
        try {
            return await response.json();
        } catch (error) {
            return {error: response.statusText};
        }
    }

    function wait(ms) {
        return new Promise((resolve) => window.setTimeout(resolve, ms));
    }
});
