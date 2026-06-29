import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .provider_config import OPENAI_COMPATIBLE_TYPE, normalize_base_url


DEFAULT_PROVIDER_CONFIG_FILENAME = "default_providers.json"


class ModelConfigurationError(RuntimeError):
    """Raised when the built-in provider/model JSON is missing or invalid."""


def default_provider_config_path(path: Optional[str] = None) -> Path:
    configured = path or os.getenv("RAG_DEFAULT_PROVIDERS_FILE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / DEFAULT_PROVIDER_CONFIG_FILENAME


def _load_raw(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and return the raw JSON from the config file."""
    config_path = default_provider_config_path(path)
    if not config_path.exists():
        raise ModelConfigurationError(_missing_file_message(config_path))

    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ModelConfigurationError(
            f"Configurazione modelli non valida in {config_path}: JSON non valido ({e})."
        ) from e

    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# LLM providers (returns dict keyed by provider ID)
# ---------------------------------------------------------------------------

def load_builtin_provider_definitions(path: Optional[str] = None) -> Dict[str, Dict[str, object]]:
    """Load LLM provider definitions.

    Supports:
      - New format:  {"llm": [{id, models, ...}, ...], ...}
      - Legacy format: {"providers": {id: {models, ...}, ...}}
    """
    raw = _load_raw(path)

    # New format: llm array
    llm_list = raw.get("llm")
    if isinstance(llm_list, list):
        return _providers_from_list(llm_list)

    # Legacy format: providers dict (backward compat)
    providers = raw.get("providers", raw) if isinstance(raw, dict) else {}
    if not isinstance(providers, dict):
        raise ModelConfigurationError(_empty_models_message(default_provider_config_path(path)))

    normalized: Dict[str, dict] = {}
    total_models = 0
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        entry: Dict[str, Any] = {**provider, "id": str(provider_id)}
        normalized[str(provider_id)] = _normalize_llm_provider(entry)
        total_models += len(normalized[str(provider_id)]["models"])

    if not normalized or total_models == 0:
        raise ModelConfigurationError(_empty_models_message(default_provider_config_path(path)))

    return normalized


def _providers_from_list(providers: List[Any]) -> Dict[str, Dict[str, object]]:
    normalized: Dict[str, dict] = {}
    total_models = 0
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id", ""))
        if not provider_id:
            continue
        entry = _normalize_llm_provider({**provider, "id": provider_id})
        normalized[provider_id] = entry
        total_models += len(entry.get("models", []))

    if not normalized or total_models == 0:
        raise ModelConfigurationError(_empty_models_message(default_provider_config_path(None)))

    return normalized


def _normalize_llm_provider(entry: Dict[str, Any]) -> Dict[str, Any]:
    provider_id = str(entry.get("id", ""))
    models = _normalize_models(entry.get("models"))
    if not models:
        return {**entry, "models": [], "default_model": ""}

    default_model = str(entry.get("default_model") or models[0]).strip()
    if default_model not in models:
        models.insert(0, default_model)

    normalized = {
        **entry,
        "id": provider_id,
        "name": str(entry.get("name") or provider_id),
        "type": str(entry.get("type") or OPENAI_COMPATIBLE_TYPE),
        "base_url": normalize_base_url(entry.get("base_url")),
        "requires_api_key": bool(entry.get("requires_api_key", True)),
        "api_key_env": str(entry.get("api_key_env") or ""),
        "billing_note": str(entry.get("billing_note") or ""),
        "models": models,
        "default_model": default_model,
    }
    return normalized


# ---------------------------------------------------------------------------
# Embedding providers (returns list of dicts)
# ---------------------------------------------------------------------------

def load_builtin_embedding_providers(path: Optional[str] = None) -> List[Dict[str, Any]]:
    raw = _load_raw(path)
    embedding_list = raw.get("embedding", raw.get("providers", {}).get("embedding", []))
    if not isinstance(embedding_list, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in embedding_list:
        if not isinstance(entry, dict):
            continue
        result.append({
            **entry,
            "id": str(entry.get("id", "")),
            "name": str(entry.get("name", "")),
            "type": str(entry.get("type") or OPENAI_COMPATIBLE_TYPE),
            "base_url": normalize_base_url(entry.get("base_url")),
            "requires_api_key": bool(entry.get("requires_api_key", True)),
            "api_key_env": str(entry.get("api_key_env", "")),
            "models": _normalize_models(entry.get("models")),
            "default_model": str(entry.get("default_model", "")),
            "dimensions": int(entry.get("dimensions", 0)),
            "privacy_note": str(entry.get("privacy_note", "")),
        })
    return result


# ---------------------------------------------------------------------------
# Reranker providers (returns list of dicts)
# ---------------------------------------------------------------------------

def load_builtin_reranker_providers(path: Optional[str] = None) -> List[Dict[str, Any]]:
    raw = _load_raw(path)
    reranker_list = raw.get("reranker", [])
    if not isinstance(reranker_list, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in reranker_list:
        if not isinstance(entry, dict):
            continue
        result.append({
            **entry,
            "id": str(entry.get("id", "")),
            "name": str(entry.get("name", "")),
            "type": str(entry.get("type") or OPENAI_COMPATIBLE_TYPE),
            "base_url": normalize_base_url(entry.get("base_url")),
            "requires_api_key": bool(entry.get("requires_api_key", True)),
            "api_key_env": str(entry.get("api_key_env", "")),
            "models": _normalize_models(entry.get("models")),
            "default_model": str(entry.get("default_model", "")),
            "reranker_mode": _normalize_reranker_mode(entry.get("reranker_mode")),
        })
    return result


# ---------------------------------------------------------------------------
# Voice providers (returns list of dicts)
# ---------------------------------------------------------------------------

def load_builtin_voice_providers(path: Optional[str] = None) -> List[Dict[str, Any]]:
    raw = _load_raw(path)
    voice_list = raw.get("voice", [])
    if not isinstance(voice_list, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in voice_list:
        if not isinstance(entry, dict):
            continue
        result.append({
            **entry,
            "id": str(entry.get("id", "")),
            "name": str(entry.get("name", "")),
            "type": str(entry.get("type") or OPENAI_COMPATIBLE_TYPE),
            "requires_api_key": bool(entry.get("requires_api_key", True)),
            "api_key_env": str(entry.get("api_key_env", "")),
            "base_url": normalize_base_url(entry.get("base_url")),
            "stt_model": str(entry.get("stt_model", "")),
            "stt_language": str(entry.get("stt_language", "")),
            "tts_model": str(entry.get("tts_model", "")),
            "voice": str(entry.get("voice", "alloy")),
            "format": str(entry.get("format", "mp3")),
        })
    return result


# ---------------------------------------------------------------------------
# OCR providers (returns list of dicts)
# ---------------------------------------------------------------------------

def load_builtin_ocr_providers(path: Optional[str] = None) -> List[Dict[str, Any]]:
    raw = _load_raw(path)
    ocr_list = raw.get("ocr", [])
    if not isinstance(ocr_list, list):
        return []
    result: List[Dict[str, Any]] = []
    for entry in ocr_list:
        if not isinstance(entry, dict):
            continue
        result.append({
            **entry,
            "id": str(entry.get("id", "")),
            "name": str(entry.get("name", "")),
            "type": str(entry.get("type") or OPENAI_COMPATIBLE_TYPE),
            "requires_api_key": bool(entry.get("requires_api_key", True)),
            "api_key_env": str(entry.get("api_key_env", "")),
            "base_url": normalize_base_url(entry.get("base_url")),
            "models": _normalize_models(entry.get("models")),
            "default_model": str(entry.get("default_model", "")),
            "ocr_mode": _normalize_ocr_mode(entry.get("ocr_mode")),
            "input_types": _normalize_values(entry.get("input_types"), ["image", "pdf"]),
            "output_format": str(entry.get("output_format") or "text"),
            "supports_layout": bool(entry.get("supports_layout", False)),
            "supports_tables": bool(entry.get("supports_tables", False)),
        })
    return result


# ---------------------------------------------------------------------------
# Unified loader (returns dict with all categories)
# ---------------------------------------------------------------------------

def load_builtin_defaults(path: Optional[str] = None) -> Dict[str, Any]:
    return {
        "llm": load_builtin_provider_definitions(path),
        "embedding": load_builtin_embedding_providers(path),
        "reranker": load_builtin_reranker_providers(path),
        "voice": load_builtin_voice_providers(path),
        "ocr": load_builtin_ocr_providers(path),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_model_configuration_error(path: Optional[str] = None) -> Optional[str]:
    try:
        load_builtin_provider_definitions(path)
        return None
    except ModelConfigurationError as e:
        return str(e)


def _normalize_models(raw_models) -> list[str]:
    if isinstance(raw_models, list):
        values = raw_models
    else:
        values = str(raw_models or "").replace(",", "\n").splitlines()

    models = []
    seen = set()
    for value in values:
        model = str(value).strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def _normalize_values(raw_values, default: list[str]) -> list[str]:
    if isinstance(raw_values, list):
        values = raw_values
    else:
        values = str(raw_values or "").replace(",", "\n").splitlines()

    normalized = []
    for value in values:
        item = str(value).strip().lower()
        if item and item not in normalized:
            normalized.append(item)
    return normalized or list(default)


def _normalize_reranker_mode(value: Any) -> str:
    selected = str(value or "chat_completions").strip().lower().replace("-", "_")
    if selected in {"chat", "chat_completion", "chat_completions"}:
        return "chat_completions"
    if selected in {"rerank", "reranker", "rerank_endpoint"}:
        return "rerank"
    if selected == "auto":
        return "auto"
    return "chat_completions"


def _normalize_ocr_mode(value: Any) -> str:
    selected = str(value or "vision_chat").strip().lower().replace("-", "_")
    if selected in {"vision", "vision_chat", "chat_completions"}:
        return "vision_chat"
    if selected in {"ocr", "ocr_endpoint", "dedicated_ocr"}:
        return "ocr_endpoint"
    if selected in {"local", "local_engine"}:
        return "local_engine"
    return "vision_chat"


def _missing_file_message(config_path: Path) -> str:
    return (
        f"File configurazione provider/modelli non trovato: {config_path}. "
        f"Ripristina o crea il file {DEFAULT_PROVIDER_CONFIG_FILENAME} con almeno un provider e un modello. "
        "I provider distribuiti di default sono Mistral e Regolo, entrambi richiedono una API key propria. "
        "Dopo l'avvio puoi configurare provider custom e parametri runtime da /admin/config."
    )


def _empty_models_message(config_path: Path) -> str:
    return (
        f"Nessun modello configurato in {config_path}. "
        "Aggiungi almeno un modello nel JSON alla chiave llm[].models. "
        "Il progetto viene distribuito con Mistral e Regolo di default, ma entrambi richiedono una API key propria. "
        "Per provider custom e parametri runtime usa /admin/config."
    )


# ---------------------------------------------------------------------------
# Module-level cached instances
# ---------------------------------------------------------------------------

try:
    BUILTIN_PROVIDER_DEFINITIONS = load_builtin_provider_definitions()
    BUILTIN_DEFAULTS = load_builtin_defaults()
    BUILTIN_EMBEDDING = BUILTIN_DEFAULTS.get("embedding", [])
    BUILTIN_RERANKER = BUILTIN_DEFAULTS.get("reranker", [])
    BUILTIN_VOICE = BUILTIN_DEFAULTS.get("voice", [])
    BUILTIN_OCR = BUILTIN_DEFAULTS.get("ocr", [])
except ModelConfigurationError:
    BUILTIN_PROVIDER_DEFINITIONS = {}
    BUILTIN_DEFAULTS = {"llm": {}, "embedding": [], "reranker": [], "voice": [], "ocr": []}
    BUILTIN_EMBEDDING = []
    BUILTIN_RERANKER = []
    BUILTIN_VOICE = []
    BUILTIN_OCR = []
