from types import SimpleNamespace

from app.utils.voice_provider import OpenAICompatibleVoiceProvider


def test_transcribe_passes_configured_stt_language(tmp_path):
    captured = {}

    class FakeTranscriptions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="ciao")

    provider = object.__new__(OpenAICompatibleVoiceProvider)
    provider.stt_model = "whisper-1"
    provider.stt_language = "it"
    provider.client = SimpleNamespace(
        audio=SimpleNamespace(transcriptions=FakeTranscriptions())
    )
    audio_path = tmp_path / "audio.webm"
    audio_path.write_bytes(b"audio")

    transcript = provider.transcribe(str(audio_path))

    assert transcript == "ciao"
    assert captured["model"] == "whisper-1"
    assert captured["language"] == "it"
