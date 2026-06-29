import os
from typing import Any


SUPPORTED_STATE_BACKENDS = {"memory", "redis"}
SUPPORTED_QUEUE_BACKENDS = {"inline", "redis"}


class StateBackendError(RuntimeError):
    pass


def configured_state_backend() -> str:
    return _configured_backend("RAG_STATE_BACKEND", "memory", SUPPORTED_STATE_BACKENDS)


def configured_queue_backend() -> str:
    return _configured_backend("RAG_QUEUE_BACKEND", "inline", SUPPORTED_QUEUE_BACKENDS)


def state_key_prefix() -> str:
    raw = os.getenv("RAG_STATE_PREFIX", "rag")
    return "".join(char if char.isalnum() or char in ":-_" else "-" for char in raw).strip(":-_") or "rag"


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def redis_connection():
    try:
        import redis
    except ImportError as exc:
        raise StateBackendError("redis package is not installed") from exc

    return redis.Redis.from_url(
        redis_url(),
        decode_responses=False,
        socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT", "1.0")),
        socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT", "1.0")),
    )


def redis_scan_delete(client: Any, pattern: str) -> int:
    deleted = 0
    batch = []
    for key in client.scan_iter(match=pattern, count=200):
        batch.append(key)
        if len(batch) >= 200:
            deleted += int(client.delete(*batch) or 0)
            batch = []
    if batch:
        deleted += int(client.delete(*batch) or 0)
    return deleted


def runtime_state_status(*, active_jobs_count: int = 0, queue_depth: int = 0) -> dict:
    state_backend = configured_state_backend()
    queue_backend = configured_queue_backend()
    redis_required = state_backend == "redis" or queue_backend == "redis"
    redis_ready = True
    redis_error = ""
    queue_ready = queue_backend == "inline"
    queue_error = ""

    if redis_required:
        try:
            redis_client = redis_connection()
            redis_client.ping()
        except Exception as exc:
            redis_ready = False
            redis_error = str(exc)
        else:
            if queue_backend == "redis":
                try:
                    from rq import Queue

                    queue_depth = len(Queue(_queue_name(), connection=redis_client))
                    queue_ready = True
                except Exception as exc:
                    queue_ready = False
                    queue_error = str(exc)

    payload = {
        "state_backend": state_backend,
        "queue_backend": queue_backend,
        "redis_ready": redis_ready,
        "queue_ready": queue_ready,
        "queue_depth": queue_depth,
        "active_jobs_count": active_jobs_count,
    }
    if redis_error:
        payload["redis_error"] = redis_error
    if queue_error:
        payload["queue_error"] = queue_error
    return payload


def _configured_backend(env_name: str, default: str, supported: set[str]) -> str:
    value = os.getenv(env_name, default).strip().lower()
    return value if value in supported else default


def _queue_name() -> str:
    return os.getenv("RAG_QUEUE_NAME", "rag-default").strip() or "rag-default"
