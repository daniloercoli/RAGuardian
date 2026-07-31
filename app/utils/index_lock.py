import hashlib
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable

from redis.exceptions import (
    AuthenticationError,
    AuthorizationError,
    ClusterDownError,
    ConnectionError as RedisConnectionError,
    LockNotOwnedError,
    TimeoutError as RedisTimeoutError,
    TryAgainError,
)

from utils.job_store import lock_ttl_seconds
from utils.state_backend import configured_queue_backend, configured_state_backend, redis_connection, state_key_prefix

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows uses the thread-safe fallback
    _fcntl = None


sys.modules.setdefault("utils.index_lock", sys.modules[__name__])
sys.modules.setdefault("app.utils.index_lock", sys.modules[__name__])


_MEMORY_INDEX_LOCK = threading.RLock()
_LOG = logging.getLogger(__name__)
_LIFECYCLE_INVALIDATORS: dict[str, Callable[[], None]] = {}
_LIFECYCLE_INVALIDATORS_LOCK = threading.RLock()
_OBSERVED_LIFECYCLE_GENERATIONS: dict[str, int] = {}
_LIFECYCLE_GENERATION_THREAD_LOCK = threading.Lock()
_LIFECYCLE_GENERATION_SYNC_LOCK = threading.RLock()
_ACTIVE_DISTRIBUTED_RENEWERS = weakref.WeakKeyDictionary()
_ACTIVE_DISTRIBUTED_RENEWERS_LOCK = threading.RLock()


class DistributedLockLeaseLostError(RuntimeError):
    """Raised when a distributed lock can no longer fence mutations."""


@contextmanager
def index_write_lock():
    lock = _lock_context()
    with lock:
        assert_distributed_locks_healthy()
        yield


def assert_distributed_locks_healthy() -> None:
    """Raise when a Redis lock held by this thread has lost its lease."""

    owner = threading.current_thread()
    with _ACTIVE_DISTRIBUTED_RENEWERS_LOCK:
        renewers = tuple(_ACTIVE_DISTRIBUTED_RENEWERS.get(owner, ()))
    for renewer in renewers:
        error = renewer.current_error()
        if error is not None:
            raise DistributedLockLeaseLostError(
                f"Redis {renewer.description} lock lease was lost"
            ) from error


def _register_distributed_renewer(renewer):
    owner = threading.current_thread()
    with _ACTIVE_DISTRIBUTED_RENEWERS_LOCK:
        active = _ACTIVE_DISTRIBUTED_RENEWERS.setdefault(owner, [])
        if not any(item is renewer for item in active):
            active.append(renewer)
    return owner


def _unregister_distributed_renewer(renewer, owner) -> None:
    if owner is None:
        return
    with _ACTIVE_DISTRIBUTED_RENEWERS_LOCK:
        active = _ACTIVE_DISTRIBUTED_RENEWERS.get(owner)
        if not active:
            return
        active[:] = [item for item in active if item is not renewer]
        if not active:
            _ACTIVE_DISTRIBUTED_RENEWERS.pop(owner, None)


@contextmanager
def lifecycle_read_lock(scope: str | None = None):
    """Allow reads while excluding a global or same-scope lifecycle writer."""

    if scope is None:
        with _lifecycle_lock(
            shared=True,
            scope=None,
            synchronize=True,
            publish=False,
        ):
            yield
        return

    with _lifecycle_lock(
        shared=True,
        scope=None,
        synchronize=True,
        publish=False,
    ):
        with _lifecycle_lock(
            shared=True,
            scope=scope,
            synchronize=False,
            publish=False,
        ):
            yield


@contextmanager
def lifecycle_read_locks(scopes) -> None:
    """Hold the global read gate and every requested KB read lock.

    Scopes are normalized and acquired in lexical order so concurrent
    multi-knowledge-base queries cannot deadlock by choosing a different
    request order.
    """

    ordered_scopes = sorted(
        {
            str(scope).strip()
            for scope in (scopes or ())
            if str(scope or "").strip()
        }
    )
    with _lifecycle_lock(
        shared=True,
        scope=None,
        synchronize=True,
        publish=False,
    ):
        with ExitStack() as stack:
            for scope in ordered_scopes:
                stack.enter_context(
                    _lifecycle_lock(
                        shared=True,
                        scope=scope,
                        synchronize=False,
                        publish=False,
                    )
                )
            assert_distributed_locks_healthy()
            yield
            assert_distributed_locks_healthy()


