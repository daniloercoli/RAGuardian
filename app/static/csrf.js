(function () {
    "use strict";

    const meta = document.querySelector("meta[name='csrf-token']");
    const token = meta ? meta.content : "";
    if (!token) return;

    const originalFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
        const options = Object.assign({}, init || {});
        const method = String(options.method || (typeof input !== "string" ? input.method : "GET") || "GET").toUpperCase();
        const url = new URL(typeof input === "string" ? input : input.url, window.location.href);
        if (url.origin === window.location.origin && !["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
            const headers = new Headers(options.headers || (typeof input !== "string" ? input.headers : undefined));
            headers.set("X-CSRF-Token", token);
            options.headers = headers;
        }
        return originalFetch(input, options);
    };

    document.addEventListener("DOMContentLoaded", function () {
        document.querySelectorAll("form").forEach(function (form) {
            if (String(form.method || "GET").toUpperCase() !== "POST" || form.querySelector("input[name='csrf_token']")) {
                return;
            }
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "csrf_token";
            input.value = token;
            form.appendChild(input);
        });
    });
})();
