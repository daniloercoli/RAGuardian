import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.file_lock import ProcessSafeFileLock


_BUILTIN_DEFAULTS_CACHE: Dict[str, Any] = {}

def _builtin_defaults() -> Dict[str, Any]:
    """Lazy-load built-in defaults, with graceful fallback for missing config file."""
    if _BUILTIN_DEFAULTS_CACHE:
        return _BUILTIN_DEFAULTS_CACHE
    from .model_defaults import load_builtin_defaults, ModelConfigurationError
    try:
        result = load_builtin_defaults()
        _BUILTIN_DEFAULTS_CACHE.clear()
        _BUILTIN_DEFAULTS_CACHE.update(result)
        return result
    except ModelConfigurationError:
        _BUILTIN_DEFAULTS_CACHE.clear()
        _BUILTIN_DEFAULTS_CACHE.update({"llm": {}, "embedding": [], "reranker": [], "voice": [], "ocr": []})
        return _BUILTIN_DEFAULTS_CACHE


def _builtin_llm() -> Dict[str, Any]:
    defaults = _builtin_defaults()
    return defaults.get("llm", {})


def _builtin_embedding() -> List[Dict[str, Any]]:
    defaults = _builtin_defaults()
    return defaults.get("embedding", [])


def _builtin_reranker() -> List[Dict[str, Any]]:
    defaults = _builtin_defaults()
    return defaults.get("reranker", [])


def _builtin_voice() -> List[Dict[str, Any]]:
    defaults = _builtin_defaults()
    return defaults.get("voice", [])


def _builtin_ocr() -> List[Dict[str, Any]]:
    defaults = _builtin_defaults()
    return defaults.get("ocr", [])


def _first_llm_provider() -> tuple[str, str]:
    """Return (provider_id, default_model) of the first LLM provider."""
    llm = _builtin_llm()
    for provider_id, config in llm.items():
        return provider_id, str(config.get("default_model", ""))
    return "", ""


def _first_embedding_provider() -> tuple[str, str]:
    """Return (provider_id, default_model) of the first embedding provider."""
    for prov in _builtin_embedding():
        return prov.get("id", ""), prov.get("default_model", "")
    return "local", "sentence-transformers/all-MiniLM-L6-v2"


def _first_reranker_provider() -> Dict[str, Any]:
    for prov in _builtin_reranker():
        return prov
    return {"id": "local", "default_model": "BAAI/bge-reranker-v2-m3", "name": "Local BGE"}


def _first_voice_provider() -> Dict[str, Any]:
    for prov in _builtin_voice():
        return prov
    return {
        "id": "",
        "base_url": "",
        "stt_model": "",
        "stt_language": "",
        "tts_model": "",
        "voice": "alloy",
        "format": "mp3",
    }


def _first_ocr_provider() -> Dict[str, Any]:
    for prov in _builtin_ocr():
        return prov
    return {
        "id": "",
        "name": "",
        "base_url": "",
        "api_key_env": "",
        "requires_api_key": True,
        "models": [],
        "default_model": "",
        "ocr_mode": "vision_chat",
        "input_types": ["image", "pdf"],
        "output_format": "text",
        "supports_layout": False,
        "supports_tables": False,
    }


def _default_reranker_model_value(provider: Dict[str, Any]) -> str:
    provider_id = str(provider.get("id") or "local")
    model = str(provider.get("default_model") or "")
    if provider_id and provider_id != "local" and model:
        return f"{provider_id}/{model}"
    return model or "BAAI/bge-reranker-v2-m3"


# Build DEFAULT_SETTINGS from loaders (no hardcoded provider-specific values)
_llm_provider_id, _llm_default_model = _first_llm_provider()
_emb_provider_id, _emb_default_model = _first_embedding_provider()
_reranker_default = _first_reranker_provider()
_voice_default = _first_voice_provider()
_ocr_default = _first_ocr_provider()


DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 1,
    "rag": {
        "embedding_model": _emb_default_model,
        "embedding_provider": _emb_provider_id,
        "chunk_size": 1000,
        "chunk_overlap": 150,
        "query_k": 5,
        "temperature": 0.3,
        "default_provider": _llm_provider_id,
        "default_model": _llm_default_model,
        "enable_cache": True,
        "cache_ttl": 3600,
        "use_internal_knowledge": False,
        "reranker_enabled": False,
        "reranker_type": str(_reranker_default.get("id") or "local"),
        "reranker_model": _default_reranker_model_value(_reranker_default),
        "reranker_top_n": 20,
        "reranker_diversity_mode": "none",
        "reranker_mmr_lambda": 0.7,
        "reranker_mmr_candidate_pool": 80,
        "reranker_threshold": 0.0,
        "reranker_api_key": "",
        "reranker_regolo_api_key": "",
    },
    "auth": {
        "admin_password_hash": "",
        "api_keys": [],
    },
    "custom_providers": [],
    "embedding_providers": [],
    "reranker_providers": [],
    "voice_providers": [],
    "ocr_providers": [],
    "voice": {
        "provider": _voice_default.get("id", ""),
        "enabled": True,
        "base_url": _voice_default.get("base_url", ""),
        "api_key_env": _voice_default.get("api_key_env", ""),
        "requires_api_key": _voice_default.get("requires_api_key", True),
        "api_key": "",
        "stt_model": _voice_default.get("stt_model", ""),
        "stt_language": _voice_default.get("stt_language", ""),
        "tts_model": _voice_default.get("tts_model", ""),
        "voice": _voice_default.get("voice", "alloy"),
        "format": _voice_default.get("format", "mp3"),
    },
    "ocr": {
        "provider": _ocr_default.get("id", ""),
        "enabled": bool(_ocr_default.get("id")),
        "auto_on_empty_pdf": True,
        "base_url": _ocr_default.get("base_url", ""),
        "api_key_env": _ocr_default.get("api_key_env", ""),
        "requires_api_key": _ocr_default.get("requires_api_key", True),
        "api_key": "",
        "models": list(_ocr_default.get("models", [])),
        "default_model": _ocr_default.get("default_model", ""),
        "ocr_mode": _ocr_default.get("ocr_mode", "vision_chat"),
        "input_types": list(_ocr_default.get("input_types", ["image", "pdf"])),
        "output_format": _ocr_default.get("output_format", "text"),
        "supports_layout": bool(_ocr_default.get("supports_layout", False)),
        "supports_tables": bool(_ocr_default.get("supports_tables", False)),
    },
    "data_sources": [],
    "updated_at": "",
}

API_SCOPES = {"query", "ingest", "speech"}
VOICE_FORMATS = {"mp3", "wav", "opus", "aac", "flac"}
OCR_MODES = {"vision_chat", "ocr_endpoint", "local_engine"}
OCR_INPUT_TYPES = {"image", "pdf"}
DIVERSITY_MODES = {"none", "source_diversity", "mmr"}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _settings_path(path: Optional[str] = None) -> Path:
    configured = path or os.getenv("RAG_SETTINGS_FILE", "app/data/settings.json")
    return Path(configured)


