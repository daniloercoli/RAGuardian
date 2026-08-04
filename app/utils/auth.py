import functools
import hmac
import os
from typing import Optional

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash


from utils.settings_store import API_SCOPES
from utils.user_store import UserStore, normalize_email
from utils.workspace import safe_workspace_id


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
            "scopes": ["query", "ingest", "speech"],
            "can_upload": True,
            "knowledge_base_ids": ["default"],
            "user_id": admin["id"],
            "workspace_id": safe_workspace_id(admin["id"]),
        }

    found = _user_store().find_api_key_by_value(value)
    if found is not None:
        found["workspace_id"] = safe_workspace_id(found["user_id"])
    return found


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


def require_api_any_scope(*required_scopes: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            key = find_api_key(request.headers.get("X-API-Key"))
            if not key:
                return jsonify(error="API key mancante o non valida", status="unauthorized"), 401
            if not any(api_key_has_scope(key, scope) for scope in required_scopes):
                return jsonify(
                    error="API key senza uno scope richiesto",
                    status="forbidden",
                ), 403
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


def require_login_or_api_any_scope(*required_scopes: str):
    """Allow any authenticated user or an API key with one required scope."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if is_logged_in():
                return view(*args, **kwargs)
            key = find_api_key(request.headers.get("X-API-Key"))
            if key and any(api_key_has_scope(key, scope) for scope in required_scopes):
                request.api_key = key
                return view(*args, **kwargs)
            return jsonify(
                error="Credenziali mancanti o scope richiesto assente",
                status="unauthorized",
            ), 401

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


def require_admin_or_api_any_scope(*required_scopes: str):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if is_admin_logged_in():
                return view(*args, **kwargs)
            key = find_api_key(request.headers.get("X-API-Key"))
            if key and any(api_key_has_scope(key, scope) for scope in required_scopes):
                request.api_key = key
                return view(*args, **kwargs)
            return jsonify(
                error="Credenziali mancanti o scope richiesto assente",
                status="unauthorized",
            ), 401

        return wrapper

    return decorator


def require_admin_or_upload_api_key(view):
    return require_login_or_api_scope("ingest")(view)


def _user_store() -> UserStore:
    return UserStore(current_app.config.get("USERS_DB"))


def _first_admin_user() -> Optional[dict]:
    for user in _user_store().list():
        if user.get("role") == "admin" and user.get("enabled", True):
            return user
    return None
