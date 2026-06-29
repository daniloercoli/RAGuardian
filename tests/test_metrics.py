"""Tests for metrics module."""
import gc
import pytest
from app.utils.metrics import RAGMetrics, get_metrics, _Counter, _BucketedHistogram


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset singleton before each test."""
    RAGMetrics._instance = None
    yield
    RAGMetrics._instance = None
    gc.collect()


class TestCounter:
    def test_initial_value(self):
        c = _Counter()
        assert c.value == 0.0

    def test_increment(self):
        c = _Counter()
        c.inc()
        c.inc(2.5)
        assert c.value == 3.5


class TestHistogram:
    def test_observe_increases_count(self):
        h = _BucketedHistogram(upper_bounds=[1.0, 2.0])
        h.observe(0.5)
        h.observe(1.5)
        assert h.count == 2
        assert h.total == pytest.approx(2.0)

    def test_buckets_count_correctly(self):
        h = _BucketedHistogram(upper_bounds=[0.5, 1.0, 2.0])
        h.observe(0.3)
        h.observe(0.8)
        h.observe(1.5)
        cumulative = 0
        with h._lock:
            for idx, bound in enumerate(h._upper_bounds):
                cumulative += h._buckets[idx]
            cumulative += h._buckets[-1]
        assert cumulative == 3


class TestRAGMetrics:
    def test_singleton(self):
        a = get_metrics()
        b = get_metrics()
        assert a is b

    def test_observe_query(self):
        m = get_metrics()
        m.observe_query(duration=2.0, status="success")
        text = m.generate_prometheus_text()
        assert 'status="success"' in text
        # Verify it's in the 2.0-5.0 bucket
        assert 'rag_query_duration_seconds_bucket{le="2.5"} 1' in text
        assert "rag_query_duration_seconds_count 1" in text
        assert "rag_query_duration_seconds_sum 2.0" in text

    def test_observe_retrieval(self):
        m = get_metrics()
        m.observe_retrieval(duration=0.01, docs_count=3, cache_hit=True)
        text = m.generate_prometheus_text()
        assert "rag_cache_hits_total" in text
        # Check histogram updated
        assert "rag_retrieval_duration_seconds_count 1" in text

    def test_cache_hit_rate(self):
        m = get_metrics()
        m._cache_hits.inc(80)
        m._cache_misses.inc(20)
        hit_rate = m._cache_hits.value / max(
            m._cache_hits.value + m._cache_misses.value, 1
        )
        assert hit_rate == pytest.approx(0.8)

    def test_observe_error(self):
        m = get_metrics()
        m.observe_error("timeout")
        text = m.generate_prometheus_text()
        assert 'type="timeout"' in text

    def test_begin_end_query(self):
        m = get_metrics()
        assert m.active_queries == 0
        m.begin_query()
        assert m.active_queries == 1
        m.end_query()
        assert m.active_queries == 0

    def test_prometheus_format_contains_help_lines(self):
        m = get_metrics()
        text = m.generate_prometheus_text()
        assert "# HELP rag_queries_total" in text
        assert "# TYPE rag_queries_total counter" in text
        assert "# HELP rag_query_duration_seconds" in text
        assert "# TYPE rag_query_duration_seconds histogram" in text
        assert 'rag_query_duration_seconds_bucket{le="+Inf"}' in text
        assert "\tbuckets.le" not in text
        assert "# HELP rag_uptime_seconds" in text
        assert text.endswith("\n")

    def test_uptime_increases(self):
        m = get_metrics()
        assert m.uptime_seconds >= 0

    def test_observe_request(self):
        m = get_metrics()
        m.observe_request(
            duration=0.5,
            method="POST",
            endpoint="/api/v1/query",
            status_code=200,
        )
        text = m.generate_prometheus_text()
        assert 'endpoint="/api/v1/query"' in text
        assert 'method="POST"' in text
        assert 'status="200"' in text

    def test_backup_metrics(self):
        m = get_metrics()
        m.observe_backup("create", 1.5, "success")
        m.observe_backup("restore", 2.0, "success")
        text = m.generate_prometheus_text()
        assert "rag_backup_total" in text
        assert "rag_restore_total" in text
        assert 'action="create"' in text
        assert 'action="restore"' in text
