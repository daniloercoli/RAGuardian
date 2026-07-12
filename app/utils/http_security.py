from __future__ import annotations

import os
import secrets
import time
from urllib.parse import urlsplit

from flask import Flask, Response, current_app, has_request_context, jsonify, request, session


class RequestTimeoutExceeded(TimeoutError):
    pass


def env_csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def security_hardening_required() -> bool:
    environment = os.getenv("RAG_ENV", os.getenv("FLASK_ENV", "")).strip().lower()
    if environment in {"prod", "production", "staging"}:
        return True

    bind = os.getenv("GUNICORN_BIND", "").strip()
    host = os.getenv("GUNICORN_HOST", os.getenv("FLASK_HOST", "127.0.0.1")).strip()
    if bind and not bind.startswith("unix:"):
        host = bind.rsplit(":", 1)[0].strip("[]")
    return host not in {"", "127.0.0.1", "::1", "localhost"}


def validate_security_config(app: Flask) -> None:
    if app.testing or not security_hardening_required():
        return

    errors = []
    flask_secret = str(app.config.get("SECRET_KEY") or "")
    connector_secret = str(app.config.get("RAG_SECRET_KEY") or "")
    admin_hash = str(os.getenv("RAG_ADMIN_PASSWORD_HASH") or os.getenv("ADMIN_PASSWORD_HASH") or "")
    admin_password = str(os.getenv("RAG_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD") or "")
    known_dev_secrets = {
        "dev-secret",
        "dev-secret-key",
        "dev-only-change-me-min-32-characters",
        "dev-only-change-me-too-min-32-characters",
    }

    if len(flask_secret) < 32 or flask_secret in known_dev_secrets:
        errors.append("FLASK_SECRET_KEY must be a non-development secret of at least 32 characters")
    if len(connector_secret) < 32 or connector_secret in known_dev_secrets:
        errors.append("RAG_SECRET_KEY must be a non-development secret of at least 32 characters")
    if not admin_hash and (not admin_password or admin_password == "admin"):
        errors.append("configure RAG_ADMIN_PASSWORD_HASH or a non-default RAG_ADMIN_PASSWORD")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf_request(app: Flask):
    if not app.config.get("CSRF_ENABLED", True):
        return None
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"} or _csrf_exempt_request():
        return None

    expected = session.get("_csrf_token")
    provided = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if expected and provided and secrets.compare_digest(str(expected), str(provided)):
        return None
    if request.is_json:
        return jsonify(error="Token CSRF mancante o non valido", status="forbidden"), 403
    return Response("Token CSRF mancante o non valido", status=403, mimetype="text/plain")


def safe_next_url(value: str | None) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    return candidate


def request_timeout_seconds(app: Flask | None = None) -> float:
    value = (app or current_app).config.get("REQUEST_TIMEOUT_SECONDS", 0)
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def ensure_request_not_timed_out() -> None:
    if not has_request_context():
        return
    deadline = getattr(request, "_rag_deadline", None)
    if deadline and time.monotonic() > float(deadline):
        raise RequestTimeoutExceeded("Richiesta scaduta")


def cors_origin_for_request(app: Flask) -> str:
    origin = request.headers.get("Origin", "")
    if not origin:
        return ""
    allowed = app.config.get("CORS_ALLOWED_ORIGINS") or []
    if isinstance(allowed, str):
        allowed = split_config_csv(allowed)
    if "*" in allowed:
        return origin if app.config.get("CORS_ALLOW_CREDENTIALS") else "*"
    return origin if origin in allowed else ""


def apply_cors_headers(app: Flask, response: Response) -> None:
    origin = cors_origin_for_request(app)
    if not origin:
        return
    response.headers["Access-Control-Allow-Origin"] = origin
    _append_vary_header(response, "Origin")
    response.headers["Access-Control-Allow-Methods"] = ", ".join(
        split_config_csv(app.config.get("CORS_ALLOWED_METHODS") or [])
    )
    response.headers["Access-Control-Allow-Headers"] = ", ".join(
        split_config_csv(app.config.get("CORS_ALLOWED_HEADERS") or [])
    )
    response.headers["Access-Control-Max-Age"] = str(app.config.get("CORS_MAX_AGE", 600))
    if app.config.get("CORS_ALLOW_CREDENTIALS"):
        response.headers["Access-Control-Allow-Credentials"] = "true"


def split_config_csv(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _csrf_exempt_request() -> bool:
    if request.path.startswith("/api/v1/"):
        return bool(request.headers.get("X-API-Key"))
    return request.path == "/upload" and bool(request.headers.get("X-API-Key"))


def _append_vary_header(response: Response, value: str) -> None:
    existing = [part.strip() for part in response.headers.get("Vary", "").split(",") if part.strip()]
    if value not in existing:
        existing.append(value)
    response.headers["Vary"] = ", ".join(existing)
