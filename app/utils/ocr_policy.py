from dataclasses import dataclass
from typing import Any

from utils.ocr_provider import ocr_has_api_key


@dataclass(frozen=True)
class PdfOcrDecision:
    should_run: bool
    reason: str
    error_message: str = ""


def decide_pdf_ocr_for_ingestion(
    settings: dict[str, Any],
    parsed_documents: list[Any] | None,
    parse_error: str = "",
) -> PdfOcrDecision:
    """Decide whether a PDF ingestion should fall back to OCR."""
    if parsed_documents:
        return PdfOcrDecision(False, "parser_produced_chunks")

    ocr = settings.get("ocr", {})
    base_error = parse_error or "Il PDF non contiene testo indicizzabile"

    if not ocr.get("enabled"):
        return PdfOcrDecision(
            False,
            "ocr_disabled",
            f"{base_error} e OCR non e' configurato",
        )
    if not ocr.get("auto_on_empty_pdf", True):
        return PdfOcrDecision(
            False,
            "ocr_auto_disabled",
            f"{base_error} e OCR automatico e' disattivato",
        )

    readiness_error = _ocr_readiness_error(ocr)
    if readiness_error:
        return PdfOcrDecision(
            False,
            "ocr_not_ready",
            f"{base_error}. OCR non pronto: {readiness_error}",
        )

    return PdfOcrDecision(True, "parser_produced_no_chunks")


def _ocr_readiness_error(ocr: dict[str, Any]) -> str:
    if not ocr.get("provider"):
        return "provider mancante"
    if not ocr.get("base_url"):
        return "base URL mancante"
    if not ocr.get("default_model"):
        return "modello mancante"
    if bool(ocr.get("requires_api_key", True)) and not ocr_has_api_key(ocr):
        api_key_env = str(ocr.get("api_key_env") or "").strip()
        if api_key_env:
            return f"API key non configurata ({api_key_env})"
        return "API key non configurata"
    return ""
