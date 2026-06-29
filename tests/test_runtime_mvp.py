import logging
import json
from pathlib import Path

from app.utils.logging_config import setup_logger


ROOT = Path(__file__).resolve().parents[1]


def test_file_logging_writes_to_configured_log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_FILE_LOG", "1")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FILE", "test-rag.log")

    logger = setup_logger("test.runtime.file_logging", level=logging.INFO)
    logger.info("hello from runtime test")
    for handler in logger.handlers:
        handler.flush()

    assert (tmp_path / "test-rag.log").exists()
    assert "hello from runtime test" in (tmp_path / "test-rag.log").read_text(encoding="utf-8")


def test_gunicorn_runtime_files_are_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    gunicorn_config = (ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")

    assert "gunicorn -c gunicorn.conf.py wsgi:application" in readme
    assert "LOG_DIR=app/logs" in env_example
    assert "GUNICORN_WORKERS=1" in env_example
    assert "GUNICORN_RUNTIME_LOG=gunicorn_runtime.log" in env_example
    assert "workers = _env_int(\"GUNICORN_WORKERS\", 1)" in gunicorn_config
    assert "gunicorn_access.log" in gunicorn_config
    assert "gunicorn_runtime.log" in gunicorn_config
    assert "LOG_TO_CONSOLE" in gunicorn_config


def test_wordpress_reference_plugin_is_present():
    plugin_root = ROOT / "integrations" / "wordpress" / "rag-client"
    plugin = plugin_root / "rag-client.php"
    includes = plugin_root / "includes"
    script = plugin_root / "assets" / "rag-client.js"
    styles = plugin_root / "assets" / "rag-client.css"
    wp_env = plugin_root / ".wp-env.json"
    package_json = plugin_root / "package.json"
    playwright_config = plugin_root / "playwright.config.mjs"
    fake_rag = plugin_root / "tests" / "support" / "fake-rag-server.mjs"
    e2e_spec = plugin_root / "tests" / "e2e" / "plugin.spec.mjs"
    wxr_fixture = plugin_root / "tests" / "fixtures" / "wp-export-public-posts.xml"
    php_sources = [plugin, *sorted(includes.glob("*.php"))]
    plugin_text = plugin.read_text(encoding="utf-8")
    php_text = "\n".join(path.read_text(encoding="utf-8") for path in php_sources)
    script_text = script.read_text(encoding="utf-8")
    styles_text = styles.read_text(encoding="utf-8")
    package = json.loads(package_json.read_text(encoding="utf-8"))

    assert plugin.exists()
    assert (includes / "autoload.php").exists()
    assert (includes / "class-ec-rag-api-client.php").exists()
    assert (includes / "class-ec-rag-ingestion.php").exists()
    assert (includes / "class-ec-rag-widget.php").exists()
    assert wp_env.exists()
    assert package_json.exists()
    assert playwright_config.exists()
    assert fake_rag.exists()
    assert e2e_spec.exists()
    assert wxr_fixture.exists()
    assert "EC_Rag_Options::register()" in plugin_text
    assert "EC_Rag_Widget::register()" in plugin_text
    assert "EC_Rag_Ingestion::register()" in plugin_text
    assert "add_shortcode('rag_chat'" in php_text
    assert "add_action('wp_footer'" in php_text
    assert "add_action('wp_enqueue_scripts'" in php_text
    assert "enable_global_widget" in php_text
    assert "allow_guest_chat" in php_text
    assert "ingest_public_posts" in php_text
    assert "ec_rag_wxr_import" in php_text
    assert "XMLReader" in php_text
    assert "(string) $wp->post_type !== 'post'" in php_text
    assert "(string) $wp->status !== 'publish'" in php_text
    assert "post_password" in php_text
    assert "transition_post_status" in php_text
    assert "save_post_post" in php_text
    assert "before_delete_post" in php_text
    assert "/api/v1/files?async=true" in php_text
    assert "/api/v1/audio?async=true" in php_text
    assert "$wpdb" not in php_text
    assert "get_the_author" not in php_text
    assert "get_userdata" not in php_text
    assert "custom_css" in php_text
    assert "client_context" in php_text
    assert "data-ec-rag-chat" in php_text
    assert "X-API-Key" in php_text
    assert "request_with_retry" in php_text
    assert "wp_add_inline_script" in php_text
    assert "api_key" not in script_text.lower()
    assert "downloadTranscript" in script_text
    assert "ec_rag_audio_upload" in script_text
    assert "ec-rag-launcher" in php_text
    assert ".ec-rag-chat--floating" in styles_text
    assert ".ec-rag-download" in styles_text
    assert '"plugins"' in wp_env.read_text(encoding="utf-8")
    assert "@wordpress/env" in package["devDependencies"]
    assert "@playwright/test" in package["devDependencies"]
    assert "build:zip" in package["scripts"]
    assert "test:php" in package["scripts"]
    assert "fake-rag-server.mjs" in playwright_config.read_text(encoding="utf-8")
    assert "/api/v1/files" in fake_rag.read_text(encoding="utf-8")
