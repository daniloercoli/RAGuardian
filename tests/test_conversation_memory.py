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
    assert stored.startswith(b'{"schema_version":1')
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


def test_redis_conversation_memory_clear_by_prefix_removes_states_and_locks():
    redis = FakeRedis()
    store = RedisConversationMemoryStore(
        redis_client=redis,
        key_prefix="test:conversation",
    )
    store.append_turn("workspace-a:conversation-1", user="A", assistant="A1")
    store.append_turn("workspace-a:conversation-2", user="B", assistant="B1")
    store.append_turn("workspace-b:conversation-1", user="C", assistant="C1")
    redis.data["test:conversation:lock:workspace-a:conversation-1"] = b"locked"

    assert store.clear_by_prefix("workspace-a:") == 2
    assert store.render_for_prompt("workspace-a:conversation-1") == ""
    assert store.render_for_prompt("workspace-a:conversation-2") == ""
    assert "C" in store.render_for_prompt("workspace-b:conversation-1")
    assert "test:conversation:lock:workspace-a:conversation-1" not in redis.data


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.data:
                deleted += 1
                del self.data[key]
        return deleted

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.data):
            if not match or key.startswith(prefix):
                yield key

    def lock(self, *_args, **_kwargs):
        return FakeLock()


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
