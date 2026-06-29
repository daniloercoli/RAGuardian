from typing import Any

from utils.providers.exceptions import ProviderError
from utils.provider_config import client_api_key, normalize_base_url, requires_api_key, resolve_api_key
from utils.settings_store import get_settings


CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
}


class OpenAICompatibleVoiceProvider:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.base_url = normalize_base_url(config.get("base_url"))
        self.api_key = client_api_key(config, placeholder="voice")
        self.requires_api_key = requires_api_key(config, default=False)
        self.stt_model = config.get("stt_model", "")
        self.stt_language = str(config.get("stt_language") or "").strip()
        self.tts_model = config.get("tts_model", "")
        self.default_voice = config.get("voice", "alloy")
        self.default_format = config.get("format", "mp3")

        if not self.base_url:
            raise ProviderError("Voice provider base_url is not configured")
        if self.requires_api_key and self.api_key == "voice":
            raise ProviderError("Voice provider API key is not configured")

        from openai import OpenAI

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def transcribe(self, file_path: str, language: str | None = None) -> str:
        if not self.stt_model:
            raise ProviderError("STT model is not configured")

        try:
            with open(file_path, "rb") as audio_file:
                payload = {"model": self.stt_model, "file": audio_file}
                selected_language = self.stt_language if language is None else str(language or "").strip()
                if selected_language:
                    payload["language"] = selected_language
                result = self.client.audio.transcriptions.create(**payload)
        except Exception as exc:
            raise ProviderError(f"STT provider error: {exc}") from exc

        text = getattr(result, "text", None)
        if text is None and isinstance(result, dict):
            text = result.get("text")
        return str(text or "").strip()

    def synthesize(self, text: str, voice: str | None = None, audio_format: str | None = None) -> bytes:
        if not self.tts_model:
            raise ProviderError("TTS model is not configured")

        selected_format = audio_format or self.default_format
        try:
            result = self.client.audio.speech.create(
                model=self.tts_model,
                voice=voice or self.default_voice,
                input=text,
                response_format=selected_format,
            )
        except Exception as exc:
            raise ProviderError(f"TTS provider error: {exc}") from exc

        if hasattr(result, "content"):
            return bytes(result.content)
        if hasattr(result, "read"):
            return result.read()
        if isinstance(result, bytes):
            return result
        raise ProviderError("TTS provider returned an unsupported response")


def get_voice_provider(settings: dict | None = None) -> OpenAICompatibleVoiceProvider:
    config = (settings or get_settings()).get("voice", {})
    if not config.get("enabled"):
        raise ProviderError("Voice provider is disabled")
    return OpenAICompatibleVoiceProvider(config)


def voice_readiness(settings: dict) -> dict:
    config = settings.get("voice", {})
    enabled = bool(config.get("enabled"))
    has_base = bool(config.get("base_url"))
    has_key = voice_has_api_key(config)
    requires_key = bool(config.get("requires_api_key", False))
    provider_name = (config.get("provider") or "openai-compatible") if enabled and has_base else ""
    return {
        "voice_provider": provider_name,
        "stt_ready": bool(enabled and has_base and (has_key or not requires_key) and config.get("stt_model")),
        "tts_ready": bool(enabled and has_base and (has_key or not requires_key) and config.get("tts_model")),
    }


def content_type_for_format(audio_format: str) -> str:
    return CONTENT_TYPES.get(audio_format, "application/octet-stream")


def voice_has_api_key(config: dict[str, Any]) -> bool:
    return bool(resolve_api_key(config))
