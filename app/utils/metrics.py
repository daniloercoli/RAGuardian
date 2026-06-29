"""Prometheus-compatible in-process metrics registry.

Collects RAG service metrics exposed in Prometheus text exposition format.
No external dependencies required -- pure Python implementation.

Metrics:
  - rag_queries_total (Counter, by status)
  - rag_query_duration_seconds (Histogram)
  - rag_retrieval_duration_seconds (Histogram)
  - rag_cache_hits_total (Counter)
  - rag_cache_misses_total (Counter)
  - rag_context_docs_count (Gauge, set per query)
  - rag_tokens_generated_total (Counter, by provider)
  - rag_errors_total (Counter, by type)
  - rag_active_queries (Gauge)
  - rag_request_duration_seconds (Histogram, by method/endpoint/status)
  - rag_http_requests_total (Counter, by method/endpoint/status)
"""

from __future__ import annotations

import time
import threading
from typing import Dict, List, Optional, Sequence


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Counter:
    """Simple thread-safe counter with labels."""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


class _BucketedHistogram:
    """Thread-safe histogram with configurable buckets."""

    __slots__ = ("_lock", "_buckets", "_upper_bounds", "_count", "_sum")

    def __init__(self, upper_bounds: Optional[Sequence[float]] = None) -> None:
        self._lock = threading.Lock()
        if upper_bounds is None:
            upper_bounds = (
                0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75,
                1.0, 2.5, 5.0, 10.0, 25.0, 60.0, 120.0, 300.0,
            )
        self._upper_bounds: List[float] = sorted(upper_bounds)
        self._buckets: List[float] = [0.0] * (len(self._upper_bounds) + 1)
        self._count: int = 0
        self._sum: float = 0.0

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for idx, bound in enumerate(self._upper_bounds):
                if value <= bound:
                    self._buckets[idx] += 1
                    break
            else:
                self._buckets[-1] += 1  # +Inf bucket

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def total(self) -> float:
        with self._lock:
            return self._sum

    def _bucket_label(self, idx: int) -> str:
        if idx == len(self._upper_bounds):
            return "+Inf"
        return str(self._upper_bounds[idx])

    def format_prometheus(self, metric_name: str, labels: str = "") -> List[str]:
        cumulative = 0.0
        lines: List[str] = []
        with self._lock:
            for idx in range(len(self._upper_bounds)):
                cumulative += self._buckets[idx]
                bucket_labels = _merge_labels(labels, f'le="{self._bucket_label(idx)}"')
                lines.append(f"{metric_name}_bucket{{{bucket_labels}}} {cumulative:.0f}")
            cumulative += self._buckets[-1]
            bucket_labels = _merge_labels(labels, 'le="+Inf"')
            lines.append(f"{metric_name}_bucket{{{bucket_labels}}} {cumulative:.0f}")
            if labels:
                lines.append(f"{metric_name}_sum{{{labels}}} {self._sum:.3f}")
                lines.append(f"{metric_name}_count{{{labels}}} {self._count:.0f}")
            else:
                lines.append(f"{metric_name}_sum {self._sum:.3f}")
                lines.append(f"{metric_name}_count {self._count:.0f}")
        return lines


