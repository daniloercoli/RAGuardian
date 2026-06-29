from app.utils.ocr_policy import decide_pdf_ocr_for_ingestion


def _settings(**ocr_overrides):
    ocr = {
        "enabled": True,
        "auto_on_empty_pdf": True,
        "provider": "regolo",
        "base_url": "https://api.regolo.ai/v1",
        "requires_api_key": True,
        "api_key": "test-key",
        "api_key_env": "REGOLO_API_KEY",
        "default_model": "deepseek-ocr-2",
    }
    ocr.update(ocr_overrides)
    return {"ocr": ocr}


def test_pdf_ocr_policy_skips_when_parser_produced_chunks():
    decision = decide_pdf_ocr_for_ingestion(_settings(), parsed_documents=[object()])

    assert decision.should_run is False
    assert decision.reason == "parser_produced_chunks"


def test_pdf_ocr_policy_runs_when_parser_produced_no_chunks_and_ocr_is_ready():
    decision = decide_pdf_ocr_for_ingestion(_settings(), parsed_documents=[])

    assert decision.should_run is True
    assert decision.reason == "parser_produced_no_chunks"


def test_pdf_ocr_policy_skips_when_ocr_is_disabled():
    decision = decide_pdf_ocr_for_ingestion(_settings(enabled=False), parsed_documents=[])

    assert decision.should_run is False
    assert decision.reason == "ocr_disabled"


def test_pdf_ocr_policy_requires_api_key_when_provider_requires_it():
    decision = decide_pdf_ocr_for_ingestion(
        _settings(api_key="", api_key_env="MISSING_OCR_KEY"),
        parsed_documents=[],
    )

    assert decision.should_run is False
    assert decision.reason == "ocr_not_ready"
    assert "MISSING_OCR_KEY" in decision.error_message
