import os

import pytest
from langchain_core.documents import Document


REDIS_INTEGRATION_URL = os.getenv("RAG_REDIS_INTEGRATION_URL", "")
pytestmark = pytest.mark.skipif(
    not REDIS_INTEGRATION_URL,
    reason="Set RAG_REDIS_INTEGRATION_URL to run real Redis integration tests",
)


def test_real_redis_state_backends(monkeypatch):
    import redis

    from app.app import RateLimiter
    from app.utils import state_backend
    from app.utils.cache import RAGCache
    from app.utils.conversation_memory import RedisConversationMemoryStore
    from app.utils.index_lock import index_write_lock
    from app.utils.job_store import RedisJobStore

    monkeypatch.setenv("REDIS_URL", REDIS_INTEGRATION_URL)
    monkeypatch.setenv("RAG_STATE_BACKEND", "redis")
    monkeypatch.setenv("RAG_QUEUE_BACKEND", "redis")
    monkeypatch.setenv("RAG_STATE_PREFIX", "rag-integration-test")
    state_backend.reset_redis_pools()
    client = redis.Redis.from_url(REDIS_INTEGRATION_URL, decode_responses=False)
    client.flushdb()

    try:
        assert client.ping() is True
        status = state_backend.runtime_state_status()
        assert status["redis_ready"] is True
        assert status["queue_ready"] is True

        RAGCache.reset()
        cache = RAGCache()
        documents = [Document(page_content="Redis context", metadata={"source": "redis.pdf"})]
        cache.set("real redis query", documents, k=1, model="integration-model")
        restored = cache.get("real redis query", k=1, model="integration-model")
        assert restored[0].page_content == "Redis context"
        cache_payloads = list(client.scan_iter(match="rag-integration-test:cache:*"))
        assert cache_payloads
        assert client.get(cache_payloads[0]).startswith(b'{"schema_version":1')

        conversations = RedisConversationMemoryStore(
            redis_client=client,
            key_prefix="rag-integration-test:conversation",
        )
        conversations.append_turn("conversation-1", user="Question", assistant="Answer")
        assert "Question" in conversations.render_for_prompt("conversation-1")
        assert client.get("rag-integration-test:conversation:conversation-1").startswith(
            b'{"schema_version":1'
        )

        jobs = RedisJobStore(redis_client=client)
        payload, status_code = jobs.create_job(
            {
                "id": "job-1",
                "type": "integration",
                "status": "running",
                "errors": [],
            }
        )
        assert status_code == 202
        assert payload["id"] == "job-1"
        jobs.finish("job-1", "completed", "done")
        assert jobs.get("job-1")["status"] == "completed"

        limiter = RateLimiter(max_requests=1, window_seconds=60)
        assert limiter.backend == "redis"
        assert limiter.is_allowed("127.0.0.1")[0] is True
        assert limiter.is_allowed("127.0.0.1")[0] is False

        with index_write_lock():
            assert client.get("rag-integration-test:lock:index-write") is not None
    finally:
        client.flushdb()
        RAGCache.reset()
        state_backend.reset_redis_pools()
