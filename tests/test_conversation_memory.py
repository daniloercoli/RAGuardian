import json
import time
from fnmatch import fnmatchcase

from app.utils.conversation_memory import ConversationMemoryStore, RedisConversationMemoryStore


def test_conversation_memory_returns_summary_job_after_threshold():
    store = ConversationMemoryStore(summary_threshold_chars=260, recent_turns_to_keep=1)
    conversation_id = "conv-12345678"

    first_job = store.append_turn(
        conversation_id,
        user="Prima domanda " + "u" * 50,
        assistant="Prima risposta " + "a" * 50,
    )
    second_job = store.append_turn(
        conversation_id,
        user="Seconda domanda " + "u" * 50,
        assistant="Seconda risposta " + "a" * 50,
    )

    assert first_job is None
    assert second_job is not None
    assert len(second_job.turns_to_summarize) == 1
    assert len(second_job.recent_turns) == 1

    applied = store.apply_summary(second_job, "Riassunto operativo della prima parte.")

    assert applied is True
    prompt_context = store.render_for_prompt(conversation_id)
    assert "Riassunto operativo della prima parte." in prompt_context
    assert "Seconda domanda" in prompt_context
    assert "Prima domanda" not in prompt_context


def test_redis_conversation_memory_uses_shared_backend():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
        summary_threshold_chars=260,
        recent_turns_to_keep=1,
    )
    conversation_id = "conv-12345678"

    first_job = store.append_turn(
        conversation_id,
        user="Prima domanda " + "u" * 50,
        assistant="Prima risposta " + "a" * 50,
    )
    second_job = store.append_turn(
        conversation_id,
        user="Seconda domanda " + "u" * 50,
        assistant="Seconda risposta " + "a" * 50,
    )

    assert first_job is None
    assert second_job is not None
    assert store.apply_summary(second_job, "Riassunto Redis.") is True

    prompt_context = store.render_for_prompt(conversation_id)
    assert "Riassunto Redis." in prompt_context
    assert "Seconda domanda" in prompt_context
    stored = redis.data["test:conversation:conv-12345678"]
    assert stored.startswith(b'{"schema_version":2')
    assert store.clear(conversation_id) is True
    assert store.render_for_prompt(conversation_id) == ""


def test_conversation_memory_clear_by_prefix_is_workspace_scoped():
    store = ConversationMemoryStore()
    store.append_turn("workspace-a:conversation-1", user="A", assistant="A1")
    store.append_turn("workspace-a:conversation-2", user="B", assistant="B1")
    store.append_turn("workspace-b:conversation-1", user="C", assistant="C1")

    assert store.clear_by_prefix("workspace-a:") == 2
    assert store.render_for_prompt("workspace-a:conversation-1") == ""
    assert store.render_for_prompt("workspace-a:conversation-2") == ""
    assert "C" in store.render_for_prompt("workspace-b:conversation-1")


def test_conversation_memory_tracks_kb_union_and_clears_federated_chat():
    store = ConversationMemoryStore()
    conversation_id = "workspace-a:multi-chat:conversation-1"
    store.append_turn(
        conversation_id,
        user="A",
        assistant="A1",
        knowledge_base_ids=["default", "kb_legal"],
    )
    store.append_turn(
        conversation_id,
        user="B",
        assistant="B1",
        knowledge_base_ids=["kb_finance"],
    )

    assert store.knowledge_base_ids(conversation_id) == {
        "default",
        "kb_legal",
        "kb_finance",
    }
    assert store.clear_by_knowledge_base("workspace-a", "kb_legal") == 1
    assert store.render_for_prompt(conversation_id) == ""


def test_conversation_snapshot_is_immutable_after_a_concurrent_append():
    store = ConversationMemoryStore()
    conversation_id = "workspace-a:multi-chat:conversation-snapshot"
    store.append_turn(
        conversation_id,
        user="Domanda iniziale",
        assistant="Risposta iniziale",
        knowledge_base_ids=["default"],
    )

    snapshot = store.snapshot(conversation_id)
    store.append_turn(
        conversation_id,
        user="Domanda successiva",
        assistant="Risposta successiva",
        knowledge_base_ids=["kb_secret"],
    )

    assert snapshot.version == 1
    assert snapshot.knowledge_base_ids == frozenset({"default"})
    assert "Risposta iniziale" in snapshot.prompt_context
    assert "Risposta successiva" not in snapshot.prompt_context
    assert store.clear_if_version(conversation_id, snapshot.version) is False
    assert "Risposta successiva" in store.snapshot(
        conversation_id
    ).prompt_context


def test_conversation_clear_if_version_removes_only_the_current_state():
    store = ConversationMemoryStore()
    conversation_id = "workspace-a:multi-chat:conditional-clear"
    store.append_turn(
        conversation_id,
        user="Domanda",
        assistant="Risposta",
        knowledge_base_ids=["default"],
    )

    snapshot = store.snapshot(conversation_id)

    assert store.clear_if_version(conversation_id, snapshot.version) is True
    assert store.snapshot(conversation_id).version is None


