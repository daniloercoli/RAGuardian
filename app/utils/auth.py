import functools
import hmac
import os
from datetime import datetime, time, timezone
from typing import Optional

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash


from utils.settings_store import API_SCOPES
from utils.user_store import UserStore, api_key_matches, normalize_email
from utils.workspace import workspace_for_user


def hash_password(password: str) -> str:
    from werkzeug.security import generate_password_hash

    return generate_password_hash(password)


def check_admin_password(password: str) -> bool:
    """Backward-compatible helper used by older tests and setup checks."""
    env_password_hash = os.getenv("RAG_ADMIN_PASSWORD_HASH") or os.getenv("ADMIN_PASSWORD_HASH")
    if env_password_hash:
        return check_password_hash(env_password_hash, password)
    env_password = os.getenv("RAG_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD")
    return password == (env_password or "admin")


def authenticate_user(email: str, password: str) -> Optional[dict]:
    store = _user_store()
    email = normalize_email(email or "admin@example.local")
    if not store.has_users() and check_admin_password(password):
        return store.bootstrap_admin_if_empty(email=email, password=password)
    return store.authenticate(email, password)


def current_user() -> Optional[dict]:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = _user_store().get(user_id)
    if not user or not user.get("enabled", True):
        session.clear()
        return None
    return user


def is_logged_in() -> bool:
    return bool(current_user())


def is_admin_logged_in() -> bool:
    user = current_user()
    return bool(user and user.get("role") == "admin")


def require_login(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if is_logged_in():
            return view(*args, **kwargs)
        return redirect(url_for("admin_login", next=request.path))

    return wrapper


def require_admin(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if is_admin_logged_in():
            return view(*args, **kwargs)
        if is_logged_in():
            return jsonify(error="Permessi admin richiesti", status="forbidden"), 403
        return redirect(url_for("admin_login", next=request.path))

    return wrapper


def find_api_key(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None

    env_key = os.getenv("RAG_API_KEY")
    if env_key and hmac.compare_digest(value, env_key):
        admin = _first_admin_user()
        if not admin:
            return None
        return {
            "name": "env",
            "key": value,
            "enabled": True,
            "scopes": sorted(API_SCOPES),
            "can_upload": True,
            "user_id": admin["id"],
            "workspace_id": admin["id"],
        }

    store = _user_store()
    if not _first_admin_user() and not _user_store().has_users():
        _user_store().bootstrap_admin_if_empty(
            email="admin@example.local",
            password=os.urandom(16).hex(),
        )

    with store._lock:
        users = store._list_unlocked()
    for user in users:
        for key_item in (user.get("api_keys") or []):
            if not key_item.get("enabled", True) or not api_key_matches(key_item, value):
                continue
            if _api_key_is_expired(key_item.get("expires_at")):
                continue
            try:
                workspace = workspace_for_user(user, app=current_app)
            except Exception:
                workspace = workspace_for_user({"id": user["id"]}, app=current_app)
            scopes = key_item.get("scopes", ["query"])
            return {
                "name": key_item.get("name", "custom"),
                "key": value,
                "enabled": True,
                "scopes": scopes,
                "can_upload": "ingest" in scopes,
                "user_id": user["id"],
                "workspace_id": workspace.workspace_id,
                "api_key_id": key_item.get("id") or key_item.get("name"),
                "_user_key_name": key_item.get("name"),
                "_user_id_for_logging": user["id"],
            }

    return None


def api_key_has_scope(key: Optional[dict], scope: str) -> bool:
    if not key:
        return False
    scopes = key.get("scopes") or []
    if scope in scopes:
        return True
    return scope == "ingest" and bool(key.get("can_upload"))


def require_api_key(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        key = find_api_key(request.headers.get("X-API-Key"))
        if not key:
            return jsonify(error="API key mancante o non valida", status="unauthorized"), 401
        request.api_key = key
        return view(*args, **kwargs)

    return wrapper


def require_api_scope(scope: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            key = find_api_key(request.headers.get("X-API-Key"))
            if not key:
                return jsonify(error="API key mancante o non valida", status="unauthorized"), 401
            if not api_key_has_scope(key, scope):
                return jsonify(error=f"API key senza scope richiesto: {scope}", status="forbidden"), 403
            request.api_key = key
            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_login_or_api_scope(scope: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if is_logged_in():
                return view(*args, **kwargs)
            key = find_api_key(request.headers.get("X-API-Key"))
            if key and api_key_has_scope(key, scope):
                request.api_key = key
                return view(*args, **kwargs)
            return jsonify(error=f"Credenziali mancanti o scope richiesto assente: {scope}", status="unauthorized"), 401

        return wrapper

    return decorator


def require_admin_or_api_scope(scope: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if is_admin_logged_in():
                return view(*args, **kwargs)
            key = find_api_key(request.headers.get("X-API-Key"))
            if key and api_key_has_scope(key, scope):
                request.api_key = key
                return view(*args, **kwargs)
            return jsonify(error=f"Credenziali mancanti o scope richiesto assente: {scope}", status="unauthorized"), 401

        return wrapper

    return decorator


def require_admin_or_upload_api_key(view):
    return require_login_or_api_scope("ingest")(view)


def _user_store() -> UserStore:
    return UserStore(current_app.config.get("USERS_FILE"))


def _first_admin_user() -> Optional[dict]:
    for user in _user_store().list():
        if user.get("role") == "admin" and user.get("enabled", True):
            return user
    return None


def _api_key_is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    parsed = _parse_expiration(expires_at)
    if parsed is None:
        return False
    return parsed <= datetime.now(timezone.utc)


def _parse_expiration(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            day = datetime.fromisoformat(raw).date()
            return datetime.combine(day, time.max, tzinfo=timezone.utc)
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
