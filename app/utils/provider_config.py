import os
from hashlib import sha256
from typing import Any, Iterable


OPENAI_COMPATIBLE_TYPE = "openai_compatible"
NO_AUTH_API_KEY = "openai-compatible"


def normalize_base_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def env_names(raw_env: Any) -> list[str]:
    if isinstance(raw_env, (list, tuple, set)):
        values: Iterable[Any] = raw_env
    else:
        values = str(raw_env or "").replace(",", "\n").splitlines()
    return [str(value).strip() for value in values if str(value).strip()]


def resolve_api_key(config: dict[str, Any]) -> str:
    explicit = str(config.get("api_key") or "").strip()
    if explicit:
        return explicit

    for env_name in env_names(config.get("api_key_env")):
        value = os.getenv(env_name)
        if value:
            return value
    return ""


def requires_api_key(config: dict[str, Any], default: bool = False) -> bool:
    value = config.get("requires_api_key")
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "required"}


def client_api_key(config: dict[str, Any], placeholder: str = NO_AUTH_API_KEY) -> str:
    return resolve_api_key(config) or placeholder


def default_model(config: dict[str, Any]) -> str:
    models = [str(model) for model in config.get("models", [])]
    return str(config.get("default_model") or (models[0] if models else ""))


def is_openai_compatible(config: dict[str, Any]) -> bool:
    return (
        str(config.get("type") or OPENAI_COMPATIBLE_TYPE) == OPENAI_COMPATIBLE_TYPE
        or bool(config.get("base_url"))
    )


def provider_cache_key(provider_id: str, config: dict[str, Any]) -> str:
    secret_hash = sha256(resolve_api_key(config).encode("utf-8")).hexdigest()[:12]
    parts = [
        provider_id,
        str(config.get("type") or ""),
        normalize_base_url(config.get("base_url")),
        secret_hash,
    ]
    return ":".join(parts)