class SettingsStore:
    """Small JSON-backed settings store with atomic writes."""

    _locks: Dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Optional[str] = None):
        self.path = _settings_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locks_guard:
            self._lock = self._locks.setdefault(
                str(self.path.resolve()),
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def save(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            normalized = self._normalize(_deep_merge(DEFAULT_SETTINGS, settings))
            normalized["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._write_unlocked(normalized)
            return normalized

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self._load_unlocked()
            normalized = self._normalize(_deep_merge(DEFAULT_SETTINGS, _deep_merge(current, patch)))
            normalized["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._write_unlocked(normalized)
            return normalized

    def public_view(self) -> Dict[str, Any]:
        settings = self.load()
        view = deepcopy(settings)

        for provider in view.get("custom_providers", []):
            provider["api_key"] = mask_secret(provider.get("api_key", ""))

        for provider in view.get("embedding_providers", []):
            provider["api_key"] = mask_secret(provider.get("api_key", ""))

        for provider in view.get("reranker_providers", []):
            provider["api_key"] = mask_secret(provider.get("api_key", ""))

        for provider in view.get("voice_providers", []):
            provider["api_key"] = mask_secret(provider.get("api_key", ""))

        for provider in view.get("ocr_providers", []):
            provider["api_key"] = mask_secret(provider.get("api_key", ""))

        voice = view.setdefault("voice", {})
        voice["api_key"] = mask_secret(voice.get("api_key", ""))

        ocr = view.setdefault("ocr", {})
        ocr["api_key"] = mask_secret(ocr.get("api_key", ""))

        rag = view.setdefault("rag", {})
        rag["reranker_api_key"] = mask_secret(rag.get("reranker_api_key", ""))
        rag["reranker_regolo_api_key"] = mask_secret(rag.get("reranker_regolo_api_key", ""))

        auth = view.setdefault("auth", {})
        auth["api_keys"] = [
            {**key, "key": mask_secret(key.get("key", ""))}
            for key in auth.get("api_keys", [])
        ]
        return view

    def _write_unlocked(self, settings: Dict[str, Any]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".settings.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _load_unlocked(self) -> Dict[str, Any]:
        if not self.path.exists():
            settings = deepcopy(DEFAULT_SETTINGS)
            self._write_unlocked(settings)
            return settings
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError):
            loaded = {}
        return self._normalize(_deep_merge(DEFAULT_SETTINGS, loaded))

    def _normalize(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        custom_providers = []
        for provider in settings.get("custom_providers", []):
            normalized_provider = normalize_custom_provider(provider)
            if normalized_provider.get("id"):
                custom_providers.append(normalized_provider)
        settings["custom_providers"] = custom_providers

        embedding_providers = []
        for provider in settings.get("embedding_providers", []):
            normalized_provider = normalize_embedding_provider(provider)
            if normalized_provider.get("id"):
                embedding_providers.append(normalized_provider)
        settings["embedding_providers"] = embedding_providers

        reranker_providers = []
        for provider in settings.get("reranker_providers", []):
            normalized_provider = normalize_reranker_provider(provider)
            if normalized_provider.get("id"):
                reranker_providers.append(normalized_provider)
        settings["reranker_providers"] = reranker_providers

        voice_providers = []
        for provider in settings.get("voice_providers", []):
            normalized_provider = normalize_voice_provider(provider)
            if normalized_provider.get("id"):
                voice_providers.append(normalized_provider)
        settings["voice_providers"] = voice_providers
        settings["voice"] = normalize_voice_settings(settings.get("voice", {}), voice_providers)

        ocr_providers = []
        for provider in settings.get("ocr_providers", []):
            normalized_provider = normalize_ocr_provider(provider)
            if normalized_provider.get("id"):
                ocr_providers.append(normalized_provider)
        settings["ocr_providers"] = ocr_providers
        settings["ocr"] = normalize_ocr_settings(settings.get("ocr", {}), ocr_providers)

        data_sources = []
        seen_data_source_ids = set()
        for source in settings.get("data_sources", []):
            normalized_source = normalize_data_source(source)
            source_id = normalized_source.get("id")
            if source_id and source_id not in seen_data_source_ids:
                data_sources.append(normalized_source)
                seen_data_source_ids.add(source_id)
        settings["data_sources"] = data_sources

        rag = settings.setdefault("rag", {})
        rag["chunk_size"] = _int_between(rag.get("chunk_size"), 100, 10000, 1000)
        rag["chunk_overlap"] = _int_between(rag.get("chunk_overlap"), 0, 500, 150)
        rag["query_k"] = _int_between(rag.get("query_k"), 1, 50, 5)
        rag["temperature"] = _float_between(rag.get("temperature"), 0.0, 1.0, 0.3)
        rag["default_provider"] = str(
            rag.get("default_provider") or DEFAULT_SETTINGS["rag"]["default_provider"]
        ).strip()
        rag["default_model"] = str(
            rag.get("default_model") or DEFAULT_SETTINGS["rag"]["default_model"]
        ).strip()
        _normalize_llm_default(settings)
        rag["embedding_provider"] = _embedding_provider_choice(
            rag.get("embedding_provider"),
            settings["embedding_providers"],
        )
        rag["embedding_model"] = _embedding_model_for_provider(
            rag["embedding_provider"],
            rag.get("embedding_model"),
            settings["embedding_providers"],
        )
        rag["cache_ttl"] = _int_between(rag.get("cache_ttl"), 60, 86400, 3600)
        rag["enable_cache"] = _as_bool(rag.get("enable_cache"), True)
        rag["use_internal_knowledge"] = _as_bool(rag.get("use_internal_knowledge"), False)
        rag["reranker_enabled"] = _as_bool(rag.get("reranker_enabled"), False)
        _normalize_reranker_default(settings)
        rag["reranker_top_n"] = _int_between(rag.get("reranker_top_n"), 1, 200, 20)
        rag["reranker_diversity_mode"] = _choice(
            rag.get("reranker_diversity_mode"),
            DIVERSITY_MODES,
            "none",
        )
        rag["reranker_mmr_lambda"] = _float_between(rag.get("reranker_mmr_lambda"), 0.0, 1.0, 0.7)
        rag["reranker_mmr_candidate_pool"] = _int_between(
            rag.get("reranker_mmr_candidate_pool"),
            rag["reranker_top_n"],
            200,
            min(max(rag["reranker_top_n"], rag["reranker_top_n"] * 4), 200),
        )
        rag["reranker_threshold"] = _float_between(rag.get("reranker_threshold"), 0.0, 10.0, 0.0)
        rag["reranker_api_key"] = str(
            rag.get("reranker_api_key") or rag.get("reranker_regolo_api_key") or ""
        ).strip()
        rag["reranker_regolo_api_key"] = str(rag.get("reranker_regolo_api_key", "")).strip()
        rag.pop("reranker_source_diversity", None)
        rag.pop("reranker_mmr_enabled", None)
        rag.pop("reranker_custom_api_key", None)

        auth = settings.setdefault("auth", {})
        api_keys = []
        for item in auth.get("api_keys", []):
            normalized_key = normalize_api_key(item)
            if normalized_key.get("key"):
                api_keys.append(normalized_key)
        auth["api_keys"] = api_keys
        return settings


def get_settings(path: Optional[str] = None) -> Dict[str, Any]:
    return SettingsStore(path).load()


def get_public_settings(path: Optional[str] = None) -> Dict[str, Any]:
    return SettingsStore(path).public_view()


def save_settings(settings: Dict[str, Any], path: Optional[str] = None) -> Dict[str, Any]:
    return SettingsStore(path).save(settings)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def parse_models(raw: Any) -> List[str]:
    if isinstance(raw, list):
        values = raw
    else:
        values = str(raw or "").replace(",", "\n").splitlines()
    seen = set()
    models = []
    for value in values:
        model = str(value).strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def normalize_custom_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = _slug(provider.get("id") or provider.get("name"))
    models = parse_models(provider.get("models", []))
    default_model = str(provider.get("default_model") or (models[0] if models else "")).strip()
    canonical_default = _canonical_model_from_list(models, default_model)
    if canonical_default:
        default_model = canonical_default
    elif default_model and default_model not in models:
        models.insert(0, default_model)

    return {
        "id": provider_id,
        "name": str(provider.get("name") or provider_id).strip(),
        "type": str(provider.get("type") or "openai_compatible").strip(),
        "base_url": str(provider.get("base_url") or "").strip().rstrip("/"),
        "api_key_env": str(provider.get("api_key_env") or "").strip(),
        "requires_api_key": _as_bool(provider.get("requires_api_key"), False),
        "api_key": str(provider.get("api_key") or "").strip(),
        "models": models,
        "default_model": default_model,
        "enabled": _as_bool(provider.get("enabled"), True),
    }


def normalize_data_source(source: Dict[str, Any]) -> Dict[str, Any]:
    source_id = _slug(source.get("id") or source.get("name"))
    plugin = str(source.get("plugin") or "").strip()
    config = source.get("config") if isinstance(source.get("config"), dict) else {}
    secrets_env = source.get("secrets_env") if isinstance(source.get("secrets_env"), dict) else {}
    secrets = source.get("secrets") if isinstance(source.get("secrets"), dict) else {}
    cursor = source.get("cursor") if isinstance(source.get("cursor"), dict) else {}
    sync_interval_seconds = _data_source_sync_interval_seconds(source)
    return {
        "id": source_id,
        "name": str(source.get("name") or source_id).strip(),
        "plugin": plugin,
        "enabled": _as_bool(source.get("enabled"), True),
        "sync_enabled": _as_bool(source.get("sync_enabled"), False),
        "sync_interval_seconds": sync_interval_seconds,
        "config": {str(key): value for key, value in config.items()},
        "secrets_env": {str(key): str(value).strip() for key, value in secrets_env.items()},
        "secrets": {
            str(key): value
            for key, value in secrets.items()
            if isinstance(value, dict) and value.get("ref")
        },
        "cursor": cursor,
        "last_sync": str(source.get("last_sync") or "").strip(),
        "last_sync_status": str(source.get("last_sync_status") or "").strip(),
        "next_sync_at": str(source.get("next_sync_at") or "").strip(),
        "last_error": str(source.get("last_error") or "").strip(),
    }


def _data_source_sync_interval_seconds(source: Dict[str, Any]) -> int:
    if "sync_interval_seconds" in source:
        return _int_between(source.get("sync_interval_seconds"), 0, 2592000, 0)
    try:
        minutes = int(source.get("sync_interval_minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    return _int_between(minutes * 60, 0, 2592000, 0)


def normalize_embedding_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_custom_provider(provider)
    normalized["dimensions"] = _int_between(provider.get("dimensions"), 0, 1000000, 0)
    return normalized


def normalize_reranker_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_custom_provider(provider)
    normalized["reranker_mode"] = _reranker_mode(provider.get("reranker_mode"))
    return normalized


def normalize_voice_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    selected_format = _voice_format(provider.get("format"), "mp3")
    return {
        "id": _slug(provider.get("id") or provider.get("name")),
        "name": str(provider.get("name") or provider.get("id") or "").strip(),
        "type": str(provider.get("type") or "openai_compatible").strip(),
        "base_url": str(provider.get("base_url") or "").strip().rstrip("/"),
        "api_key_env": str(provider.get("api_key_env") or "").strip(),
        "requires_api_key": _as_bool(provider.get("requires_api_key"), True),
        "api_key": str(provider.get("api_key") or "").strip(),
        "stt_model": str(provider.get("stt_model") or "").strip(),
        "stt_language": _voice_language(provider.get("stt_language")),
        "tts_model": str(provider.get("tts_model") or "").strip(),
        "voice": str(provider.get("voice") or "alloy").strip() or "alloy",
        "format": selected_format,
        "enabled": _as_bool(provider.get("enabled"), True),
    }


def normalize_ocr_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    models = parse_models(provider.get("models", []))
    default_model = str(provider.get("default_model") or (models[0] if models else "")).strip()
    canonical_default = _canonical_model_from_list(models, default_model)
    if canonical_default:
        default_model = canonical_default
    elif default_model and default_model not in models:
        models.insert(0, default_model)

    return {
        "id": _slug(provider.get("id") or provider.get("name")),
        "name": str(provider.get("name") or provider.get("id") or "").strip(),
        "type": str(provider.get("type") or "openai_compatible").strip(),
        "base_url": str(provider.get("base_url") or "").strip().rstrip("/"),
        "api_key_env": str(provider.get("api_key_env") or "").strip(),
        "requires_api_key": _as_bool(provider.get("requires_api_key"), True),
        "api_key": str(provider.get("api_key") or "").strip(),
        "models": models,
        "default_model": default_model,
        "ocr_mode": _ocr_mode(provider.get("ocr_mode")),
        "input_types": _ocr_input_types(provider.get("input_types")),
        "output_format": str(provider.get("output_format") or "text").strip() or "text",
        "supports_layout": _as_bool(provider.get("supports_layout"), False),
        "supports_tables": _as_bool(provider.get("supports_tables"), False),
        "enabled": _as_bool(provider.get("enabled"), True),
    }


def normalize_api_key(item: Dict[str, Any]) -> Dict[str, Any]:
    scopes = _normalize_api_scopes(item)
    return {
        "name": str(item.get("name") or "default").strip(),
        "key": str(item.get("key") or "").strip(),
        "enabled": _as_bool(item.get("enabled"), True),
        "scopes": scopes,
        "can_upload": "ingest" in scopes,
    }


def _builtin_voice_defaults() -> Dict[str, Any]:
    return _first_voice_provider()


def normalize_voice_settings(
    settings: Dict[str, Any],
    custom_providers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    providers = _voice_provider_definitions(custom_providers or [])
    selected_provider = str(settings.get("provider") or "").strip()
    if selected_provider not in providers:
        selected_provider = next(iter(providers), selected_provider)
    provider_defaults = providers.get(selected_provider, _builtin_voice_defaults())

    selected_format = _voice_format(
        _voice_value(settings, provider_defaults, "format", "mp3", selected_provider),
        "mp3",
    )
    if "stt_language" in settings:
        stt_language = _voice_language(settings.get("stt_language"))
    else:
        stt_language = _voice_language(
            _voice_value(settings, provider_defaults, "stt_language", "", selected_provider)
        )

    requires_api_key = settings.get("requires_api_key")
    default_voice = DEFAULT_SETTINGS.get("voice", {})
    if (
        selected_provider != str(default_voice.get("provider") or "").strip()
        and requires_api_key == default_voice.get("requires_api_key")
    ):
        requires_api_key = provider_defaults.get("requires_api_key")

    return {
        "provider": selected_provider,
        "name": str(provider_defaults.get("name") or selected_provider).strip(),
        "type": str(provider_defaults.get("type") or "openai_compatible").strip(),
        "enabled": _as_bool(settings.get("enabled"), True),
        "base_url": _voice_value(settings, provider_defaults, "base_url", "", selected_provider).rstrip("/"),
        "api_key_env": _voice_value(settings, provider_defaults, "api_key_env", "", selected_provider),
        "requires_api_key": _as_bool(
            requires_api_key,
            bool(provider_defaults.get("requires_api_key", True)),
        ),
        "api_key": str(settings.get("api_key") or provider_defaults.get("api_key") or "").strip(),
        "stt_model": _voice_value(settings, provider_defaults, "stt_model", "", selected_provider),
        "stt_language": stt_language,
        "tts_model": _voice_value(settings, provider_defaults, "tts_model", "", selected_provider),
        "voice": _voice_value(settings, provider_defaults, "voice", "alloy", selected_provider) or "alloy",
        "format": selected_format,
    }


def _voice_provider_definitions(custom_providers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {}
    for builtin in _builtin_voice():
        provider_id = str(builtin.get("id") or "").strip()
        if provider_id:
            providers[provider_id] = dict(builtin)

    for custom in custom_providers:
        if not custom.get("enabled", True):
            continue
        provider_id = str(custom.get("id") or "").strip()
        if provider_id:
            providers[provider_id] = dict(custom)
    return providers


def _voice_value(
    settings: Dict[str, Any],
    provider_defaults: Dict[str, Any],
    key: str,
    default: str,
    selected_provider: str,
) -> str:
    value = str(settings.get(key) or "").strip()
    provider_value = str(provider_defaults.get(key) or "").strip()
    default_voice = DEFAULT_SETTINGS.get("voice", {})
    default_provider = str(default_voice.get("provider") or "").strip()
    default_value = str(default_voice.get(key) or "").strip()
    if provider_value and selected_provider != default_provider and value == default_value:
        return provider_value
    return value or provider_value or default


def _voice_format(value: Any, default: str) -> str:
    selected = str(value or default).strip().lower()
    return selected if selected in VOICE_FORMATS else default


def _voice_language(value: Any) -> str:
    selected = str(value or "").strip().lower().replace("_", "-")
    if not selected:
        return ""
    if len(selected) > 16:
        return ""
    if not all(char.isalnum() or char == "-" for char in selected):
        return ""
    return selected


def normalize_ocr_settings(
    settings: Dict[str, Any],
    custom_providers: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    providers = _ocr_provider_definitions(custom_providers or [])
    selected_provider = str(settings.get("provider") or "").strip()
    if selected_provider not in providers:
        selected_provider = next(iter(providers), selected_provider)
    provider_defaults = providers.get(selected_provider, _first_ocr_provider())
    models = parse_models(provider_defaults.get("models", []))
    default_ocr = DEFAULT_SETTINGS.get("ocr", {})
    default_provider = str(default_ocr.get("provider") or "").strip()
    configured_default_model = str(settings.get("default_model") or "").strip()
    if (
        selected_provider != default_provider
        and configured_default_model == str(default_ocr.get("default_model") or "").strip()
    ):
        configured_default_model = ""
    default_model = str(
        configured_default_model
        or provider_defaults.get("default_model")
        or (models[0] if models else "")
    ).strip()
    canonical_default = _canonical_model_from_list(models, default_model)
    if canonical_default:
        default_model = canonical_default
    elif default_model and default_model not in models:
        models.insert(0, default_model)

    requires_api_key = settings.get("requires_api_key")
    if (
        selected_provider != default_provider
        and requires_api_key == default_ocr.get("requires_api_key")
    ):
        requires_api_key = provider_defaults.get("requires_api_key")

    return {
        "provider": selected_provider,
        "name": str(provider_defaults.get("name") or selected_provider).strip(),
        "type": str(provider_defaults.get("type") or "openai_compatible").strip(),
        "enabled": _as_bool(settings.get("enabled"), False),
        "auto_on_empty_pdf": _as_bool(settings.get("auto_on_empty_pdf"), True),
        "base_url": _ocr_value(settings, provider_defaults, "base_url", "", selected_provider).rstrip("/"),
        "api_key_env": _ocr_value(settings, provider_defaults, "api_key_env", "", selected_provider),
        "requires_api_key": _as_bool(
            requires_api_key,
            bool(provider_defaults.get("requires_api_key", True)),
        ),
        "api_key": str(settings.get("api_key") or provider_defaults.get("api_key") or "").strip(),
        "models": models,
        "default_model": default_model,
        "ocr_mode": _ocr_mode(settings.get("ocr_mode") or provider_defaults.get("ocr_mode")),
        "input_types": _ocr_input_types(settings.get("input_types") or provider_defaults.get("input_types")),
        "output_format": str(settings.get("output_format") or provider_defaults.get("output_format") or "text").strip(),
        "supports_layout": _as_bool(
            settings.get("supports_layout"),
            bool(provider_defaults.get("supports_layout", False)),
        ),
        "supports_tables": _as_bool(
            settings.get("supports_tables"),
            bool(provider_defaults.get("supports_tables", False)),
        ),
    }


def _ocr_provider_definitions(custom_providers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {}
    for builtin in _builtin_ocr():
        provider_id = str(builtin.get("id") or "").strip()
        if provider_id:
            providers[provider_id] = dict(builtin)

    for custom in custom_providers:
        if not custom.get("enabled", True):
            continue
        provider_id = str(custom.get("id") or "").strip()
        if provider_id:
            providers[provider_id] = dict(custom)
    return providers


def _ocr_value(
    settings: Dict[str, Any],
    provider_defaults: Dict[str, Any],
    key: str,
    default: str,
    selected_provider: str,
) -> str:
    value = str(settings.get(key) or "").strip()
    provider_value = str(provider_defaults.get(key) or "").strip()
    default_ocr = DEFAULT_SETTINGS.get("ocr", {})
    default_provider = str(default_ocr.get("provider") or "").strip()
    default_value = str(default_ocr.get(key) or "").strip()
    if provider_value and selected_provider != default_provider and value == default_value:
        return provider_value
    return value or provider_value or default


def _ocr_mode(value: Any) -> str:
    selected = str(value or "vision_chat").strip().lower().replace("-", "_")
    if selected in {"vision", "vision_chat", "chat_completions"}:
        return "vision_chat"
    if selected in {"ocr", "ocr_endpoint", "dedicated_ocr"}:
        return "ocr_endpoint"
    if selected in {"local", "local_engine"}:
        return "local_engine"
    return "vision_chat"


def _ocr_input_types(value: Any) -> List[str]:
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = str(value or "").replace(",", "\n").splitlines()
    result = []
    for item in raw_values:
        normalized = str(item).strip().lower()
        if normalized in OCR_INPUT_TYPES and normalized not in result:
            result.append(normalized)
    return result or ["image", "pdf"]


def _slug(value: Any) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or ""))
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:80]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _normalize_api_scopes(item: Dict[str, Any]) -> List[str]:
    raw_scopes = item.get("scopes")
    scopes: List[str] = []
    if isinstance(raw_scopes, str):
        values = raw_scopes.replace(",", "\n").splitlines()
    elif isinstance(raw_scopes, list):
        values = raw_scopes
    else:
        values = []

    for value in values:
        scope = str(value).strip().lower()
        if scope in API_SCOPES and scope not in scopes:
            scopes.append(scope)

    if not scopes:
        scopes = ["query"]
        if _as_bool(item.get("can_upload"), False):
            scopes.append("ingest")

    return scopes


def _int_between(value: Any, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _float_between(value: Any, min_value: float, max_value: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _choice(value: Any, choices: set[str], default: str) -> str:
    selected = str(value or default).strip()
    return selected if selected in choices else default


def _reranker_mode(value: Any) -> str:
    selected = str(value or "chat_completions").strip().lower().replace("-", "_")
    if selected in {"chat", "chat_completion", "chat_completions"}:
        return "chat_completions"
    if selected in {"rerank", "reranker", "rerank_endpoint"}:
        return "rerank"
    if selected == "auto":
        return "auto"
    return "chat_completions"


def _embedding_builtin_ids() -> List[str]:
    """Return IDs of built-in embedding providers from JSON."""
    return [p["id"] for p in _builtin_embedding() if p.get("id")]


def _embedding_builtin_model(provider_id: str) -> str:
    """Get the default model for a built-in embedding provider."""
    for p in _builtin_embedding():
        if p.get("id") == provider_id:
            return p.get("default_model", "")
    return ""


def _embedding_provider_choice(value: Any, embedding_providers: List[Dict[str, Any]]) -> str:
    builtin_ids = _embedding_builtin_ids()
    selected = str(value or builtin_ids[0] if builtin_ids else "local").strip()

    # Check built-in providers
    if selected in builtin_ids:
        return selected

    # Check local (always available as fallback)
    if selected == "local":
        return selected

    # Check custom providers
    custom_provider_ids = {
        provider["id"]
        for provider in embedding_providers
        if provider.get("id") and provider.get("enabled", True)
    }
    if selected in custom_provider_ids:
        return selected

    # Fallback to first builtin or local
    return builtin_ids[0] if builtin_ids else "local"


def _embedding_model_for_provider(
    provider: str,
    model: Any,
    embedding_providers: Optional[List[Dict[str, Any]]] = None,
) -> str:
    selected = str(model or "").strip()

    # Local provider
    if provider == "local":
        return selected if selected.startswith("sentence-transformers/") else "sentence-transformers/all-MiniLM-L6-v2"

    # Built-in providers (from JSON)
    builtin_model = _embedding_builtin_model(provider)
    if builtin_model:
        if selected and (selected == builtin_model or selected.lower() == builtin_model.lower()):
            return builtin_model
        return builtin_model

    # Custom providers (from settings)
    for custom in embedding_providers or []:
        if custom.get("id") != provider or not custom.get("enabled", True):
            continue

        models = [str(m) for m in custom.get("models", [])]
        canonical = _canonical_model_from_list(models, selected)
        if canonical:
            return canonical
        return str(custom.get("default_model") or (models[0] if models else selected))

    # Ultimate fallback
    fallback = _embedding_builtin_model("local") or "sentence-transformers/all-MiniLM-L6-v2"
    return fallback


def _reranker_provider_definitions(custom_providers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {
        "local": {
            "id": "local",
            "name": "Local BGE",
            "models": ["BAAI/bge-reranker-v2-m3"],
            "default_model": "BAAI/bge-reranker-v2-m3",
        }
    }
    for builtin in _builtin_reranker():
        provider_id = str(builtin.get("id") or "").strip()
        if not provider_id:
            continue
        providers[provider_id] = dict(builtin)

    for custom in custom_providers:
        if not custom.get("enabled", True):
            continue
        providers[custom["id"]] = {
            "id": custom["id"],
            "name": custom.get("name") or custom["id"],
            "models": custom.get("models", []),
            "default_model": custom.get("default_model", ""),
            "base_url": custom.get("base_url", ""),
            "api_key": custom.get("api_key", ""),
            "reranker_mode": custom.get("reranker_mode", "chat_completions"),
        }
    return providers


def _normalize_reranker_default(settings: Dict[str, Any]) -> None:
    rag = settings.setdefault("rag", {})
    providers = _reranker_provider_definitions(settings.get("reranker_providers", []))
    selected_value = str(
        rag.get("reranker_model") or DEFAULT_SETTINGS["rag"]["reranker_model"]
    ).strip()
    selected_type = str(rag.get("reranker_type") or DEFAULT_SETTINGS["rag"]["reranker_type"]).strip()

    if "/" in selected_value:
        possible_provider, possible_model = selected_value.split("/", 1)
        if possible_provider in providers:
            selected_type = possible_provider
            selected_value = possible_model

    if selected_type not in providers:
        selected_type = DEFAULT_SETTINGS["rag"]["reranker_type"]
    if selected_type not in providers:
        selected_type = next(iter(providers))

    provider = providers[selected_type]
    models = [str(model) for model in provider.get("models", [])]
    canonical = _canonical_model_from_list(models, selected_value)
    if canonical:
        selected_model = canonical
    else:
        selected_model = str(provider.get("default_model") or (models[0] if models else selected_value))

    rag["reranker_type"] = selected_type
    if selected_type == "local":
        rag["reranker_model"] = f"local/{selected_model}" if selected_model else "local/"
    else:
        rag["reranker_model"] = f"{selected_type}/{selected_model}" if selected_model else selected_type


def _normalize_llm_default(settings: Dict[str, Any]) -> None:
    rag = settings.setdefault("rag", {})
    providers = _llm_provider_definitions(settings.get("custom_providers", []))
    if not providers:
        return

    provider_id = str(rag.get("default_provider") or DEFAULT_SETTINGS["rag"]["default_provider"]).strip()
    selected_model = str(rag.get("default_model") or "").strip()

    canonical = _find_canonical_model(providers, selected_model, preferred_provider=provider_id)
    if canonical:
        rag["default_provider"], rag["default_model"] = canonical
        return

    if provider_id not in providers:
        provider_id = DEFAULT_SETTINGS["rag"]["default_provider"]
    if provider_id not in providers:
        provider_id = next(iter(providers))

    provider = providers[provider_id]
    models = [str(model) for model in provider.get("models", [])]
    rag["default_provider"] = provider_id
    rag["default_model"] = str(provider.get("default_model") or (models[0] if models else selected_model))


def _llm_provider_definitions(custom_providers: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    providers: Dict[str, Dict[str, Any]] = {}
    try:
        from .model_defaults import load_builtin_provider_definitions

        providers.update(
            {provider_id: dict(provider) for provider_id, provider in load_builtin_provider_definitions().items()}
        )
    except Exception:
        return providers

    for custom in custom_providers:
        if not custom.get("enabled", True):
            continue
        providers[custom["id"]] = {
            "name": custom.get("name") or custom["id"],
            "type": "openai_compatible",
            "models": custom.get("models", []),
            "default_model": custom.get("default_model", ""),
        }
    return providers


def _find_canonical_model(
    providers: Dict[str, Dict[str, Any]],
    selected_model: str,
    preferred_provider: str,
) -> Optional[tuple[str, str]]:
    if not selected_model:
        return None

    if preferred_provider in providers:
        model = _canonical_model_from_list(providers[preferred_provider].get("models", []), selected_model)
        if model:
            return preferred_provider, model

    for provider_id, provider in providers.items():
        model = _canonical_model_from_list(provider.get("models", []), selected_model)
        if model:
            return provider_id, model
    return None


def _canonical_model_from_list(models: List[Any], selected_model: str) -> Optional[str]:
    selected = str(selected_model or "").strip()
    if not selected:
        return None

    canonical_models = [str(model) for model in models]
    if selected in canonical_models:
        return selected

    selected_lower = selected.lower()
    matches = [model for model in canonical_models if model.lower() == selected_lower]
    if len(matches) == 1:
        return matches[0]
    return None
