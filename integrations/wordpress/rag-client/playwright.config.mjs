import {defineConfig} from "@playwright/test";

export default defineConfig({
    testDir: "./tests/e2e",
    timeout: 60000,
    expect: {
        timeout: 10000,
    },
    use: {
        baseURL: process.env.WP_BASE_URL || "http://localhost:8888",
        trace: "retain-on-failure",
        screenshot: "only-on-failure",
        video: "retain-on-failure",
    },
    webServer: {
        command: "node tests/support/fake-rag-server.mjs",
        url: process.env.FAKE_RAG_INSPECT_URL || "http://127.0.0.1:5055/__health",
        reuseExistingServer: true,
        timeout: 10000,
    },
});
