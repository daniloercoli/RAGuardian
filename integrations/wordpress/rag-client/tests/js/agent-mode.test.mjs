import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {dirname, resolve} from "node:path";
import test from "node:test";
import {fileURLToPath} from "node:url";

const testDirectory = dirname(fileURLToPath(import.meta.url));
const publicScript = readFileSync(
    resolve(testDirectory, "../../assets/rag-client.js"),
    "utf8"
);
const adminScript = readFileSync(
    resolve(testDirectory, "../../assets/rag-admin-agents.js"),
    "utf8"
);

test("public Agent mode fails closed and never offers an implicit Custom fallback", () => {
    assert.match(publicScript, /if \(config\.agentMode && !config\.agentId\)/);
    assert.match(publicScript, /Choose an assistant before sending a message/);
    assert.match(publicScript, /if \(config\.agentId\) payload\.agent_id = config\.agentId/);
    assert.doesNotMatch(publicScript, /payload\.knowledge_base_id/);
    assert.doesNotMatch(publicScript, /Custom Chat/);
});

test("switching Agent rotates the conversation and clears transcript state", () => {
    assert.match(publicScript, /function conversationStorageKey\(agentId\)/);
    assert.match(publicScript, /rotateConversationId\(config\.agentId\)/);
    assert.match(publicScript, /transcript\.splice\(0, transcript\.length\)/);
    assert.match(publicScript, /messages\.replaceChildren\(\)/);
});

test("catalog parsing is fail-safe and admin CRUD uses nonce-protected AJAX actions", () => {
    assert.match(publicScript, /try \{[\s\S]*JSON\.parse\(chat\.dataset\.ecRagAgentsCatalog/);
    assert.match(publicScript, /catch \(_error\) \{[\s\S]*agentsCatalog = \[\]/);
    assert.match(adminScript, /body\.set\("nonce", root\.dataset\.nonce/);
    assert.match(adminScript, /ec_rag_create_agent/);
    assert.match(adminScript, /ec_rag_update_agent/);
    assert.match(adminScript, /ec_rag_delete_agent/);
});
