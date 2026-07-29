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

test("knowledge-base switching clears the old transcript before remote cleanup", () => {
    const handler = between(
        "function handleKnowledgeBaseChange()",
        "function loadKnowledgeBaseId()"
    );
    const resetIndex = handler.indexOf(
        "resetKnowledgeBaseConversation(nextKnowledgeBaseId)"
    );
    const cleanupIndex = handler.indexOf("clearServerConversation(");

    assert.ok(resetIndex >= 0);
    assert.ok(cleanupIndex > resetIndex);
    assert.doesNotMatch(handler, /await\s+clearServerConversation/);
});

test("streaming knowledge-base failures survive the fallback reset", () => {
    const streamHandler = between(
        "function handleStreamEvent(",
        "function appendMessage("
    );
    const renderIndex = streamHandler.indexOf(
        'renderBotAnswer(messageDiv, formatError(event, "Streaming interrupted"))'
    );
    const fallbackIndex = streamHandler.indexOf(
        "handleUnavailableKnowledgeBase(event, messageDiv)"
    );
    const fallbackHandler = between(
        "async function handleUnavailableKnowledgeBase(",
        "function resetKnowledgeBaseConversation("
    );
    const resetHandler = between(
        "function resetKnowledgeBaseConversation(",
        "async function handleFileUpload("
    );

    assert.ok(renderIndex >= 0);
    assert.ok(fallbackIndex > renderIndex);
    assert.match(
        fallbackHandler,
        /resetKnowledgeBaseConversation\("default", preservedMessage\)/
    );
    assert.match(resetHandler, /chatbox\.appendChild\(preservedMessage\)/);
    assert.equal(
        source.match(/handleUnavailableKnowledgeBase\(event, messageDiv\)/g)?.length,
        2
    );
});