@contextmanager
def lifecycle_write_lock(
    scope: str | None = None,
    *,
    publish: bool | None = None,
):
    """Exclude all readers globally, or only readers of one KB scope."""

    if publish is None:
        publish = scope is None
    if scope is None:
        with _lifecycle_lock(
            shared=False,
            scope=None,
            synchronize=True,
            publish=publish,
        ):
            yield
        return

    # A scoped writer remains a global reader so restore can still exclude
    # every in-flight query/delete, while unrelated KBs continue serving.
    with _lifecycle_lock(
        shared=True,
        scope=None,
        synchronize=True,
        publish=False,
    ):
        with _lifecycle_lock(
            shared=False,
            scope=scope,
            synchronize=False,
            publish=publish,
        ):
            yield


def register_lifecycle_invalidator(
    name: str,
    callback: Callable[[], None],
) -> None:
    """Register one process-local cache reset for lifecycle epoch changes."""

    with _LIFECYCLE_GENERATION_SYNC_LOCK:
        with _LIFECYCLE_INVALIDATORS_LOCK:
            _LIFECYCLE_INVALIDATORS[str(name)] = callback


def invalidate_lifecycle_caches() -> None:
    """Immediately reset all registered process-local runtime caches."""

    with _LIFECYCLE_GENERATION_SYNC_LOCK:
        with _LIFECYCLE_INVALIDATORS_LOCK:
            callbacks = list(_LIFECYCLE_INVALIDATORS.values())
        for callback in callbacks:
            callback()


def synchronize_lifecycle_generation() -> int:
    """Apply cache invalidators when another process completed a writer."""

    with _LIFECYCLE_GENERATION_SYNC_LOCK:
        identity = _lifecycle_generation_identity()
        current = _read_lifecycle_generation()
        with _LIFECYCLE_INVALIDATORS_LOCK:
            observed = _OBSERVED_LIFECYCLE_GENERATIONS.get(identity)
            if observed is None:
                _OBSERVED_LIFECYCLE_GENERATIONS[identity] = current
                return current
            callbacks = (
                list(_LIFECYCLE_INVALIDATORS.values())
                if current != observed
                else []
            )
        for callback in callbacks:
            callback()
        if callbacks:
            with _LIFECYCLE_INVALIDATORS_LOCK:
                _OBSERVED_LIFECYCLE_GENERATIONS[identity] = current
        return current


def bump_lifecycle_generation() -> int:
    """Publish a completed destructive lifecycle to every process."""

    with _LIFECYCLE_GENERATION_SYNC_LOCK:
        identity = _lifecycle_generation_identity()
        generation = _increment_lifecycle_generation()
        with _LIFECYCLE_INVALIDATORS_LOCK:
            _OBSERVED_LIFECYCLE_GENERATIONS[identity] = generation
        return generation


def _lock_context():
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        try:
            client = redis_connection()
            timeout = lock_ttl_seconds()
            lock = client.lock(
                f"{state_key_prefix()}:lock:index-write",
                timeout=timeout,
                blocking_timeout=30,
                thread_local=False,
            )
            return _RenewingRedisLock(
                lock,
                ttl=timeout,
                description="index writer",
            )
        except Exception:
            if configured_queue_backend() == "redis":
                raise
    return _MEMORY_INDEX_LOCK


