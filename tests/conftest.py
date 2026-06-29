import sys
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["RAG_STATE_BACKEND"] = "memory"
os.environ["RAG_QUEUE_BACKEND"] = "inline"
_api_key_usage_test_file = Path(tempfile.gettempdir()) / "rag_api_key_usage_test.json"
os.environ.setdefault("RAG_API_KEY_USAGE_FILE", str(_api_key_usage_test_file))
try:
    _api_key_usage_test_file.unlink()
except FileNotFoundError:
    pass


def pytest_runtest_setup(item):
    try:
        from app.utils.job_store import MemoryJobStore

        MemoryJobStore.reset()
    except Exception:
        pass
