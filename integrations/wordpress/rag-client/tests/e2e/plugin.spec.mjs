import {execFileSync} from "node:child_process";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {expect, test} from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "../..");
const fakeRagInspectUrl = process.env.FAKE_RAG_INSPECT_URL || "http://127.0.0.1:5055";
const wpRagBaseUrl = process.env.WP_RAG_BASE_URL || "http://host.docker.internal:5055";
const wpEnvBin = path.join(pluginRoot, "node_modules", ".bin", "wp-env");

async function fakeRagRequests() {
    const response = await fetch(`${fakeRagInspectUrl}/__requests`);
    const payload = await response.json();
    return payload.requests || [];
}

async function resetFakeRag() {
    await fetch(`${fakeRagInspectUrl}/__reset`, {method: "POST"});
}

async function waitForFakeRag(predicate) {
    for (let attempt = 0; attempt < 40; attempt++) {
        const requests = await fakeRagRequests();
        if (predicate(requests)) return requests;
        await new Promise((resolve) => setTimeout(resolve, 500));
    }
    return fakeRagRequests();
}

async function authenticateAsAdmin(page) {
    const expires = Math.floor(Date.now() / 1000) + 3600;
    const script = [
        `$expires = ${expires};`,
        `$user_id = 1;`,
        `echo LOGGED_IN_COOKIE . "\\t" . wp_generate_auth_cookie($user_id, $expires, "logged_in") . "\\n";`,
        `echo AUTH_COOKIE . "\\t" . wp_generate_auth_cookie($user_id, $expires, "auth") . "\\n";`,
    ].join(" ");
    const cookies = wpCli(["eval", script])
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
            const [name, value] = line.split("\t");
            return {
                name,
                value,
                url: "http://localhost:8888",
                expires,
                httpOnly: true,
                sameSite: "Lax",
            };
        });

    await page.context().addCookies(cookies);
    await page.goto("/wp-admin/", {waitUntil: "domcontentloaded"});
    await expect(page.locator("#adminmenu")).toBeVisible();
}

async function configurePlugin(page, options = {}) {
    await authenticateAsAdmin(page);
    await page.goto("/wp-admin/options-general.php?page=ec-rag-client");
    await page.fill("#ec-rag-base-url", wpRagBaseUrl);
    await page.fill("#ec-rag-api-key", "wp-e2e-key");
    await setCheckbox(page, 'input[name="ec_rag_client_options[enable_global_widget]"]', true);
    await setCheckbox(page, 'input[name="ec_rag_client_options[allow_guest_chat]"]', options.allowGuest ?? true);
    await setCheckbox(page, 'input[name="ec_rag_client_options[ingest_public_posts]"]', options.liveIngestion ?? true);
    await setCheckbox(page, 'input[name="ec_rag_client_options[enable_tts]"]', options.tts ?? false);
    await setCheckbox(page, 'input[name="ec_rag_client_options[enable_audio_upload]"]', options.audio ?? false);
    await setCheckbox(page, 'input[name="ec_rag_client_options[show_sources]"]', options.showSources ?? true);
    await page.fill(
        'input[name="ec_rag_client_options[rate_limit_requests]"]',
        String(options.rateLimit ?? 10),
    );
    await page.fill(
        'input[name="ec_rag_client_options[rate_limit_window]"]',
        String(options.rateLimitWindow ?? 60),
    );
    await page.click("#submit");
    await expect(page.locator(".notice-success, #setting-error-settings_updated")).toBeVisible();
}

async function setCheckbox(page, selector, checked) {
    const locator = page.locator(selector);
    if ((await locator.isChecked()) !== checked) {
        await locator.click();
    }
}

function wpCli(args) {
    return execFileSync(wpEnvBin, ["run", "cli", "wp", ...args], {
        cwd: pluginRoot,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "pipe"],
    }).trim();
}

test.beforeEach(async () => {
    await resetFakeRag();
    wpCli(["transient", "delete", "--all"]);
});

test.beforeAll(() => {
    wpCli(["user", "update", "admin", "--user_pass=password"]);
    wpCli(["plugin", "activate", "rag-client"]);
});

test("admin can configure the plugin and test RAG health", async ({page}) => {
    await configurePlugin(page);
    const healthHref = await page.locator('a.button[href*="ec_rag_test=1"]').getAttribute("href");
    await page.goto(healthHref);
    await expect(page.getByText(/RAGuardian connection OK/i)).toBeVisible();

    const requests = await fakeRagRequests();
    expect(requests.some((request) => request.type === "health" && request.apiKey === "wp-e2e-key")).toBe(true);
});

test("guest visitor can use the chatbot without seeing the API key", async ({page}) => {
    await configurePlugin(page, {allowGuest: true});
    await page.context().clearCookies();
    await page.goto("/?reset_token=must-not-leave-wordpress");
    await page.locator(".ec-rag-launcher").click({force: true});
    await page.locator("[data-ec-rag-input]").fill("What is in the knowledge base?");
    await page.locator(".ec-rag-form button[type='submit']").click({force: true});
    await expect(page.getByText(/Fake RAG answer for:/i)).toBeVisible();

    const pageHtml = await page.content();
    expect(pageHtml).not.toContain("wp-e2e-key");
    const requests = await fakeRagRequests();
    const queryRequest = requests.find(
        (request) => request.type === "query" && request.body.query === "What is in the knowledge base?",
    );
    expect(queryRequest).toBeTruthy();
    expect(queryRequest.body.client_context.page_url).not.toContain("reset_token");
});

