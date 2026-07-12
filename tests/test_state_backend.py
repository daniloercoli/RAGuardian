import sys
import types

from langchain_core.documents import Document

from app.utils import state_backend
from app.utils.cache import _deserialize_documents, _serialize_documents
from app.utils.state_backend import configured_queue_backend, configured_state_backend, runtime_state_status


def test_state_backend_defaults_to_memory_and_inline(monkeypatch):
    monkeypatch.delenv("RAG_STATE_BACKEND", raising=False)
    monkeypatch.delenv("RAG_QUEUE_BACKEND", raising=False)

    assert configured_state_backend() == "memory"
    assert configured_queue_backend() == "inline"


def test_state_backend_ignores_unsupported_values(monkeypatch):
    monkeypatch.setenv("RAG_STATE_BACKEND", "unsupported")
    monkeypatch.setenv("RAG_QUEUE_BACKEND", "unsupported")

    assert configured_state_backend() == "memory"
    assert configured_queue_backend() == "inline"


def test_runtime_state_status_reports_memory_ready(monkeypatch):
    monkeypatch.setenv("RAG_STATE_BACKEND", "memory")
    monkeypatch.setenv("RAG_QUEUE_BACKEND", "inline")

    status = runtime_state_status(active_jobs_count=2, queue_depth=0)

    assert status == {
        "state_backend": "memory",
        "queue_backend": "inline",
        "redis_ready": True,
        "queue_ready": True,
        "queue_depth": 0,
        "active_jobs_count": 2,
    }


def test_redis_connection_reuses_connection_pool(monkeypatch):
    created_pools = []

    class FakeConnectionPool:
        @classmethod
        def from_url(cls, *args, **kwargs):
            pool = types.SimpleNamespace(args=args, kwargs=kwargs, disconnected=False)
            pool.disconnect = lambda: setattr(pool, "disconnected", True)
            created_pools.append(pool)
            return pool

    class FakeRedisClient:
        def __init__(self, connection_pool):
            self.connection_pool = connection_pool

    fake_redis = types.SimpleNamespace(
        ConnectionPool=FakeConnectionPool,
        Redis=FakeRedisClient,
    )
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    state_backend.reset_redis_pools()

    first = state_backend.redis_connection()
    second = state_backend.redis_connection()

    assert first.connection_pool is second.connection_pool
    assert len(created_pools) == 1

    state_backend.reset_redis_pools()
    assert created_pools[0].disconnected is True


def test_redis_cache_document_payload_uses_json_not_pickle():
    raw = _serialize_documents(
        [Document(page_content="context", metadata={"source": "demo.pdf", "page": 1})]
    )

    assert raw.startswith(b'{"schema_version":1')
    restored = _deserialize_documents(raw)
    assert restored[0].page_content == "context"
    assert restored[0].metadata == {"source": "demo.pdf", "page": 1}