def test_redis_clear_if_version_preserves_a_concurrent_append_and_markers():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
    )
    conversation_id = "workspace-a:multi-chat:conditional-clear"
    store.append_turn(
        conversation_id,
        user="Domanda iniziale",
        assistant="Risposta iniziale",
        knowledge_base_ids=["kb_legal"],
    )
    snapshot = store.snapshot(conversation_id)
    store.append_turn(
        conversation_id,
        user="Domanda concorrente",
        assistant="Risposta concorrente",
        knowledge_base_ids=["kb_finance"],
    )

    assert store.clear_if_version(conversation_id, snapshot.version) is False
    assert "Risposta concorrente" in store.render_for_prompt(conversation_id)
    assert any(
        key.startswith("test:conversation:kb-membership:workspace-a:")
        and conversation_id in key
        for key in redis.data
    )

    current_snapshot = store.snapshot(conversation_id)
    assert (
        store.clear_if_version(
            conversation_id,
            current_snapshot.version,
        )
        is True
    )
    assert store.render_for_prompt(conversation_id) == ""
    assert not any(conversation_id in key for key in redis.data)


def test_redis_conversation_memory_clear_by_prefix_removes_states_and_locks():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
    )
    store.append_turn(
        "workspace-a:conversation-1",
        user="A",
        assistant="A1",
        knowledge_base_ids=["kb_legal"],
    )
    store.append_turn(
        "workspace-a:conversation-2",
        user="B",
        assistant="B1",
        knowledge_base_ids=["kb_finance"],
    )
    store.append_turn(
        "workspace-b:conversation-1",
        user="C",
        assistant="C1",
        knowledge_base_ids=["kb_legal"],
    )
    redis.data["test:conversation:lock:workspace-a:conversation-1"] = b"locked"

    assert store.clear_by_prefix("workspace-a:") == 2
    assert store.render_for_prompt("workspace-a:conversation-1") == ""
    assert store.render_for_prompt("workspace-a:conversation-2") == ""
    assert "C" in store.render_for_prompt("workspace-b:conversation-1")
    assert "test:conversation:lock:workspace-a:conversation-1" not in redis.data
    assert not any(
        key.startswith("test:conversation:kb-membership:workspace-a:")
        for key in redis.data
    )
    assert any(
        key.startswith("test:conversation:kb-membership:workspace-b:")
        for key in redis.data
    )


def test_redis_conversation_memory_uses_independent_expiring_kb_markers():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
        ttl_seconds=60,
    )
    first_id = "workspace-a:multi-chat:conversation-1"
    second_id = "workspace-a:multi-chat:conversation-2"

    store.append_turn(
        first_id,
        user="A",
        assistant="A1",
        knowledge_base_ids=["kb_legal"],
    )
    store.append_turn(
        second_id,
        user="B",
        assistant="B1",
        knowledge_base_ids=["kb_legal"],
    )

    first_marker = (
        "test:conversation:kb-membership:workspace-a:kb_legal:"
        f"{first_id}"
    )
    second_marker = (
        "test:conversation:kb-membership:workspace-a:kb_legal:"
        f"{second_id}"
    )
    assert redis.data[first_marker] == b"1"
    assert redis.data[second_marker] == b"1"
    assert redis.ttls[first_marker] == 60
    assert redis.ttls[second_marker] == 60
    assert store.clear(first_id) is True
    assert first_marker not in redis.data
    assert second_marker in redis.data


def test_redis_conversation_memory_expiry_removes_all_kb_markers():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
        ttl_seconds=60,
    )
    conversation_id = "workspace-a:multi-chat:conversation-1"
    store.append_turn(
        conversation_id,
        user="A",
        assistant="A1",
        knowledge_base_ids=["kb_legal", "kb_finance"],
    )
    state_key = f"test:conversation:{conversation_id}"
    payload = json.loads(redis.data[state_key])
    payload["updated_at"] = time.time() - 120
    redis.data[state_key] = json.dumps(payload).encode("utf-8")

    assert store.render_for_prompt(conversation_id) == ""
    assert state_key not in redis.data
    assert not any(
        key.startswith("test:conversation:kb-membership:workspace-a:")
        for key in redis.data
    )


def test_redis_conversation_memory_clear_by_kb_removes_federated_state_and_markers():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
    )
    conversation_id = "workspace-a:multi-chat:conversation-1"
    store.append_turn(
        conversation_id,
        user="A",
        assistant="A1",
        knowledge_base_ids=["kb_legal", "kb_finance"],
    )

    assert store.clear_by_knowledge_base("workspace-a", "kb_legal") == 1
    assert store.render_for_prompt(conversation_id) == ""
    assert not any(conversation_id in key for key in redis.data)


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttls[key] = ttl

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.data:
                deleted += 1
                del self.data[key]
                self.ttls.pop(key, None)
        return deleted

    def scan_iter(self, match=None, count=None):
        for key in list(self.data):
            if not match or fnmatchcase(key, match):
                yield key

    def lock(self, *_args, **_kwargs):
        return FakeLock()


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
