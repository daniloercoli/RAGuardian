import sys
import threading
from contextlib import contextmanager

from utils.job_store import lock_ttl_seconds
from utils.state_backend import configured_queue_backend, configured_state_backend, redis_connection, state_key_prefix


sys.modules.setdefault("utils.index_lock", sys.modules[__name__])
sys.modules.setdefault("app.utils.index_lock", sys.modules[__name__])


_MEMORY_INDEX_LOCK = threading.RLock()


@contextmanager
def index_write_lock():
    lock = _lock_context()
    with lock:
        yield


def _lock_context():
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        try:
            client = redis_connection()
            return client.lock(
                f"{state_key_prefix()}:lock:index-write",
                timeout=lock_ttl_seconds(),
                blocking_timeout=30,
            )
        except Exception:
            if configured_queue_backend() == "redis":
                raise
    return _MEMORY_INDEX_LOCK
