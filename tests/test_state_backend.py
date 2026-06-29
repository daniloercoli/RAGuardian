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

