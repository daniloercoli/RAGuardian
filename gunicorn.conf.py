import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


host = os.getenv("GUNICORN_HOST", os.getenv("FLASK_HOST", "127.0.0.1"))
port = os.getenv("GUNICORN_PORT", os.getenv("FLASK_PORT", "5000"))
bind = os.getenv("GUNICORN_BIND", f"{host}:{port}")

# Keep a single worker for the current MVP because admin rebuild progress is
# tracked in process memory. Scale with threads before increasing workers.
workers = _env_int("GUNICORN_WORKERS", 1)
threads = _env_int("GUNICORN_THREADS", 4)
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")
timeout = _env_int("GUNICORN_TIMEOUT", 180)
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)

loglevel = os.getenv("LOG_LEVEL", "info").lower()
capture_output = True
enable_stdio_inheritance = True
raw_env = [
    f"LOG_TO_CONSOLE={os.getenv('LOG_TO_CONSOLE', '0')}",
]

log_dir = Path(os.getenv("LOG_DIR", "app/logs"))
log_dir.mkdir(parents=True, exist_ok=True)
accesslog = str(log_dir / os.getenv("GUNICORN_ACCESS_LOG", "gunicorn_access.log"))
runtime_log_name = os.getenv("GUNICORN_RUNTIME_LOG", os.getenv("GUNICORN_ERROR_LOG", "gunicorn_runtime.log"))
errorlog = str(log_dir / runtime_log_name)
access_log_format = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s '
    '"%(f)s" "%(a)s" %(L)s'
)