test("disabled sources are removed from the AJAX response", async ({page}) => {
    await configurePlugin(page, {allowGuest: true, showSources: false});
    await page.context().clearCookies();
    await page.goto("/");
    await page.locator(".ec-rag-launcher").click({force: true});
    await page.locator("[data-ec-rag-input]").fill("Hide the source metadata");

    const responsePromise = page.waitForResponse((response) => (
        response.url().includes("/wp-admin/admin-ajax.php")
        && (response.request().postData() || "").includes("action=ec_rag_query")
    ));
    await page.locator(".ec-rag-form button[type='submit']").click({force: true});

    const ajaxResponse = await responsePromise;
    const payload = await ajaxResponse.json();
    expect(payload.success).toBe(true);
    expect(payload.data.sources).toBeUndefined();
    await expect(page.locator(".ec-rag-sources")).toHaveCount(0);
});

test("rotating conversation IDs does not bypass guest rate limits", async ({page}) => {
    await configurePlugin(page, {allowGuest: true, rateLimit: 1});
    await page.context().clearCookies();
    await page.goto("/");
    const nonce = await page.evaluate(() => window.ecRagClient.nonce);
    const endpoint = "/wp-admin/admin-ajax.php";

    const first = await page.request.post(endpoint, {
        form: {
            action: "ec_rag_query",
            nonce,
            query: "First allowed request",
            conversation_id: "conversation-one",
        },
    });
    expect(first.status()).toBe(200);

    const second = await page.request.post(endpoint, {
        form: {
            action: "ec_rag_query",
            nonce,
            query: "Second blocked request",
            conversation_id: "conversation-two",
        },
    });
    expect(second.status()).toBe(429);
    expect((await second.json()).success).toBe(false);
});

test("guest TTS and audio upload proxy through WordPress", async ({page}) => {
    await configurePlugin(page, {allowGuest: true, tts: true, audio: true});
    await page.context().clearCookies();
    await page.goto("/");
    await page.locator(".ec-rag-launcher").click({force: true});
    await page.locator("[data-ec-rag-input]").fill("Read this answer aloud");
    await page.locator(".ec-rag-form button[type='submit']").click({force: true});
    await expect(page.getByText(/Fake RAG answer for:/i)).toBeVisible();

    await page.getByRole("button", {name: "Listen"}).click({force: true});
    await waitForFakeRag((entries) => entries.some((entry) => entry.type === "tts"));

    await page.locator("[data-ec-rag-audio]").setInputFiles({
        name: "sample.wav",
        mimeType: "audio/wav",
        buffer: Buffer.from("RIFF-fake-wave-data"),
    });
    await expect(page.getByText(/Audio queued for transcription/i)).toBeVisible();

    const requests = await fakeRagRequests();
    expect(requests.some((entry) => entry.type === "tts")).toBe(true);
    expect(requests.some((entry) => (
        entry.type === "audio_upload"
        && entry.multipart.file.filename === "sample.wav"
    ))).toBe(true);
});

test("initial WXR import queues only public articles", async ({page}) => {
    await configurePlugin(page);
    await page.goto("/wp-admin/options-general.php?page=ec-rag-client");
    await page.setInputFiles('input[name="ec_rag_wxr"]', path.join(pluginRoot, "tests", "fixtures", "wp-export-public-posts.xml"));
    await page.locator('input[type="submit"][value="Upload and queue public articles"]').click({force: true});
    await expect(page.getByText(/public articles found/i)).toBeVisible();
    await page.locator('input[type="submit"][value="Process next batch now"]').click({force: true});

    const requests = await waitForFakeRag((entries) => entries.some((entry) => entry.type === "file_upload"));
    const uploads = requests.filter((request) => request.type === "file_upload");
    expect(uploads).toHaveLength(1);
    expect(uploads[0].multipart.fields.relative_path).toBe("wordpress/posts/post-101.txt");
    expect(uploads[0].multipart.file.text).toContain("Public Article One");
    expect(uploads[0].multipart.file.text).not.toContain("Draft Article");
    expect(uploads[0].multipart.file.text).not.toContain("Password Protected Article");

    const state = JSON.parse(wpCli(["option", "get", "ec_rag_client_import_state", "--format=json"]));
    expect(state.status).toBe("completed");
    expect(state.queue_path).toBe("");
});

test("live publish and unpublish hooks sync public posts", async ({page}) => {
    await configurePlugin(page, {liveIngestion: true});
    await resetFakeRag();

    const postId = wpCli([
        "post",
        "create",
        "--post_type=post",
        "--post_status=publish",
        "--post_title=E2E Live Article",
        "--post_content=Live article body for RAG.",
        "--porcelain",
    ]);
    wpCli(["cron", "event", "run", "ec_rag_sync_post"]);

    let requests = await waitForFakeRag((entries) => entries.some((entry) => entry.type === "file_upload"));
    expect(requests.some((entry) => entry.type === "file_upload" && entry.multipart.fields.relative_path === `wordpress/posts/post-${postId}.txt`)).toBe(true);

    await resetFakeRag();
    wpCli(["post", "update", postId, "--post_status=draft"]);
    wpCli(["cron", "event", "run", "ec_rag_sync_post"]);
    requests = await waitForFakeRag((entries) => entries.some((entry) => entry.type === "file_delete"));
    expect(requests.some((entry) => entry.type === "file_delete" && entry.filename === `wordpress/posts/post-${postId}.txt`)).toBe(true);
});
