import pytest

from app.utils.providers import sentence_transformer_provider as st_provider


def test_local_embedding_provider_reports_old_torch_on_macos_x86(monkeypatch):
    monkeypatch.setattr(st_provider, "version", lambda package: "2.2.2")
    monkeypatch.setattr(st_provider.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(st_provider.platform, "machine", lambda: "x86_64")

    with pytest.raises(ValueError) as exc:
        st_provider._ensure_modern_torch_available()

    message = str(exc.value)
    assert "torch 2.2.2" in message
    assert "macOS x86_64" in message
    assert "Provider embeddings=regolo" in message
    assert "testo dei documenti e delle query viene inviato a Regolo" in message