class RAGMetrics:
    """Collects and exposes RAG service metrics in Prometheus text format.

    All counters and histograms are thread-safe. Use the class-level property
    to get the global instance:

        metrics = RAGMetrics.get()
        metrics.observe_query(duration=2.3, status="success")
    """

    _instance: Optional["RAGMetrics"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # --- Counters ---
        self._query_total_labels: Dict[str, _Counter] = {}
        self._cache_hits = _Counter()
        self._cache_misses = _Counter()
        self._tokens_total_labels: Dict[str, _Counter] = {}
        self._error_total_labels: Dict[str, _Counter] = {}
        self._request_total_labels: Dict[str, _Counter] = {}

        # --- Backup counters ---
        self._backup_total_labels: Dict[str, _Counter] = {}
        self._restore_total_labels: Dict[str, _Counter] = {}

        # --- Histograms ---
        self._query_duration = _BucketedHistogram()
        self._retrieval_duration = _BucketedHistogram()
        self._request_duration = _BucketedHistogram()

        # --- Gauges ---
        self._lock = threading.Lock()
        self._active_queries: int = 0
        self._context_docs_count: float = 0.0

        # Startup timestamp
        self._start_time = time.time()

        # --- Memory tracking ---
        self._last_memory_rss: float = 0.0

        self._load_memory_stats()

    @classmethod
    def get(cls) -> "RAGMetrics":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------
    # Observers (call from your code)
    # ------------------------------------------------------------------

    def begin_query(self) -> None:
        with self._lock:
            self._active_queries += 1

    def end_query(self) -> None:
        with self._lock:
            self._active_queries = max(0, self._active_queries - 1)

    def observe_query(
        self,
        duration: float,
        status: str = "success",
        provider: str = "",
        tokens: int = 0,
    ) -> None:
        self._inc_counter("query_total", f"status=\"{status}\"", 1)
        self._query_duration.observe(duration)
        if provider and tokens:
            self._inc_counter("tokens_total", f"provider=\"{provider}\"", tokens)

    def observe_retrieval(self, duration: float, docs_count: int, cache_hit: bool) -> None:
        self._retrieval_duration.observe(duration)
        with self._lock:
            self._context_docs_count = docs_count
        if cache_hit:
            self._cache_hits.inc()
        else:
            self._cache_misses.inc()

    def observe_error(self, error_type: str) -> None:
        self._inc_counter("error_total", f"type=\"{error_type}\"", 1)

    def observe_request(
        self,
        duration: float,
        method: str,
        endpoint: str,
        status_code: int,
    ) -> None:
        label = (
            f'method="{_escape_label_value(method)}", '
            f'endpoint="{_escape_label_value(endpoint)}", '
            f'status="{status_code}"'
        )
        self._inc_counter("request_total", label, 1)
        self._request_duration.observe(duration)

    def observe_backup(self, action: str, duration: float, status: str) -> None:
        """Track backup / restore operations."""
        label = (
            f'action="{_escape_label_value(action)}", '
            f'status="{_escape_label_value(status)}"'
        )
        if action == "restore":
            self._inc_counter("restore_total", label, 1)
        else:
            self._inc_counter("backup_total", label, 1)

    def set_context_docs_count(self, count: int) -> None:
        with self._lock:
            self._context_docs_count = count

    # ------------------------------------------------------------------
    # Gauge accessors
    # ------------------------------------------------------------------

    @property
    def active_queries(self) -> int:
        with self._lock:
            return self._active_queries

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._start_time

    def _load_memory_stats(self) -> None:
        """Read process memory from /proc on Linux or fall back to 0."""
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            self._last_memory_rss = usage.ru_maxrss * 1024  # KB -> bytes on Linux
        except Exception:
            self._last_memory_rss = 0.0

    @property
    def last_memory_bytes(self) -> float:
        return self._last_memory_rss

    def refresh_memory(self) -> None:
        self._load_memory_stats()

    # ------------------------------------------------------------------
    # Prometheus text exposition format
    # ------------------------------------------------------------------

    def generate_prometheus_text(self) -> str:
        """Return metrics in Prometheus text exposition format."""
        lines: List[str] = []
        lines.append("# HELP rag_queries_total Total RAG queries.")
        lines.append("# TYPE rag_queries_total counter")
        for label, counter in sorted(self._query_total_labels.items()):
            lines.append(f'rag_queries_total{{{label}}} {counter.value:.1f}')

        # Query duration histogram
        lines.append("# HELP rag_query_duration_seconds RAG query duration.")
        lines.append("# TYPE rag_query_duration_seconds histogram")
        lines.extend(self._query_duration.format_prometheus("rag_query_duration_seconds"))

        # Retrieval duration histogram
        lines.append("# HELP rag_retrieval_duration_seconds Retrieval latency.")
        lines.append("# TYPE rag_retrieval_duration_seconds histogram")
        lines.extend(self._retrieval_duration.format_prometheus("rag_retrieval_duration_seconds"))

        # Cache hits/misses
        lines.append("# HELP rag_cache_hits_total Cache hits.")
        lines.append("# TYPE rag_cache_hits_total counter")
        lines.append(f"rag_cache_hits_total {self._cache_hits.value:.1f}")

        lines.append("# HELP rag_cache_misses_total Cache misses.")
        lines.append("# TYPE rag_cache_misses_total counter")
        lines.append(f"rag_cache_misses_total {self._cache_misses.value:.1f}")

        # Tokens
        lines.append("# HELP rag_tokens_generated_total Tokens generated by provider.")
        lines.append("# TYPE rag_tokens_generated_total counter")
        for label, counter in sorted(self._tokens_total_labels.items()):
            lines.append(f'rag_tokens_generated_total{{{label}}} {counter.value:.1f}')

        # Errors
        lines.append("# HELP rag_errors_total Error count by type.")
        lines.append("# TYPE rag_errors_total counter")
        for label, counter in sorted(self._error_total_labels.items()):
            lines.append(f'rag_errors_total{{{label}}} {counter.value:.1f}')

        # Active queries gauge
        lines.append("# HELP rag_active_queries Currently active queries.")
        lines.append("# TYPE rag_active_queries gauge")
        lines.append(f"rag_active_queries {self.active_queries}")

        # Context docs gauge
        lines.append("# HELP rag_context_docs_count Docs in last query context.")
        lines.append("# TYPE rag_context_docs_count gauge")
        with self._lock:
            lines.append(f"rag_context_docs_count {self._context_docs_count:.0f}")

        # HTTP request duration
        lines.append(
            "# HELP rag_request_duration_seconds HTTP request latency by method/endpoint/status."
        )
        lines.append("# TYPE rag_request_duration_seconds histogram")
        lines.extend(self._request_duration.format_prometheus("rag_request_duration_seconds"))

        # HTTP request count
        lines.append("# HELP rag_http_requests_total Total HTTP requests.")
        lines.append("# TYPE rag_http_requests_total counter")
        for label, counter in sorted(self._request_total_labels.items()):
            lines.append(f'rag_http_requests_total{{{label}}} {counter.value:.1f}')

        # Uptime
        lines.append("# HELP rag_uptime_seconds Service uptime in seconds.")
        lines.append("# TYPE rag_uptime_seconds gauge")
        lines.append(f"rag_uptime_seconds {self.uptime_seconds:.1f}")

        # Memory
        lines.append("# HELP rag_memory_rss_bytes Process RSS memory in bytes.")
        lines.append("# TYPE rag_memory_rss_bytes gauge")
        lines.append(f"rag_memory_rss_bytes {self.last_memory_bytes:.0f}")

        # Backup operations
        lines.append(
            "# HELP rag_backup_total Backup operations (create, restore)."
        )
        lines.append("# TYPE rag_backup_total counter")
        for label, counter in sorted(self._backup_total_labels.items()):
            lines.append(f'rag_backup_total{{{label}}} {counter.value:.1f}')

        lines.append(
            "# HELP rag_restore_total Restore operations."
        )
        lines.append("# TYPE rag_restore_total counter")
        for label, counter in sorted(self._restore_total_labels.items()):
            lines.append(f'rag_restore_total{{{label}}} {counter.value:.1f}')

        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _inc_counter(self, family_name: str, label: str, amount: float = 1.0) -> None:
        with self._lock:
            counter_map = getattr(
                self,
                f"_{family_name}_labels",
                None,
            )
            if counter_map is None:
                return
            if label not in counter_map:
                counter_map[label] = _Counter()
            counter_map[label].inc(amount)


def get_metrics() -> RAGMetrics:
    """Shortcut to access the global metrics instance."""
    return RAGMetrics.get()


def _merge_labels(*labels: str) -> str:
    return ",".join(label.strip() for label in labels if label.strip())
