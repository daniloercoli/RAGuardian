from __future__ import annotations

import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback remains thread-safe
    fcntl = None


class ProcessSafeFileLock:
    """Serialize a JSON store across threads and, on Unix, processes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._thread_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            lock_file = self.path.open("a+")
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._local.lock_file = lock_file
        self._local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        depth = getattr(self._local, "depth", 1) - 1
        self._local.depth = depth
        if depth == 0:
            lock_file = self._local.lock_file
            try:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
                del self._local.lock_file
        self._thread_lock.release()
