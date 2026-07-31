import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import test from "node:test";
import {fileURLToPath} from "node:url";

const testDirectory = dirname(fileURLToPath(import.meta.url));
const scriptPath = resolve(
    testDirectory,
    "../../../../../app/static/script.js"
);
const source = readFileSync(scriptPath, "utf8");

function between(startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.notEqual(start, -1, `Missing marker: ${startMarker}`);
    assert.notEqual(end, -1, `Missing marker: ${endMarker}`);
    return source.slice(start, end);
}

test("knowledge-base switching preserves transcript and conversation memory", () => {
    const handler = between(
        "function commitKnowledgeBaseSelection(",
        "function renderKnowledgeBaseSelection("
    );

    assert.match(handler, /persistKnowledgeBaseIds\(knowledgeBaseIds\)/);
    assert.match(handler, /appendKnowledgeBaseNotice\(/);
    assert.doesNotMatch(handler, /createConversationId\(/);
    assert.doesNotMatch(handler, /clearServerConversation\(/);
    assert.doesNotMatch(handler, /chatbox\.replaceChildren/);
});

test("streaming knowledge-base failures preserve the rendered error and revalidate catalog", () => {
    const streamHandler = between(
        "function handleStreamEvent(",
        "async function waitForKnowledgeBaseRecovery("
    );
    const streamReader = between(
        "async function renderStreamingResponse(",
        "function parseNdjsonLines("
    );
    const interpreterReader = between(
        "async function renderCodeInterpreterStream(",
        "function renderCodeInterpreterPayload("
    );
    const renderIndex = streamHandler.indexOf(
        'renderBotAnswer(messageDiv, formatError(event, "Streaming interrupted"))'
    );
    const fallbackIndex = streamHandler.indexOf(
        "state.recoveryPromise = handleUnavailableKnowledgeBase("
    );
    const fallbackHandler = between(
        "async function handleUnavailableKnowledgeBase(",
        "async function handleFileUpload("
    );

    assert.ok(renderIndex >= 0);
    assert.ok(fallbackIndex > renderIndex);
    assert.match(streamReader, /await waitForKnowledgeBaseRecovery\(state\)/);
    assert.match(interpreterReader, /await waitForKnowledgeBaseRecovery\(state\)/);
    assert.match(fallbackHandler, /await loadKnowledgeBases\(\)/);
    assert.match(fallbackHandler, /appendKnowledgeBaseNotice\(/);
    assert.doesNotMatch(fallbackHandler, /resetKnowledgeBaseConversation/);
    assert.doesNotMatch(fallbackHandler, /chatbox\.replaceChildren/);
    assert.equal(
        source.match(/state\.recoveryPromise = handleUnavailableKnowledgeBase\(/g)?.length,
        2
    );
});

test("markdown rendering fails closed when the sanitizer is unavailable", () => {
    const rendererSource = between(
        "function renderSafeMarkdown(",
        "function updateChatStatus("
    );
    const buildRenderer = new Function(
        "window",
        "DOMPurify",
        "marked",
        "escapeHtml",
        `${rendererSource}\nreturn renderSafeMarkdown;`
    );
    const malicious = '<img src=x onerror="alert(1)">';
    const fallback = buildRenderer(
        {marked: {}, DOMPurify: undefined},
        undefined,
        {parse: value => value},
        value => `escaped:${value}`
    );
    const sanitized = buildRenderer(
        {marked: {}, DOMPurify: {}},
        {sanitize: value => `safe:${value}`},
        {parse: value => `html:${value}`},
        value => `escaped:${value}`
    );

    assert.equal(fallback(malicious), `escaped:${malicious}`);
    assert.equal(sanitized("answer"), "safe:html:answer");
    const interpreterRenderer = between(
        "function renderCodeInterpreterPayload(",
        "function setBusy("
    );
    assert.match(interpreterRenderer, /renderSafeMarkdown\(content\)/);
});

test("multi-KB UI normalizes storage and protects in-flight conversation state", () => {
    const normalizerSource = between(
        "function normalizeKnowledgeBaseIds(",
        "function loadKnowledgeBaseIds("
    );
    const normalize = new Function(
        `${normalizerSource}\nreturn normalizeKnowledgeBaseIds;`
    )();
    const busyHandler = between("function setBusy(", "function createAskTimeout(");
    const clearHandler = between("function clearChat(", "function loadOrCreateConversationId(");
    const chipRenderer = between(
        "function renderKnowledgeBaseSelection(",
        "function knowledgeBaseSelectionLabel("
    );

    assert.deepEqual(
        normalize(["default", " default ", "", "kb_123", "kb_123", null]),
        ["default", "kb_123"]
    );
    assert.match(busyHandler, /clearChatButton\.disabled = isBusy/);
    assert.match(clearHandler, /if \(busy\) return/);
    assert.match(chipRenderer, /focusKnowledgeBaseControlAfterRemoval\(index\)/);
});