class _ThreadReadWriteLock:
    """Writer-preferring, re-entrant read/write lock for one process."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire_read(self) -> bool:
        thread_id = threading.get_ident()
        with self._condition:
            already_held = (
                self._writer == thread_id
                or self._readers.get(thread_id, 0) > 0
            )
            while (
                not already_held
                and (
                    self._writer is not None
                    or self._waiting_writers > 0
                )
            ):
                self._condition.wait()
            self._readers[thread_id] = self._readers.get(thread_id, 0) + 1
            return not already_held

    def release_read(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            depth = self._readers.get(thread_id, 0)
            if depth <= 0:
                raise RuntimeError("Lifecycle read lock is not held")
            if depth == 1:
                del self._readers[thread_id]
            else:
                self._readers[thread_id] = depth - 1
            self._condition.notify_all()

    def acquire_write(self) -> bool:
        thread_id = threading.get_ident()
        with self._condition:
            if self._writer == thread_id:
                self._writer_depth += 1
                return False
            if self._readers.get(thread_id, 0):
                raise RuntimeError(
                    "Lifecycle lock cannot be upgraded from read to write"
                )
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers:
                    self._condition.wait()
                self._writer = thread_id
                self._writer_depth = 1
                return True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._writer != thread_id:
                raise RuntimeError("Lifecycle write lock is not held")
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer = None
                self._condition.notify_all()


_LIFECYCLE_THREAD_LOCKS: dict[str, _ThreadReadWriteLock] = {
    "global": _ThreadReadWriteLock(),
}
_LIFECYCLE_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _lifecycle_lock(
    *,
    shared: bool,
    scope: str | None,
    synchronize: bool,
    publish: bool,
):
    thread_lock = _lifecycle_thread_lock(scope)
    acquire = (
        thread_lock.acquire_read
        if shared
        else thread_lock.acquire_write
    )
    release = (
        thread_lock.release_read
        if shared
        else thread_lock.release_write
    )
    outermost = acquire()
    try:
        if outermost:
            with _lifecycle_backend_lock(shared=shared, scope=scope):
                if synchronize:
                    synchronize_lifecycle_generation()
                if not publish:
                    yield
                else:
                    try:
                        yield
                    except BaseException:
                        try:
                            bump_lifecycle_generation()
                        except Exception as bump_error:
                            _LOG.error(
                                "Unable to publish failed lifecycle mutation: %s",
                                bump_error,
                            )
                        raise
                    else:
                        bump_lifecycle_generation()
        else:
            yield
    finally:
        release()


def _lifecycle_thread_lock(scope: str | None) -> _ThreadReadWriteLock:
    key = _lifecycle_scope_key(scope)
    with _LIFECYCLE_THREAD_LOCKS_GUARD:
        return _LIFECYCLE_THREAD_LOCKS.setdefault(
            key,
            _ThreadReadWriteLock(),
        )


@contextmanager
def _lifecycle_backend_lock(*, shared: bool, scope: str | None):
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        try:
            client = redis_connection()
        except Exception:
            if configured_queue_backend() == "redis":
                raise
        else:
            with _RedisLifecycleLock(client, shared=shared, scope=scope):
                yield
            return

    with _file_lifecycle_lock(shared=shared, scope=scope):
        yield


class _RedisLifecycleLock:
    """Distributed reader/writer lock using a short gate and reader leases."""

    def __init__(
        self,
        client,
        *,
        shared: bool,
        scope: str | None = None,
    ) -> None:
        self._client = client
        self._shared = shared
        self._scope = scope
        self._gate = None
        self._reader_key: str | None = None
        self._renewer: _RedisLeaseRenewer | None = None
        self._renewer_owner = None

    def __enter__(self):
        prefix = state_key_prefix()
        lock_prefix = _redis_lifecycle_lock_prefix(prefix, self._scope)
        timeout = lock_ttl_seconds()
        wait = _lifecycle_wait_seconds()
        self._gate = self._client.lock(
            f"{lock_prefix}:gate",
            timeout=timeout,
            blocking_timeout=wait,
            thread_local=False,
        )
        if not self._gate.acquire(blocking=True):
            self._gate = None
            raise TimeoutError("Timed out acquiring lifecycle lock")

        if self._shared:
            reader_key = (
                f"{lock_prefix}:reader:{uuid.uuid4().hex}"
            )
            try:
                self._client.set(reader_key, b"1", ex=timeout)
                self._reader_key = reader_key
            finally:
                try:
                    self._gate.release()
                except Exception:
                    if self._reader_key:
                        self._client.delete(self._reader_key)
                        self._reader_key = None
                    raise
                self._gate = None
            self._renewer = _RedisLeaseRenewer(
                lambda: bool(self._client.expire(reader_key, timeout)),
                ttl=timeout,
                description="lifecycle reader",
            )
            try:
                self._renewer.start()
            except Exception:
                self._renewer = None
                self._client.delete(reader_key)
                self._reader_key = None
                raise
            self._renewer_owner = _register_distributed_renewer(self._renewer)
            return self

        self._renewer = _RedisLeaseRenewer(
            lambda: bool(
                self._gate
                and self._gate.extend(timeout, replace_ttl=True)
            ),
            ttl=timeout,
            description="lifecycle writer",
        )
        try:
            self._renewer.start()
        except Exception:
            self._renewer = None
            self._gate.release()
            self._gate = None
            raise
        deadline = time.monotonic() + wait
        reader_pattern = f"{lock_prefix}:reader:*"
        try:
            while next(
                iter(self._client.scan_iter(match=reader_pattern, count=100)),
                None,
            ) is not None:
                if self._renewer.error is not None:
                    raise self._renewer.error
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for active lifecycle readers"
                    )
                time.sleep(0.05)
        except Exception:
            self._renewer.stop()
            self._renewer = None
            self._gate.release()
            self._gate = None
            raise
        self._renewer_owner = _register_distributed_renewer(self._renewer)
        return self

    def __exit__(self, exc_type, exc, traceback):
        renewer = self._renewer
        renewer_owner = self._renewer_owner
        renewal_error = None
        try:
            if renewer:
                renewal_error = renewer.stop()
            cleanup_error = None
            if self._reader_key:
                try:
                    deleted = self._client.delete(self._reader_key)
                    if not deleted:
                        cleanup_error = RuntimeError(
                            "Redis lifecycle reader lease no longer exists"
                        )
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
                finally:
                    self._reader_key = None
            if self._gate:
                try:
                    self._gate.release()
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_error or cleanup_exc
                finally:
                    self._gate = None
            lifecycle_error = cleanup_error or renewal_error
            if lifecycle_error is not None:
                if exc_type is None:
                    raise DistributedLockLeaseLostError(
                        "Redis lifecycle lock lease was lost"
                    ) from lifecycle_error
                # Preserve the operation's original exception while making the
                # lock-loss condition visible to operators.
                _LOG.error(
                    "Redis lifecycle lock cleanup failed: %s",
                    lifecycle_error,
                )
            return False
        finally:
            self._renewer = None
            self._renewer_owner = None
            if renewer is not None:
                _unregister_distributed_renewer(
                    renewer,
                    renewer_owner,
                )


class _RedisLeaseRenewer:
    """Refresh a Redis lease until its owning context exits.

    A single Redis timeout does not prove that the lease was lost. Keep
    retrying while the last known lease can still be valid, but fail
    immediately when Redis confirms that the token no longer exists.
    """

    def __init__(self, renew, *, ttl: int, description: str) -> None:
        self._renew = renew
        self._ttl = max(0.01, float(ttl))
        self._interval = max(1.0, self._ttl / 3.0)
        self._retry_interval = max(0.05, min(1.0, self._ttl / 10.0))
        self._description = description
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._lease_deadline = time.monotonic() + self._ttl
        self._last_renewal_error: Exception | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"redis-{self._description.replace(' ', '-')}-renewer",
        )
        self._thread.start()

    def stop(self) -> Exception | None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive() and self.error is None:
                self._set_error(
                    RuntimeError(
                        f"Timed out stopping {self._description} lease renewer"
                    )
                )
        with self._state_lock:
            if (
                self.error is None
                and self._last_renewal_error is not None
                and time.monotonic() >= self._lease_deadline
            ):
                self._set_expired_error(self._last_renewal_error)
        return self.error

    def _run(self) -> None:
        delay = self._interval
        while not self._stop.wait(delay):
            try:
                renewed = self._renew()
            except LockNotOwnedError as exc:
                self._set_error(exc)
                return
            except Exception as exc:
                if not _is_transient_redis_lease_error(exc):
                    self._set_error(exc)
                    return
                with self._state_lock:
                    self._last_renewal_error = exc
                    remaining = self._lease_deadline - time.monotonic()
                    if remaining <= 0:
                        self._set_expired_error(exc)
                        return
                delay = min(self._retry_interval, remaining)
                continue
            if not renewed:
                self._set_error(
                    RuntimeError(
                        f"Unable to renew {self._description} lease"
                    )
                )
                return
            with self._state_lock:
                self._last_renewal_error = None
                self._lease_deadline = time.monotonic() + self._ttl
            delay = self._interval

    def _set_expired_error(self, cause: Exception) -> None:
        error = RuntimeError(
            f"Unable to renew {self._description} lease before expiry"
        )
        error.__cause__ = cause
        self._set_error(error)

    def _set_error(self, error: Exception) -> None:
        with self._state_lock:
            if self.error is None:
                self.error = error
                self._lost.set()

    @property
    def description(self) -> str:
        return self._description

    def current_error(self) -> Exception | None:
        with self._state_lock:
            return self.error


class _RenewingRedisLock:
    """Context wrapper that keeps a redis-py Lock alive while it is held."""

    def __init__(self, lock, *, ttl: int, description: str) -> None:
        self._lock = lock
        self._ttl = ttl
        self._description = description
        self._renewer: _RedisLeaseRenewer | None = None
        self._renewer_owner = None

    def __enter__(self):
        if not self._lock.acquire(blocking=True):
            raise TimeoutError(f"Timed out acquiring {self._description} lock")
        self._renewer = _RedisLeaseRenewer(
            lambda: bool(
                self._lock.extend(self._ttl, replace_ttl=True)
            ),
            ttl=self._ttl,
            description=self._description,
        )
        try:
            self._renewer.start()
        except Exception:
            self._renewer = None
            self._lock.release()
            raise
        self._renewer_owner = _register_distributed_renewer(self._renewer)
        return self

    def __exit__(self, exc_type, exc, traceback):
        renewer = self._renewer
        renewer_owner = self._renewer_owner
        try:
            renewal_error = renewer.stop() if renewer else None
            cleanup_error = None
            try:
                self._lock.release()
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
            lock_error = cleanup_error or renewal_error
            if lock_error is not None:
                if exc_type is None:
                    raise DistributedLockLeaseLostError(
                        f"Redis {self._description} lock lease was lost"
                    ) from lock_error
                _LOG.error(
                    "Redis %s lock cleanup failed: %s",
                    self._description,
                    lock_error,
                )
            return False
        finally:
            self._renewer = None
            self._renewer_owner = None
            if renewer is not None:
                _unregister_distributed_renewer(
                    renewer,
                    renewer_owner,
                )


def _is_transient_redis_lease_error(error: Exception) -> bool:
    """Return true only for Redis failures that can recover before lease expiry."""

    if isinstance(error, (AuthenticationError, AuthorizationError)):
        return False
    return isinstance(
        error,
        (
            RedisConnectionError,
            RedisTimeoutError,
            ClusterDownError,
            TryAgainError,
        ),
    )


@contextmanager
def _file_lifecycle_lock(*, shared: bool, scope: str | None = None):
    if _fcntl is None:
        yield
        return

    path = _lifecycle_lock_file(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+")
    try:
        operation = _fcntl.LOCK_SH if shared else _fcntl.LOCK_EX
        _fcntl.flock(lock_file.fileno(), operation)
        yield
    finally:
        try:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _lifecycle_lock_file(scope: str | None = None) -> Path:
    configured = os.getenv("RAG_LIFECYCLE_LOCK_FILE")
    if configured:
        base = Path(configured)
    else:
        identity = f"{Path.cwd().resolve()}:{state_key_prefix()}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        base = Path(tempfile.gettempdir()) / f"raguardian-lifecycle-{digest}.lock"
    if scope is None:
        return base
    return Path(f"{base}.scope-{_lifecycle_scope_key(scope)}")


def _lifecycle_generation_file() -> Path:
    return Path(f"{_lifecycle_lock_file()}.generation")


def _lifecycle_scope_key(scope: str | None) -> str:
    if scope is None:
        return "global"
    normalized = str(scope).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _redis_lifecycle_lock_prefix(prefix: str, scope: str | None) -> str:
    base = f"{prefix}:lock:lifecycle"
    if scope is None:
        return base
    return f"{base}:scope:{_lifecycle_scope_key(scope)}"


def _lifecycle_generation_identity() -> str:
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        return f"redis:{state_key_prefix()}"
    return f"file:{_lifecycle_generation_file().resolve()}"


def _read_lifecycle_generation() -> int:
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        value = redis_connection().get(
            f"{state_key_prefix()}:lock:lifecycle:generation"
        )
        if value is None:
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    path = _lifecycle_generation_file()
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip() or "0"))
    except (FileNotFoundError, OSError, ValueError):
        return 0


def _increment_lifecycle_generation() -> int:
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        return int(
            redis_connection().incr(
                f"{state_key_prefix()}:lock:lifecycle:generation"
            )
        )

    path = _lifecycle_generation_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    with _LIFECYCLE_GENERATION_THREAD_LOCK:
        lock_file = lock_path.open("a+")
        try:
            if _fcntl is not None:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
            try:
                generation = max(
                    0,
                    int(path.read_text(encoding="utf-8").strip() or "0"),
                ) + 1
            except (FileNotFoundError, OSError, ValueError):
                generation = 1
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(str(generation), encoding="utf-8")
            os.replace(temporary, path)
            return generation
        finally:
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _lifecycle_wait_seconds() -> float:
    raw = os.getenv("RAG_LIFECYCLE_LOCK_WAIT_SECONDS", "30")
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 30.0
