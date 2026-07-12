from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections import defaultdict

from utils.logging_config import APP_LOGGER as log
from utils.state_backend import configured_state_backend, redis_connection, state_key_prefix


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
        self.lock = threading.Lock()
        self.backend = configured_state_backend()
        self.redis = None
        if self.backend == "redis":
            try:
                self.redis = redis_connection()
            except Exception as exc:
                log.warning("Redis rate limiter unavailable, falling back to memory: %s", exc)
                self.backend = "memory"

    def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        if self.backend == "redis" and self.redis is not None:
            try:
                return self._is_allowed_redis(client_ip)
            except Exception as exc:
                log.warning("Redis rate limiter failed, using memory fallback: %s", exc)
                return self._is_allowed_memory(client_ip)
        return self._is_allowed_memory(client_ip)

    def _is_allowed_memory(self, client_ip: str) -> tuple[bool, int]:
        with self.lock:
            current_time = time.time()
            timestamps = [
                ts
                for ts in self.requests.get(client_ip, [])
                if current_time - ts < self.window_seconds
            ]
            self.requests[client_ip] = timestamps

            if len(timestamps) >= self.max_requests:
                oldest = timestamps[0]
                wait_time = oldest + self.window_seconds - current_time
                return False, max(1, int(wait_time) + 1)

            timestamps.append(current_time)
            return True, 0

    def _is_allowed_redis(self, client_ip: str) -> tuple[bool, int]:
        now = time.time()
        cutoff = now - self.window_seconds
        key = self._redis_key(client_ip)

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zcard(key)
        _, count = pipe.execute()

        if count >= self.max_requests:
            oldest = self.redis.zrange(key, 0, 0, withscores=True)
            wait_time = self.window_seconds
            if oldest:
                wait_time = int(float(oldest[0][1]) + self.window_seconds - now) + 1
            self.redis.expire(key, self.window_seconds + 1)
            return False, max(1, wait_time)

        self.redis.zadd(key, {f"{now}:{uuid.uuid4().hex}": now})
        self.redis.expire(key, self.window_seconds + 1)
        return True, 0

    def _redis_key(self, client_ip: str) -> str:
        client_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:24]
        return f"{state_key_prefix()}:rate_limit:{client_hash}"
