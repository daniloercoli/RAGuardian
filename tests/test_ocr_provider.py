from types import SimpleNamespace

from app.utils import ocr_provider
from app.utils.ocr_provider import OpenAICompatibleOCRProvider


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=response),
                )
            ]
        )


class FakeClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeDocument:
    def __init__(self, page_count):
        self.page_count = page_count

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __len__(self):
        return self.page_count


def _provider(responses, **overrides):
    provider = OpenAICompatibleOCRProvider(
        {
            "provider": "vision-ocr",
            "base_url": "https://ocr.example.com/v1",
            "requires_api_key": False,
            "models": ["vision-model"],
            "default_model": "vision-model",
            "input_types": ["image", "pdf"],
            **overrides,
        }
    )
    client = FakeClient(responses)
    provider.client = client
    return provider, client


def test_vision_chat_sends_explicit_positive_max_tokens():
    provider, client = _provider(["Extracted text"])

    text = provider._extract_with_vision_chat(["data:image/png;base64,abc"])

    assert text == "Extracted text"
    assert client.completions.calls[0]["max_tokens"] == 2048


def test_pdf_ocr_sends_one_page_per_provider_request(monkeypatch):
    import fitz

    provider, client = _provider(["Page one", "Page two", "Page three"])
    monkeypatch.setattr(fitz, "open", lambda file_path: FakeDocument(3))
    monkeypatch.setattr(
        ocr_provider,
        "_pdf_page_to_data_url",
        lambda document, page_index, scale: f"data:image/png;base64,page-{page_index}-scale-{scale}",
    )

    text = provider.extract_text("scan.pdf")

    assert text == "Page one\n\nPage two\n\nPage three"
    assert len(client.completions.calls) == 3
    for call in client.completions.calls:
        content = call["messages"][0]["content"]
        image_parts = [part for part in content if part["type"] == "image_url"]
        assert len(image_parts) == 1


def test_pdf_ocr_retries_with_smaller_render_scale_on_token_errors(monkeypatch):
    import fitz

    provider, client = _provider(
        [
            RuntimeError("max_tokens must be at least 1, got -807"),
            "Recovered page text",
        ]
    )
    scales = []
    monkeypatch.setattr(fitz, "open", lambda file_path: FakeDocument(1))

    def fake_page_url(document, page_index, scale):
        scales.append(scale)
        return f"data:image/png;base64,page-{page_index}-scale-{scale}"

    monkeypatch.setattr(ocr_provider, "_pdf_page_to_data_url", fake_page_url)

    text = provider.extract_text("scan.pdf")

    assert text == "Recovered page text"
    assert scales == [1.0, 0.75]
    assert len(client.completions.calls) == 2
