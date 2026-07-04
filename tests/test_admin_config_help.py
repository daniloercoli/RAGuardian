from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_rag_parameter_help_popovers_are_wired():
    template = (ROOT / "app/templates/admin_config.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/admin_config.js").read_text(encoding="utf-8")

    assert "admin_config.js" in template
    assert "Service Status" in template
    assert "setup-check-grid" in template
    assert "Knowledge base" in template
    assert template.count("class=\"help-trigger\"") >= 10
    assert "data-help-title=\"Chunk Size\"" in template
    assert "data-help-title=\"Temperature\"" in template
    assert "data-help-title=\"Default Provider\"" in template
    assert "id=\"embeddingModel\"" in template
    assert "Voice &amp; Audio" in template
    assert "admin_api_keys" in template
    assert "Application API Keys" not in template
    assert "api_key_scopes" not in template
    assert "Provider and model policy is global" in template
    assert "id=\"rerankerDiversityMode\"" in template
    assert "None uses Chroma's top candidates as-is." in template
    assert "Source Diversity prevents one document from filling the reranker pool." in template
    assert "MMR Diversity balances Chroma relevance with semantic variety before reranking." in template
    assert "data-mmr-setting" in template
    assert "help-popover" in script
    assert "syncDiversityControls" in script
    assert "setting.hidden = !showMmrSettings" in script
    assert "event.clientX" in script
    assert "Escape" in script
