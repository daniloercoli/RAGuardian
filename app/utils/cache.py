import time
import hashlib
import json
import threading
from typing import Optional, Any
from collections import OrderedDict
from .logging_config import RAG_LOGGER as log
from .state_backend import (
    configured_state_backend,
    redis_connection,
    redis_scan_delete,
    state_key_prefix,
)


def _serialize_documents(results: list) -> bytes:
    payload = []
    for item in results:
        if not hasattr(item, "page_content"):
            raise TypeError("Redis RAG cache supports document results only")
        payload.append(
            {
                "page_content": str(item.page_content or ""),
                "metadata": dict(getattr(item, "metadata", {}) or {}),
            }
        )
    return json.dumps(
        {"schema_version": 1, "documents": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _deserialize_documents(raw: bytes | str) -> list:
    from langchain_core.documents import Document

    payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("documents"), list):
        raise ValueError("Unsupported Redis cache payload")
    return [
        Document(
            page_content=str(item.get("page_content") or ""),
            metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
        )
        for item in payload["documents"]
        if isinstance(item, dict)
    ]


class CacheEntry:
    def __init__(self, value: Any, ttl: int = 3600):
        self.value = value
        self.created_at = time.time()
        self.ttl = ttl
    
    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class SimpleLRUCache:
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self.cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self.cache:
                return None

            entry = self.cache[key]
            if entry.is_expired:
                del self.cache[key]
                log.debug(f"Cache entry expired: {key[:50]}...")
                return None

            self.cache.move_to_end(key)
            return entry.value
    
    def set(self, key: str, value: Any, ttl: int = 3600):
        with self._lock:
            if key in self.cache:
                del self.cache[key]

            self.cache[key] = CacheEntry(value, ttl)

            if len(self.cache) > self.max_size:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
                log.debug(f"Cache evicted: {oldest_key[:50]}...")
    
    def clear(self):
        with self._lock:
            self.cache.clear()
        log.info("Cache cleared")
    
    def __len__(self):
        with self._lock:
            return len(self.cache)


class RAGCache:
    _instance: Optional['RAGCache'] = None
    _cache: Optional[SimpleLRUCache] = None
    
    @staticmethod
    def reset() -> None:
        """Reset singleton state (per test isolation e reload settings)."""
        RAGCache._instance = None
        RAGCache._cache = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return

        self._cache = SimpleLRUCache(max_size=100)
        self._redis = None
        self._backend = configured_state_backend()
        if self._backend == "redis":
            try:
                self._redis = redis_connection()
            except Exception as exc:
                log.warning("Redis cache backend unavailable, falling back to memory: %s", exc)
                self._backend = "memory"
        self._initialized = True
        log.info("RAGCache initialized (backend=%s, max_size=100)", self._backend)
    
    def _get_config(self):
        from config import Config
        return Config.rag
    
    def _generate_key(self, query: str, k: int, model: str, namespace: str = "stateless") -> str:
        key_data = {
            "namespace": namespace or "stateless",
            "query": query,
            "k": k,
            "model": model
        }
        key_json = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_json.encode()).hexdigest()[:16]

    def _redis_key(self, cache_key: str) -> str:
        return f"{state_key_prefix()}:cache:{cache_key}"
    
    def get(
        self,
        query: str,
        k: Optional[int] = None,
        model: Optional[str] = None,
        namespace: str = "stateless",
    ) -> Optional[list]:
        config = self._get_config()
        if not config.enable_cache:
            return None
        
        k = k or config.query_k
        model = model or config.default_model
        cache_key = self._generate_key(query, k, model, namespace)
        
        if self._backend == "redis" and self._redis is not None:
            try:
                raw = self._redis.get(self._redis_key(cache_key))
                result = _deserialize_documents(raw) if raw else None
            except Exception as exc:
                log.warning("Redis cache read failed, using memory fallback: %s", exc)
                result = self._cache.get(cache_key)
        else:
            result = self._cache.get(cache_key)
        if result:
            log.debug(f"Cache hit for query: {query[:50]}...")
        else:
            log.debug(f"Cache miss for query: {query[:50]}...")
        
        return result
    
    def set(
        self,
        query: str,
        results: list,
        k: Optional[int] = None,
        model: Optional[str] = None,
        namespace: str = "stateless",
    ):
        config = self._get_config()
        if not config.enable_cache:
            return
        
        k = k or config.query_k
        model = model or config.default_model
        cache_key = self._generate_key(query, k, model, namespace)
        
        if self._backend == "redis" and self._redis is not None:
            try:
                self._redis.setex(
                    self._redis_key(cache_key),
                    config.cache_ttl,
                    _serialize_documents(results),
                )
            except Exception as exc:
                log.warning("Redis cache write failed, using memory fallback: %s", exc)
                self._cache.set(cache_key, results, config.cache_ttl)
        else:
            self._cache.set(cache_key, results, config.cache_ttl)
        log.debug(f"Cache set for query: {query[:50]}... (key: {cache_key})")
    
    def clear(self):
        if self._backend == "redis" and self._redis is not None:
            try:
                redis_scan_delete(self._redis, f"{state_key_prefix()}:cache:*")
                log.info("Redis cache cleared")
                return
            except Exception as exc:
                log.warning("Redis cache clear failed, clearing memory fallback: %s", exc)
        if self._cache:
            self._cache.clear()
    
    @property
    def size(self) -> int:
        if self._backend == "redis" and self._redis is not None:
            try:
                return sum(1 for _ in self._redis.scan_iter(match=f"{state_key_prefix()}:cache:*", count=200))
            except Exception as exc:
                log.warning("Redis cache size failed, using memory fallback: %s", exc)
        return len(self._cache) if self._cache else 0

    @property
    def backend(self) -> str:
        return self._backend
