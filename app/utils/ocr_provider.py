import base64
import mimetypes
import os
from typing import Any

from utils.provider_config import client_api_key, default_model, normalize_base_url, requires_api_key, resolve_api_key
from utils.providers.exceptions import ProviderError
from utils.settings_store import get_settings


IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"}
OCR_EXTENSIONS = IMAGE_EXTENSIONS | {"pdf"}


class OpenAICompatibleOCRProvider:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.provider_id = str(config.get("provider") or config.get("id") or "openai-compatible")
        self.provider_name = str(config.get("name") or self.provider_id)
        self.base_url = normalize_base_url(config.get("base_url"))
        self.api_key = resolve_api_key(config)
        self.requires_api_key = requires_api_key(config, default=True)
        self.model = default_model(config)
        self.ocr_mode = str(config.get("ocr_mode") or "vision_chat")
        self.input_types = set(config.get("input_types") or ["image", "pdf"])
        self.max_pages = _int_between(config.get("max_pages"), 1, 50, 8)
        self.render_scale = _float_between(config.get("render_scale"), 0.5, 4.0, 1.0)
        self.max_output_tokens = _int_between(
            config.get("max_output_tokens") or config.get("max_tokens"),
            1,
            8192,
            2048,
        )

        if not self.base_url:
            raise ProviderError("OCR provider base_url is not configured")
        if self.requires_api_key and not self.api_key:
            raise ProviderError("OCR provider API key is not configured")
        if not self.model:
            raise ProviderError("OCR provider model is not configured")
        if self.ocr_mode != "vision_chat":
            raise ProviderError(f"OCR mode '{self.ocr_mode}' is not supported by the OpenAI-compatible OCR adapter")

        from openai import OpenAI

        self.client = OpenAI(api_key=client_api_key(config, placeholder="ocr"), base_url=self.base_url)

    def extract_text(self, file_path: str) -> str:
        extension = _extension(file_path)
        if extension in IMAGE_EXTENSIONS:
            if "image" not in self.input_types:
                raise ProviderError("OCR provider does not accept image inputs")
            image_urls = [_image_file_to_data_url(file_path)]
        elif extension == "pdf":
            if "pdf" not in self.input_types:
                raise ProviderError("OCR provider does not accept PDF inputs")
            return self._extract_pdf_with_vision_chat(file_path)
        else:
            raise ProviderError(f"OCR input type '{extension}' is not supported")

        if not image_urls:
            raise ProviderError("No OCR input pages generated")
        return self._extract_with_vision_chat(image_urls)

    def _extract_pdf_with_vision_chat(self, file_path: str) -> str:
        try:
            import fitz
        except ImportError as exc:
            raise ProviderError("PDF OCR requires PyMuPDF. Install the project dependencies again.") from exc

        parts: list[str] = []
        with fitz.open(file_path) as document:
            page_count = min(len(document), self.max_pages)
            for page_index in range(page_count):
                text = self._extract_pdf_page(document, page_index)
                if text:
                    parts.append(text)
        if not parts:
            raise ProviderError("OCR did not extract text from the PDF")
        return "\n\n".join(parts).strip()

    def _extract_pdf_page(self, document: Any, page_index: int) -> str:
        last_error = ""
        for scale in _retry_render_scales(self.render_scale):
            image_url = _pdf_page_to_data_url(document, page_index, scale=scale)
            try:
                return self._extract_with_vision_chat([image_url])
            except ProviderError as exc:
                last_error = str(exc)
                if _should_retry_with_smaller_image(last_error):
                    continue
                raise
        raise ProviderError(f"OCR provider error on page {page_index + 1}: {last_error}")

    def _extract_with_vision_chat(self, image_urls: list[str]) -> str:
        prompt = str(
            self.config.get("prompt")
            or "Extract all readable text from the provided document images. "
            "Preserve reading order. Return only the extracted text."
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": image_url}})

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
                max_tokens=self.max_output_tokens,
            )
        except Exception as exc:
            raise ProviderError(f"OCR provider error: {exc}") from exc

        message = response.choices[0].message if response.choices else None
        return str(getattr(message, "content", "") or "").strip()


def get_ocr_provider(settings: dict | None = None) -> OpenAICompatibleOCRProvider:
    config = (settings or get_settings()).get("ocr", {})
    if not config.get("enabled"):
        raise ProviderError("OCR provider is disabled")
    return OpenAICompatibleOCRProvider(config)


def ocr_readiness(settings: dict) -> dict:
    config = settings.get("ocr", {})
    enabled = bool(config.get("enabled"))
    has_base = bool(config.get("base_url"))
    requires_key = bool(config.get("requires_api_key", True))
    has_key = ocr_has_api_key(config)
    has_model = bool(config.get("default_model"))
    ready = bool(enabled and has_base and has_model and (has_key or not requires_key))
    return {
        "ocr_provider": (config.get("provider") or "") if ready else "",
        "ocr_ready": ready,
    }


def ocr_has_api_key(config: dict[str, Any]) -> bool:
    return bool(resolve_api_key(config))


def _image_file_to_data_url(file_path: str) -> str:
    mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
    with open(file_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _pdf_to_data_urls(file_path: str, max_pages: int, scale: float) -> list[str]:
    try:
        import fitz
    except ImportError as exc:
        raise ProviderError("PDF OCR requires PyMuPDF. Install the project dependencies again.") from exc

    urls: list[str] = []
    with fitz.open(file_path) as document:
        for page_index in range(min(len(document), max_pages)):
            urls.append(_pdf_page_to_data_url(document, page_index, scale=scale))
    return urls


def _pdf_page_to_data_url(document: Any, page_index: int, scale: float) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise ProviderError("PDF OCR requires PyMuPDF. Install the project dependencies again.") from exc

    page = document.load_page(page_index)
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    png_bytes = pixmap.tobytes("png")
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _extension(file_path: str) -> str:
    return os.path.splitext(file_path.lower())[1].lstrip(".")


def _retry_render_scales(initial_scale: float) -> list[float]:
    initial = _float_between(initial_scale, 0.5, 4.0, 1.0)
    scales: list[float] = []
    for scale in (initial, 1.0, 0.75, 0.5):
        if scale > initial:
            continue
        if scale not in scales:
            scales.append(scale)
    return scales


def _should_retry_with_smaller_image(error: str) -> bool:
    normalized = str(error or "").lower()
    return any(
        marker in normalized
        for marker in (
            "max_tokens must be at least 1",
            "context length",
            "context window",
            "maximum context",
            "too many tokens",
            "input too large",
        )
    )


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
