import json
import mimetypes
import os
import re
import threading
import time
import uuid
from copy import deepcopy

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from flask import (
    Flask,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

from config import Config
from utils.auth import (
    current_user,
    require_admin,
    require_admin_or_api_scope,
    require_admin_or_upload_api_key,
    require_api_scope,
    require_login,
)
from utils.file_index import FileIndex
from utils.http_security import (
    RequestTimeoutExceeded,
    apply_cors_headers as _apply_cors_headers,
    cors_origin_for_request as _cors_origin_for_request,
    csrf_token as _csrf_token,
    ensure_request_not_timed_out as _ensure_request_not_timed_out,
    env_bool as _env_bool,
    env_csv as _env_csv,
    env_float as _env_float,
    request_timeout_seconds as _request_timeout_seconds,
    security_hardening_required as _security_hardening_required,
    validate_csrf_request as _validate_csrf_request,
    validate_security_config as _validate_security_config,
)
from utils.logging_config import APP_LOGGER as log, configure_external_loggers
from utils.model_defaults import (
    ModelConfigurationError,
    default_provider_config_path,
    get_model_configuration_error,
    load_builtin_embedding_providers,
    load_builtin_ocr_providers,
    load_builtin_reranker_providers,
    load_builtin_voice_providers,
)
from utils.providers.provider_factory import ProviderFactory
from utils.providers.registry import ProviderRegistry
from utils.pagination import paginate_items as _paginate_items
from utils.rate_limiter import RateLimiter
from utils.settings_store import (
    SettingsStore,
    normalize_custom_provider,
    normalize_embedding_provider,
    normalize_ocr_provider,
    normalize_ocr_settings,
    normalize_reranker_provider,
    normalize_voice_provider,
    normalize_voice_settings,
)
from utils.state_backend import (
    configured_queue_backend,
    redis_connection,
    runtime_state_status,
)
from utils.job_store import get_job_store, queue_name
from utils.workspace import workspace_from_request
from utils.validators import (
    ValidationError,
    validate_boolean,
    validate_conversation_id,
    validate_file,
    validate_float,
    validate_integer,
    validate_query,
    validate_string,
)


_OCR_INDEX_PROFILE_KEYS = {
    "ocr_enabled",
    "ocr_auto_on_empty_pdf",
    "ocr_provider",
    "ocr_model",
    "ocr_mode",
    "ocr_output_format",
}
_WORKSPACE_ADMIN_SETTING_KEYS = {
    "rag",
    "custom_providers",
    "embedding_providers",
    "reranker_providers",
    "voice_providers",
    "ocr_providers",
    "voice",
    "ocr",
}
DOCUMENT_UPLOAD_EXTENSIONS = {"pdf", "txt", "md", "csv"}
AUDIO_UPLOAD_EXTENSIONS = {"mp3", "wav", "m4a", "webm", "ogg", "flac"}
CHAT_DATA_UPLOAD_EXTENSIONS = {"csv", "xlsx", "xls", "json", "parquet", "tsv", "zip"}
CHAT_DISPLAY_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf", "txt", "md"}
CHAT_UPLOAD_EXTENSIONS = CHAT_DATA_UPLOAD_EXTENSIONS | CHAT_DISPLAY_UPLOAD_EXTENSIONS
CHAT_FILE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
CODE_IMAGE_PATTERN = re.compile(r"^[0-9a-f]{12}_[A-Za-z0-9._-]+\.png$")


def create_app(test_config: dict | None = None) -> Flask:
    load_dotenv()
    _setup_ssl()
    configure_external_loggers()

    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["UPLOAD_FOLDER"] = Config.paths.upload_folder
    app.config["SETTINGS_FILE"] = Config.paths.settings_file
    app.config["FILE_INDEX"] = Config.paths.file_index
    app.config["USERS_FILE"] = os.getenv("RAG_USERS_FILE", "app/data/users.json")
    app.config["PROMPTS_DIR"] = os.getenv("RAG_PROMPTS_DIR", "app/data")
    app.config["SECRETS_FILE"] = os.getenv("RAG_SECRETS_FILE", "app/data/secrets.json")
    app.config["API_KEY_USAGE_FILE"] = os.getenv("RAG_API_KEY_USAGE_FILE", "app/data/api_keys_usage.json")
    app.config["WORKSPACE_DATA_DIR"] = os.getenv("RAG_WORKSPACE_DATA_DIR", "app/data/workspaces")
    app.config["WORKSPACE_UPLOAD_DIR"] = os.getenv("RAG_WORKSPACE_UPLOAD_DIR", "app/uploads/workspaces")
    app.config["SECRET_KEY"] = Config.api_keys.flask_secret_key or os.getenv("FLASK_SECRET_KEY") or "dev-secret"
    app.config["RAG_SECRET_KEY"] = os.getenv("RAG_SECRET_KEY") or app.config["SECRET_KEY"]
    app.config["MAX_UPLOAD_SIZE_MB"] = Config.paths.max_upload_size_mb
    app.config["MAX_AUDIO_UPLOAD_SIZE_MB"] = int(os.getenv("MAX_AUDIO_UPLOAD_SIZE_MB", "50"))
    app.config["RATE_LIMIT_REQUESTS"] = int(os.getenv("RATE_LIMIT_REQUESTS", "10"))
    app.config["RATE_LIMIT_WINDOW"] = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
    app.config["VECTOR_STORE_BACKEND"] = Config.vector_store.backend
    app.config["CORS_ALLOWED_ORIGINS"] = _env_csv("RAG_CORS_ALLOWED_ORIGINS")
    app.config["CORS_ALLOWED_METHODS"] = _env_csv(
        "RAG_CORS_ALLOWED_METHODS",
        default="GET,POST,DELETE,OPTIONS",
    )
    app.config["CORS_ALLOWED_HEADERS"] = _env_csv(
        "RAG_CORS_ALLOWED_HEADERS",
        default="Content-Type,X-API-Key,X-Request-ID",
    )
    app.config["CORS_ALLOW_CREDENTIALS"] = _env_bool("RAG_CORS_ALLOW_CREDENTIALS", False)
    app.config["CORS_MAX_AGE"] = int(os.getenv("RAG_CORS_MAX_AGE", "600"))
    app.config["REQUEST_TIMEOUT_SECONDS"] = _env_float("RAG_REQUEST_TIMEOUT_SECONDS", 0.0)
    app.config["CSRF_ENABLED"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _security_hardening_required()

    if test_config:
        app.config.update(test_config)
        if app.testing and "CSRF_ENABLED" not in test_config:
            app.config["CSRF_ENABLED"] = False
        if app.testing and "RAG_SECRET_KEY" not in test_config:
            app.config["RAG_SECRET_KEY"] = app.config["SECRET_KEY"]

    _validate_security_config(app)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["WORKSPACE_DATA_DIR"], exist_ok=True)
    os.makedirs(app.config["WORKSPACE_UPLOAD_DIR"], exist_ok=True)
    from utils.user_store import UserStore

    migrated_api_keys = UserStore(app.config["USERS_FILE"]).migrate_legacy_api_keys()
    if migrated_api_keys:
        log.info("Migrated %d legacy API key(s) to hashed storage", migrated_api_keys)
    SettingsStore(app.config["SETTINGS_FILE"]).load()
    model_config_error = get_model_configuration_error()
    if model_config_error:
        log.error(model_config_error)

    rate_limiter = RateLimiter(
        max_requests=app.config["RATE_LIMIT_REQUESTS"],
        window_seconds=app.config["RATE_LIMIT_WINDOW"],
    )

    register_routes(app, rate_limiter)

    @app.context_processor
    def _inject_current_user():
        return {"current_user": current_user(), "csrf_token": _csrf_token()}

    @app.errorhandler(RequestTimeoutExceeded)
    def _request_timeout_error(_error):
        return jsonify(error="Richiesta scaduta", status="timeout"), 503

    # ── Start backup scheduler (background thread) ──
    from utils.vector_store.backup_manager import start_scheduler
    start_scheduler()

    # --- Request metrics + API key usage middleware ---
    @app.before_request
    def _before_request_metrics():
        request._rag_metrics_start = time.time()
        request._rag_monotonic_start = time.monotonic()
        request._rag_request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        timeout_seconds = _request_timeout_seconds(app)
        request._rag_deadline = (
            request._rag_monotonic_start + timeout_seconds
            if timeout_seconds > 0
            else None
        )
        if request.method == "OPTIONS" and _cors_origin_for_request(app):
            return Response(status=204)
        csrf_error = _validate_csrf_request(app)
        if csrf_error is not None:
            return csrf_error
        # Track API key info for usage logging
        api_key_header = request.headers.get("X-API-Key")
        if api_key_header:
            from utils.auth import find_api_key
            request._api_key_info = find_api_key(api_key_header)

    @app.after_request
    def _after_request_metrics(response):
        start = getattr(request, "_rag_metrics_start", time.time())
        duration = time.time() - start

        endpoint = request.url_rule.rule if request.url_rule else (request.path or "<unknown>")

        method = request.method
        status_code = response.status_code

        from utils.metrics import get_metrics
        get_metrics().observe_request(
            duration=duration,
            method=method,
            endpoint=endpoint,
            status_code=status_code,
        )

        # Log per-user API key usage
        api_key_info = getattr(request, "_api_key_info", None)
        if api_key_info is not None:
            user_id = api_key_info.get("user_id", "")
            key_name = api_key_info.get("_user_key_name") or api_key_info.get("name", "")
            try:
                from utils.user_store import UserStore
                store = UserStore(app.config.get("USERS_FILE"))
                store.update_api_key_usage(
                    user_id,
                    key_name,
                    extra={"endpoint": endpoint, "status_code": status_code},
                )
            except Exception:
                pass
            try:
                from utils.api_key_logger import ApiKeyLogger
                scopes = [str(scope) for scope in (api_key_info.get("scopes") or [])]
                ip_address = request.headers.get(
                    "X-Forwarded-For",
                    request.remote_addr or "",
                ).split(",")[0].strip()
                ApiKeyLogger(app.config.get("API_KEY_USAGE_FILE")).log(
                    user_id=user_id,
                    key_name=key_name,
                    endpoint=endpoint,
                    method=method,
                    status_code=status_code,
                    scopes_used=scopes,
                    duration_ms=int(duration * 1000),
                    request_id=getattr(request, "_rag_request_id", ""),
                    ip_address=ip_address,
                    workspace_id=api_key_info.get("workspace_id", ""),
                    api_key_id=api_key_info.get("api_key_id") or key_name,
                )
            except Exception:
                pass

        response.headers["X-Request-ID"] = getattr(request, "_rag_request_id", "")
        response.headers["X-Request-Duration-Ms"] = str(int(duration * 1000))
        timeout_seconds = _request_timeout_seconds(app)
        if timeout_seconds > 0 and duration > timeout_seconds:
            log.warning(
                "Request exceeded timeout budget: %s %s took %.3fs (budget %.3fs)",
                method,
                endpoint,
                duration,
                timeout_seconds,
            )
        _apply_cors_headers(app, response)
        return response

    return app


def register_routes(app: Flask, rate_limiter: RateLimiter) -> None:
    from routes.admin_accounts import register_admin_account_routes
    from routes.auth import register_auth_routes
    from routes.backups import register_backup_routes
    from routes.prompts import register_prompt_routes

    register_admin_account_routes(app)
    register_auth_routes(app)
    register_backup_routes(app)
    register_prompt_routes(app)

    @app.route("/")
    @require_login
    def index():
        model_config_error = get_model_configuration_error()
        if model_config_error:
            return render_template(
                "configuration_error.html",
                error=model_config_error,
                provider_config_path=str(default_provider_config_path()),
            ), 500
        return render_template("index.html")

    @app.route("/models", methods=["GET"])
    def models():
        try:
            return jsonify(_models_response_payload())
        except ModelConfigurationError as e:
            return jsonify(_model_configuration_error_response(str(e))), 500

    @app.route("/health", methods=["GET"])
    def health():
        detailed = _health_status(app, deep=False)
        return jsonify(
            {
                key: detailed.get(key)
                for key in (
                    "status",
                    "system_ready",
                    "model_configuration_ready",
                    "database_ready",
                    "redis_ready",
                    "queue_ready",
                    "uptime_seconds",
                )
            }
        )

    @app.route("/metrics", methods=["GET"])
    def metrics_text():
        """Expose Prometheus metrics in text exposition format."""
        if os.getenv("METRICS_ENABLED", "1").lower() not in {"1", "true", "yes", "on"}:
            return jsonify(error="Metrics endpoint disabled", status="disabled"), 404

        from utils.metrics import get_metrics

        metrics = get_metrics()
        metrics.refresh_memory()
        return metrics.generate_prometheus_text(), 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/cache/stats", methods=["GET"])
    def cache_stats():
        from utils.rag_engine import get_cache_stats

        return jsonify(get_cache_stats())

    @app.route("/cache/clear", methods=["POST"])
    @require_admin
    def cache_clear():
        from utils.rag_engine import clear_cache

        clear_cache()
        return jsonify(message="Cache cleared")

    @app.route("/conversation/<conversation_id>", methods=["DELETE"])
    @require_login
    def conversation_clear(conversation_id):
        conversation_id = validate_conversation_id(conversation_id, required=True)
        from utils.conversation_memory import get_conversation_store

        cleared = get_conversation_store().clear(_scoped_conversation_id(conversation_id))
        return jsonify(conversation_id=conversation_id, cleared=cleared)

    @app.route("/transcribe", methods=["POST"])
    @require_login
    def transcribe():
        limited = _rate_limit_or_response(rate_limiter)
        if limited:
            return limited

        try:
            result = _transcribe_audio(app)
            return jsonify(result)
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error(f"Errore trascrizione audio: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/ocr", methods=["POST"])
    @require_login
    def ocr_extract():
        limited = _rate_limit_or_response(rate_limiter)
        if limited:
            return limited

        try:
            return jsonify(_ocr_extract_upload(app, persist=False))
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error(f"Errore OCR: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/ask", methods=["POST"])
    @require_login
    def ask():
        limited = _rate_limit_or_response(rate_limiter)
        if limited:
            return limited

        try:
            payload = _parse_query_payload(require_json=True)
            # Code interpreter mode
            if payload.get("use_code_interpreter") and payload.get("attached_files"):
                if payload["stream"] and payload["stream_format"] == "ndjson":
                    return Response(
                        run_code_interpreter_query_events(payload),
                        mimetype="application/x-ndjson",
                    )
                return jsonify(run_code_interpreter_query(payload))
            if payload["stream"]:
                if payload["stream_format"] == "ndjson":
                    return Response(
                        run_rag_query_events(payload),
                        mimetype="application/x-ndjson",
                    )
                return Response(
                    run_rag_query(payload, stream=True),
                    mimetype="text/plain",
                )

            result = run_rag_query(payload, stream=False)
            return jsonify(result)
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except RequestTimeoutExceeded:
            raise
        except Exception as e:
            log.error(f"Errore ask: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/upload", methods=["POST"])
    @require_admin_or_upload_api_key
    def upload_legacy():
        return _upload_json_response(app)

    @app.route("/admin/config", methods=["GET", "POST"])
    @require_admin
    def admin_config():
        store = SettingsStore(app.config["SETTINGS_FILE"])
        if request.method == "POST":
            try:
                _handle_config_post(store)
                _sync_admin_settings_to_workspaces(app, store.load())
                ProviderFactory.reset_cache()
                from utils.providers.embedding_factory import EmbeddingFactory

                EmbeddingFactory.reset_cache()
                flash("Configurazione salvata", "success")
            except ValidationError as e:
                flash(e.message, "error")
            except Exception as e:
                log.error(f"Errore salvataggio configurazione: {e}")
                flash(str(e), "error")
            return redirect(url_for("admin_config"))

        raw_settings = store.load()
        settings = store.public_view()
        model_config_error = None
        try:
            models = _model_payload()
        except ModelConfigurationError as e:
            models = []
            model_config_error = str(e)
        setup_status = {
            "users_configured": bool(current_user() or _has_users(app)),
            "api_key_configured": bool(
                os.getenv("RAG_API_KEY")
                or _any_user_api_keys(app)
            ),
        }
        return render_template(
            "admin_config.html",
            settings=settings,
            models=models,
            llm_providers=_llm_provider_payload(raw_settings),
            embedding_providers=_embedding_provider_payload(settings),
            reranker_providers=_reranker_provider_payload(settings),
            voice_providers=_voice_provider_payload(raw_settings),
            ocr_providers=_ocr_provider_payload(raw_settings),
            model_config_error=model_config_error,
            provider_config_path=str(default_provider_config_path()),
            health=_health_status(app, deep=False),
            index_status=_index_rebuild_status(app),
            setup_status=setup_status,
        )

    @app.route("/admin/files", methods=["GET", "POST"])
    @require_login
    def admin_files():
        config = _workspace_config(app)
        if request.method == "POST":
            try:
                uploaded = request.files.get("file")
                extension = ""
                if uploaded and uploaded.filename and "." in uploaded.filename:
                    extension = uploaded.filename.rsplit(".", 1)[1].lower()
                if extension in AUDIO_UPLOAD_EXTENSIONS:
                    result = _process_audio_upload(app, config=config)
                else:
                    result = _process_upload(app, config=config)
                flash(result["message"], "success")
            except ValidationError as e:
                flash(e.message, "error")
            except Exception as e:
                log.error(f"Errore upload admin: {e}")
                flash(str(e), "error")
            return redirect(url_for("admin_files"))

        file_index = FileIndex(config["FILE_INDEX"])
        files = file_index.list()
        files_page, files_pagination = _paginate_items(
            files,
            page=request.args.get("page", "1"),
            per_page=request.args.get("per_page", "25"),
            allowed_per_page=(10, 25, 50, 100),
            default_per_page=25,
        )
        return render_template(
            "admin_files.html",
            files=files_page,
            files_pagination=files_pagination,
            health=_health_status(app, deep=False, config=config),
            index_status=_index_rebuild_status(app, config=config),
        )

    @app.route("/admin/data-sources", methods=["GET", "POST"])
    @require_login
    def admin_data_sources():
        from utils.data_ingestion.registry import available_plugins
        from utils.data_ingestion.service import data_source_summaries

        config = _workspace_config(app)
        store = SettingsStore(config["SETTINGS_FILE"])
        if request.method == "POST":
            try:
                source = _data_source_from_form(request.form)
                settings = store.load()
                existing = {
                    item.get("id"): item
                    for item in settings.get("data_sources", [])
                    if item.get("id")
                }
                previous = existing.get(source["id"], {})
                source = {
                    **source,
                    "cursor": previous.get("cursor", {}),
                    "last_sync": previous.get("last_sync", ""),
                    "last_sync_status": previous.get("last_sync_status", ""),
                    "next_sync_at": previous.get("next_sync_at", ""),
                    "last_error": previous.get("last_error", ""),
                }
                existing[source["id"]] = source
                store.save({**settings, "data_sources": list(existing.values())})
                flash("Data source salvata", "success")
            except ValidationError as e:
                flash(e.message, "error")
            except Exception as e:
                log.error("Errore salvataggio data source: %s", e)
                flash(str(e), "error")
            return redirect(url_for("admin_data_sources"))

        settings = store.public_view()
        return render_template(
            "admin_data_sources.html",
            data_sources=data_source_summaries(settings, config["FILE_INDEX"]),
            plugins=available_plugins(),
            health=_health_status(app, deep=False, config=config),
        )

    @app.route("/admin/data-sources/<data_source_id>/sync", methods=["POST"])
    @require_login
    def admin_sync_data_source(data_source_id):
        try:
            payload, status_code = _start_data_source_sync_job(app, data_source_id)
            return jsonify(payload), status_code
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error("Errore avvio sync data source: %s", e)
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/admin/data-sources/<data_source_id>/toggle", methods=["POST"])
    @require_login
    def admin_toggle_data_source(data_source_id):
        try:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValidationError("Body JSON non valido", "enabled")
            enabled = validate_boolean(payload.get("enabled"), "enabled")
            config = _workspace_config(app)
            from utils.settings_store import SettingsStore
            from utils.data_ingestion.service import toggle_data_source_enabled
            toggle_data_source_enabled(SettingsStore(config["SETTINGS_FILE"]), data_source_id, enabled)
            return jsonify(status="ok")
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error("Errore toggle data source: %s", e)
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/admin/data-sources/jobs/<job_id>", methods=["GET"])
    @require_login
    def admin_data_source_sync_status(job_id):
        job = _get_job(job_id)
        config = _workspace_config(app)
        if job and job.get("workspace_id") != config["WORKSPACE_ID"]:
            job = None
        if not job:
            return jsonify(error="Job non trovato", status="not_found"), 404
        return jsonify(job)

    @app.route("/admin/files/delete", methods=["POST"])
    @require_login
    def admin_delete_file():
        try:
            result = _delete_indexed_file(app, request.form.get("filename"), config=_workspace_config(app))
            flash(result["message"], "success")
        except ValidationError as e:
            flash(e.message, "error")
        except Exception as e:
            log.error(f"Errore eliminazione file admin: {e}")
            flash(str(e), "error")
        return redirect(url_for("admin_files"))

    @app.route("/admin/files/rebuild", methods=["POST"])
    @require_login
    def admin_rebuild_index():
        try:
            payload, status_code = _start_rebuild_index_job(app, config=_workspace_config(app))
            return jsonify(payload), status_code
        except Exception as e:
            log.error(f"Errore avvio ricostruzione indice: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/admin/files/rebuild/<job_id>", methods=["GET"])
    @require_login
    def admin_rebuild_index_status(job_id):
        job = _get_rebuild_job(job_id)
        config = _workspace_config(app)
        if job and job.get("workspace_id") != config["WORKSPACE_ID"]:
            job = None
        if not job:
            return jsonify(error="Job non trovato", status="not_found"), 404
        return jsonify(job)

    @app.route("/admin/files/download/<path:filename>", methods=["GET"])
    @require_login
    def admin_download_file(filename):
        try:
            return _download_indexed_file(app, filename, config=_workspace_config(app))
        except ValidationError as e:
            flash(e.message, "error")
            return redirect(url_for("admin_files"))
        except Exception as e:
            log.error(f"Errore download file: {e}")
            flash(str(e), "error")
            return redirect(url_for("admin_files"))

    # ---------------------------------------------------------------
    # Code Interpreter Routes
    # ---------------------------------------------------------------

    @app.route("/upload-to-chat", methods=["POST"])
    @require_login
    def upload_to_chat():
        """Upload file for code interpreter (no RAG indexing)."""
        try:
            if not _code_interpreter_enabled():
                return jsonify(error="Code interpreter disabilitato", status="disabled"), 403
            file = request.files.get("file")
            if not file:
                return jsonify(error="Nessun file"), 400
            from utils.validators import validate_file
            config = _workspace_config(app)
            _cleanup_code_interpreter_files(config)
            max_mb = int(os.getenv("CODE_INTERPRETER_MAX_FILE_MB", "50"))
            validated = validate_file(
                file=file, field_name="file",
                allowed_extensions=sorted(CHAT_UPLOAD_EXTENSIONS),
                max_size_mb=max_mb,
            )
            filename = secure_filename(validated.filename)
            if not filename:
                raise ValidationError("Nome file non valido", "file")
            extension = filename.rsplit(".", 1)[1].lower()
            if extension not in CHAT_UPLOAD_EXTENSIONS:
                return jsonify(error="Formato file non supportato"), 400
            file_id = uuid.uuid4().hex
            scratch = _chat_upload_dir(config)
            os.makedirs(scratch, exist_ok=True)
            file_path = os.path.join(scratch, f"{file_id}_{filename}")
            validated.save(file_path)
            return jsonify({
                "filename": filename,
                "id": file_id,
                "file_id": file_id,
                "size": os.path.getsize(file_path),
                "type": validated.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
            })
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error(f"Errore upload-to-chat: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/code_pics/<path:filename>")
    @require_login
    def serve_code_pic(filename):
        """Serve generated code result images."""
        config = _workspace_config(app)
        if not CODE_IMAGE_PATTERN.match(filename):
            return jsonify(error="Image not found"), 404
        pic_path = _safe_join(_chat_pics_dir(config), filename)
        if not os.path.exists(pic_path):
            return jsonify(error="Image not found"), 404
        from flask import send_file as _send_file
        return _send_file(pic_path)

    @app.route("/api/v1/health", methods=["GET"])
    @require_api_scope("query")
    def api_health():
        return jsonify(_health_status(app))

    @app.route("/api/v1/models", methods=["GET"])
    @require_api_scope("query")
    def api_models():
        try:
            return jsonify(_models_response_payload())
        except ModelConfigurationError as e:
            return jsonify(_model_configuration_error_response(str(e))), 500

    @app.route("/api/v1/query", methods=["POST"])
    @require_api_scope("query")
    def api_query():
        limited = _rate_limit_or_response(rate_limiter)
        if limited:
            return limited

        try:
            payload = _parse_query_payload(require_json=True)
            if payload["stream"]:
                if payload["stream_format"] == "ndjson":
                    return Response(
                        run_rag_query_events(payload, public=True),
                        mimetype="application/x-ndjson",
                    )
                return Response(
                    run_rag_query(payload, stream=True, public=True),
                    mimetype="text/plain",
                )
            return jsonify(run_rag_query(payload, stream=False, public=True))
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except RequestTimeoutExceeded:
            raise
        except Exception as e:
            log.error(f"Errore api query: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/api/v1/files", methods=["POST"])
    @require_admin_or_api_scope("ingest")
    def api_files():
        return _upload_json_response(app)

    @app.route("/api/v1/audio", methods=["POST"])
    @require_admin_or_api_scope("ingest")
    def api_audio():
        return _audio_json_response(app)

    @app.route("/api/v1/jobs/<job_id>", methods=["GET"])
    @require_admin_or_api_scope("ingest")
    def api_job_status(job_id):
        job = _get_job(job_id)
        if job and getattr(request, "api_key", None) and job.get("workspace_id") != request.api_key.get("workspace_id"):
            job = None
        if not job:
            return jsonify(error="Job non trovato", status="not_found"), 404
        return jsonify(job)

    @app.route("/api/v1/ocr", methods=["POST"])
    @require_api_scope("query")
    def api_ocr():
        try:
            return jsonify(_ocr_extract_upload(app, persist=False, config=_workspace_config(app)))
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error(f"Errore OCR API: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/api/v1/tts", methods=["POST"])
    @require_api_scope("speech")
    def api_tts():
        try:
            return _tts_response(app)
        except ValidationError as e:
            return jsonify(e.to_dict()), 400
        except Exception as e:
            log.error(f"Errore TTS API: {e}")
            return jsonify(error=str(e), status="server_error"), 500

    @app.route("/api/v1/conversations/<conversation_id>", methods=["DELETE"])
    @require_api_scope("query")
    def api_conversation_clear(conversation_id):
        conversation_id = validate_conversation_id(conversation_id, required=True)
        from utils.conversation_memory import get_conversation_store

        cleared = get_conversation_store().clear(_scoped_conversation_id(conversation_id))
        return jsonify(conversation_id=conversation_id, cleared=cleared)

    @app.route("/api/v1/files/<path:filename>", methods=["DELETE"])
    @require_admin_or_api_scope("ingest")
    def api_delete_file(filename):
        try:
            return jsonify(_delete_indexed_file(app, filename, config=_workspace_config(app)))
        except ValidationError as e:
            status_code = 404 if e.code == "not_found" else 400
            return jsonify(e.to_dict()), status_code
        except Exception as e:
            log.error(f"Errore eliminazione file API: {e}")
            return jsonify(error=str(e), status="server_error"), 500


def run_rag_query(payload: dict, stream: bool = False, public: bool = False):
    from utils.rag_engine import query_rag
    from utils.metrics import get_metrics

    metrics = get_metrics()
    metrics.begin_query()
    start = time.time()
    status = "success"

    try:
        _ensure_request_not_timed_out()
        config = _workspace_config(current_app)
        raw_conversation_id = payload.get("conversation_id")
        conversation_id = _scoped_conversation_id(raw_conversation_id)
        custom_system = _resolve_system_prompt(payload.get("system_prompt_id"))
        extra_context_docs = _temporary_attachment_context_docs(payload, config)
        _ensure_request_not_timed_out()
        result = query_rag(
            payload["query"],
            model=payload.get("model"),
            provider=payload.get("provider"),
            stream=stream,
            temperature=payload.get("temperature"),
            k=payload.get("k"),
            settings_path=config["SETTINGS_FILE"],
            file_index_path=config["FILE_INDEX"],
            collection_name=config["CHROMA_COLLECTION"],
            conversation_id=conversation_id,
            client_context=payload.get("client_context"),
            response_language=payload.get("response_language"),
            public=public,
            custom_system_prompt=custom_system,
            extra_context_docs=extra_context_docs,
        )
        _ensure_request_not_timed_out()
        if isinstance(result, dict) and raw_conversation_id:
            result["conversation_id"] = raw_conversation_id
        return result
    except RequestTimeoutExceeded:
        status = "timeout"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        metrics.end_query()
        duration = time.time() - start
        metrics.observe_query(duration=duration, status=status)


def run_rag_query_events(payload: dict, public: bool = False):
    from utils.rag_engine import query_rag_stream_events
    from utils.metrics import get_metrics

    metrics = get_metrics()
    metrics.begin_query()
    start = time.time()
    status = "success"

    config = _workspace_config(current_app)
    _ensure_request_not_timed_out()
    raw_conversation_id = payload.get("conversation_id")
    conversation_id = _scoped_conversation_id(raw_conversation_id)
    custom_system = _resolve_system_prompt(payload.get("system_prompt_id"))
    extra_context_docs = _temporary_attachment_context_docs(payload, config)
    _ensure_request_not_timed_out()
    events = query_rag_stream_events(
        payload["query"],
        model=payload.get("model"),
        provider=payload.get("provider"),
        temperature=payload.get("temperature"),
        k=payload.get("k"),
        settings_path=config["SETTINGS_FILE"],
        file_index_path=config["FILE_INDEX"],
        collection_name=config["CHROMA_COLLECTION"],
        conversation_id=conversation_id,
        client_context=payload.get("client_context"),
        response_language=payload.get("response_language"),
        public=public,
        custom_system_prompt=custom_system,
        extra_context_docs=extra_context_docs,
    )

    def encode_events():
        nonlocal status
        try:
            for event in events:
                _ensure_request_not_timed_out()
                if raw_conversation_id and isinstance(event, dict) and event.get("conversation_id"):
                    event = {**event, "conversation_id": raw_conversation_id}
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except RequestTimeoutExceeded:
            status = "timeout"
            yield json.dumps(
                {"type": "error", "error": "Richiesta scaduta", "status": "timeout"},
                ensure_ascii=False,
            ) + "\n"
        except Exception:
            status = "error"
            raise
        finally:
            metrics.end_query()
            duration = time.time() - start
            metrics.observe_query(duration=duration, status=status)

    return encode_events()


def _temporary_attachment_context_docs(payload: dict, config: dict) -> list:
    """Build ephemeral RAG context for chat attachments when Python mode is off."""
    if payload.get("use_code_interpreter") or not payload.get("attached_files"):
        return []
    attachments = _resolve_chat_attachments(config, payload.get("attached_files") or [])
    if not attachments:
        return []
    from utils.temporary_attachment_rag import retrieve_attachment_context

    top_k = int(os.getenv("CHAT_ATTACHMENT_RAG_K", "4"))
    return retrieve_attachment_context(
        payload["query"],
        attachments,
        settings_path=config["SETTINGS_FILE"],
        top_k=top_k,
    )


def run_code_interpreter_query(payload: dict) -> dict:
    """Generate Python from query + RAG context, then execute it on attached files."""
    from utils.metrics import get_metrics

    metrics = get_metrics()
    metrics.begin_query()
    start = time.time()
    status = "success"
    try:
        _ensure_request_not_timed_out()
        prepared = _prepare_code_interpreter_run(payload)
        _ensure_request_not_timed_out()
        code = _generate_code_for_interpreter(prepared)
        _ensure_request_not_timed_out()
        result = _execute_interpreter_code(prepared, code)
        _ensure_request_not_timed_out()
        _append_code_interpreter_conversation_turn(payload, prepared, result)
        return _code_interpreter_response(prepared, code, result)
    except RequestTimeoutExceeded:
        status = "timeout"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        metrics.end_query()
        metrics.observe_query(duration=time.time() - start, status=status)


def run_code_interpreter_query_events(payload: dict):
    """NDJSON event stream for code interpreter mode."""
    from utils.metrics import get_metrics

    metrics = get_metrics()
    metrics.begin_query()
    start = time.time()
    status = "success"

    def encode(event: dict) -> str:
        return json.dumps(event, ensure_ascii=False) + "\n"

    try:
        _ensure_request_not_timed_out()
        prepared = _prepare_code_interpreter_run(payload)
        yield encode(_code_interpreter_meta_event(prepared))
        _ensure_request_not_timed_out()
        code = _generate_code_for_interpreter(prepared)
        yield encode({"type": "code", "code": code})
        _ensure_request_not_timed_out()
        result = _execute_interpreter_code(prepared, code)
        yield encode({"type": "execution", "result": result})
        _ensure_request_not_timed_out()
        _append_code_interpreter_conversation_turn(payload, prepared, result)
        yield encode({"type": "done", **_code_interpreter_response(prepared, code, result)})
    except RequestTimeoutExceeded:
        status = "timeout"
        yield encode({"type": "error", "error": "Richiesta scaduta", "status": "timeout"})
    except Exception as exc:
        status = "error"
        log.error("Errore code interpreter: %s", exc)
        yield encode({"type": "error", "error": str(exc), "status": "server_error"})
    finally:
        metrics.end_query()
        metrics.observe_query(duration=time.time() - start, status=status)


def _code_interpreter_enabled() -> bool:
    value = os.getenv("CODE_INTERPRETER_ENABLED", "1").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _chat_base_dir(config: dict) -> str:
    return os.path.join(config["UPLOAD_FOLDER"], "chat_files")


def _chat_upload_dir(config: dict) -> str:
    return os.path.join(_chat_base_dir(config), "uploads")


def _chat_pics_dir(config: dict) -> str:
    return os.path.join(_chat_base_dir(config), "pics")


def _chat_code_runs_dir(config: dict) -> str:
    return os.path.join(_chat_base_dir(config), "code_runs")


def _safe_join(root: str, *parts: str) -> str:
    root_abs = os.path.abspath(root)
    path = os.path.abspath(os.path.join(root_abs, *parts))
    if path != root_abs and not path.startswith(root_abs + os.sep):
        raise ValidationError("Path non valido", "path")
    return path


def _cleanup_code_interpreter_files(config: dict) -> None:
    try:
        ttl_hours = int(os.getenv("CODE_INTERPRETER_TTL_HOURS", "24"))
    except ValueError:
        ttl_hours = 24
    if ttl_hours <= 0:
        return
    cutoff = time.time() - (ttl_hours * 3600)
    for root in (_chat_upload_dir(config), _chat_pics_dir(config), _chat_code_runs_dir(config)):
        if not os.path.isdir(root):
            continue
        for current_root, dirs, files in os.walk(root, topdown=False):
            for filename in files:
                path = os.path.join(current_root, filename)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass
            for dirname in dirs:
                path = os.path.join(current_root, dirname)
                try:
                    os.rmdir(path)
                except OSError:
                    pass


def _resolve_chat_attachments(config: dict, attached_files: list[dict]) -> list[dict]:
    upload_dir = _chat_upload_dir(config)
    resolved = []
    used_runtime_names: set[str] = set()
    for index, item in enumerate(attached_files or []):
        file_id = str(item.get("id") or item.get("file_id") or "").strip().lower()
        if not CHAT_FILE_ID_PATTERN.match(file_id):
            raise ValidationError(f"attached_files[{index}].id non valido", "attached_files")
        if not os.path.isdir(upload_dir):
            raise ValidationError("File allegato non trovato", "attached_files", "not_found")
        prefix = f"{file_id}_"
        matches = sorted(
            name for name in os.listdir(upload_dir)
            if name.startswith(prefix) and os.path.isfile(_safe_join(upload_dir, name))
        )
        if not matches:
            raise ValidationError("File allegato non trovato", "attached_files", "not_found")

        stored_name = matches[0]
        original_name = stored_name[len(prefix):]
        safe_name = secure_filename(original_name)
        if not safe_name:
            raise ValidationError("Nome file allegato non valido", "attached_files")
        runtime_name = safe_name
        if runtime_name in used_runtime_names:
            runtime_name = f"{file_id[:8]}_{safe_name}"
        used_runtime_names.add(runtime_name)
        host_path = _safe_join(upload_dir, stored_name)
        extension = safe_name.rsplit(".", 1)[1].lower() if "." in safe_name else ""
        if extension not in CHAT_UPLOAD_EXTENSIONS:
            raise ValidationError("Formato file allegato non supportato", "attached_files")
        resolved.append(
            {
                "id": file_id,
                "name": safe_name,
                "type": mimetypes.guess_type(safe_name)[0] or item.get("type") or "application/octet-stream",
                "path": host_path,
                "runtime_name": runtime_name,
                "container_path": f"/data/{runtime_name}",
                "preview": _chat_file_preview(host_path, extension),
                "size": os.path.getsize(host_path),
            }
        )
    return resolved


def _chat_file_preview(path: str, extension: str, max_chars: int = 4000) -> str:
    if extension not in {"csv", "tsv", "json", "txt", "md"}:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars).strip()
    except OSError:
        return ""


def _prepare_code_interpreter_run(payload: dict) -> dict:
    if not _code_interpreter_enabled():
        raise ValidationError("Code interpreter disabilitato", "use_code_interpreter", "disabled")

    config = _workspace_config(current_app)
    _cleanup_code_interpreter_files(config)
    attached_files = _resolve_chat_attachments(config, payload.get("attached_files") or [])
    if not attached_files:
        raise ValidationError("Allega almeno un file dati", "attached_files")

    from utils.prompt_templates import build_code_system_prompt
    from utils.providers.provider_factory import ProviderFactory
    from utils.rag_engine import (
        _client_context_block,
        _serialize_context,
        _serialize_sources,
        prepare_rag_context,
    )
    from utils.settings_store import SettingsStore

    raw_conversation_id = payload.get("conversation_id")
    conversation_id = _scoped_conversation_id(raw_conversation_id)
    custom_system = _resolve_system_prompt(payload.get("system_prompt_id"))
    rag_error = ""
    try:
        rag_payload = prepare_rag_context(
            payload["query"],
            model=payload.get("model"),
            provider=payload.get("provider"),
            temperature=payload.get("temperature"),
            k=payload.get("k"),
            settings_path=config["SETTINGS_FILE"],
            collection_name=config["CHROMA_COLLECTION"],
            conversation_id=conversation_id,
            response_language=payload.get("response_language"),
            use_cache=False,
        )
    except Exception as exc:
        log.warning("RAG context unavailable for code interpreter: %s", exc)
        rag_error = str(exc)
        settings = SettingsStore(config["SETTINGS_FILE"]).load()
        provider_id, selected_model, provider_config = ProviderFactory.resolve(
            model=payload.get("model"),
            provider=payload.get("provider"),
            settings=settings,
        )
        rag_payload = {
            "settings": settings,
            "provider": provider_id,
            "model": selected_model,
            "provider_config": provider_config,
            "temperature": payload.get("temperature")
            if payload.get("temperature") is not None
            else settings["rag"]["temperature"],
            "k": payload.get("k") or settings["rag"]["query_k"],
            "response_language": payload.get("response_language") or "auto",
            "conversation_context": "",
            "context_docs": [],
        }

    context_docs = rag_payload["context_docs"]
    system_prompt = build_code_system_prompt(
        user_query=payload["query"],
        data_files=attached_files,
        rag_context=_rag_context_for_code_prompt(context_docs, rag_error=rag_error),
        conversation_context=str(rag_payload.get("conversation_context") or ""),
        client_context=_client_context_block(payload.get("client_context")),
        custom_instructions=custom_system or "",
        response_language=str(rag_payload.get("response_language") or "auto"),
    )
    return {
        "query": payload["query"],
        "config": config,
        "attached_files": attached_files,
        "system_prompt": system_prompt,
        "settings": rag_payload["settings"],
        "provider": rag_payload["provider"],
        "model": rag_payload["model"],
        "provider_name": rag_payload["provider_config"].get("name", rag_payload["provider"]),
        "temperature": rag_payload["temperature"],
        "response_language": rag_payload["response_language"],
        "conversation_id": conversation_id,
        "raw_conversation_id": raw_conversation_id,
        "context_docs": context_docs,
        "context": _serialize_context(
            context_docs,
            file_index_path=config["FILE_INDEX"],
            include_downloads=True,
        ),
        "sources": _serialize_sources(context_docs),
        "rag_error": rag_error,
    }


def _rag_context_for_code_prompt(context_docs, rag_error: str = "", max_chars: int = 12000) -> str:
    if rag_error and not context_docs:
        return f"Contesto RAG non disponibile: {rag_error}"
    if not context_docs:
        return "Nessun contesto documentale recuperato."

    remaining = max_chars
    blocks = []
    for index, doc in enumerate(context_docs, start=1):
        metadata = getattr(doc, "metadata", {}) or {}
        source = os.path.basename(str(metadata.get("source") or "documento"))
        text = " ".join(str(getattr(doc, "page_content", "") or "").split())
        if not text:
            continue
        snippet = text[: max(0, min(len(text), remaining - 120))]
        if not snippet:
            break
        blocks.append(f"[{index}] Fonte: {source}\n{snippet}")
        remaining -= len(snippet)
        if remaining <= 300:
            break
    return "\n\n".join(blocks) or "Nessun contesto documentale recuperato."


def _generate_code_for_interpreter(prepared: dict) -> str:
    from utils.providers.provider_factory import ProviderFactory

    provider_instance = ProviderFactory.get_provider(
        model=prepared["model"],
        provider=prepared["provider"],
        settings=prepared["settings"],
    )
    code_text = provider_instance.generate(
        system=prepared["system_prompt"],
        user=f"Genera codice Python per questa richiesta: {prepared['query']}",
        model=prepared["model"],
        temperature=0.0,
    )
    return _extract_code_block(code_text)


def _execute_interpreter_code(prepared: dict, code: str) -> dict:
    from utils.code_interpreter import CodeInterpreter

    interpreter = CodeInterpreter({"upload_folder": prepared["config"]["UPLOAD_FOLDER"]})
    return interpreter.execute(code, prepared["attached_files"])


def _append_code_interpreter_conversation_turn(payload: dict, prepared: dict, result: dict) -> None:
    from utils.rag_engine import _append_conversation_turn

    answer = _code_interpreter_answer_summary(result)
    _append_conversation_turn(
        prepared.get("conversation_id"),
        query=payload["query"],
        answer=answer,
        provider=prepared["provider"],
        model=prepared["model"],
        temperature=min(float(prepared.get("temperature") or 0.0), 0.2),
        settings=prepared["settings"],
    )


def _code_interpreter_answer_summary(result: dict) -> str:
    if result.get("success"):
        text = str(result.get("text") or "").strip()
        images = result.get("images") or []
        summary = "Code interpreter eseguito con successo."
        if text:
            summary += f"\nOutput:\n{text[:4000]}"
        if images:
            summary += f"\nGrafici generati: {len(images)}"
        return summary
    return f"Code interpreter fallito: {str(result.get('error') or 'errore sconosciuto')[:4000]}"


def _code_interpreter_meta_event(prepared: dict) -> dict:
    event = {
        "type": "meta",
        "model": prepared["model"],
        "provider": prepared["provider"],
        "provider_name": prepared["provider_name"],
        "response_language": prepared["response_language"],
        "attachments": [
            {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "size": item["size"],
                "container_path": item["container_path"],
            }
            for item in prepared["attached_files"]
        ],
        "context": prepared["context"],
        "sources": prepared["sources"],
    }
    if prepared.get("raw_conversation_id"):
        event["conversation_id"] = prepared["raw_conversation_id"]
    return event


def _code_interpreter_response(prepared: dict, code: str, result: dict) -> dict:
    response = {
        "type": "code_interpreter",
        "code": code,
        "result": result,
        "model": prepared["model"],
        "provider": prepared["provider"],
        "provider_name": prepared["provider_name"],
        "response_language": prepared["response_language"],
        "attachments": [
            {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "size": item["size"],
            }
            for item in prepared["attached_files"]
        ],
        "context": prepared["context"],
        "sources": prepared["sources"],
        "usage": None,
    }
    if prepared.get("raw_conversation_id"):
        response["conversation_id"] = prepared["raw_conversation_id"]
    if prepared.get("rag_error"):
        response["rag_warning"] = prepared["rag_error"]
    return response


def _any_user_api_keys(app: Flask) -> bool:
    from utils.user_store import UserStore

    return any(user.get("api_keys") for user in UserStore(app.config["USERS_FILE"]).list())


def _has_users(app: Flask) -> bool:
    from utils.user_store import UserStore

    return UserStore(app.config["USERS_FILE"]).has_users()


def _workspace_config(app: Flask) -> dict:
    return workspace_from_request(app).as_config()


def _sync_admin_settings_to_workspaces(app: Flask, settings: dict) -> None:
    workspace_root = app.config.get("WORKSPACE_DATA_DIR")
    if not workspace_root or not os.path.isdir(workspace_root):
        return

    patch = {
        key: deepcopy(settings[key])
        for key in _WORKSPACE_ADMIN_SETTING_KEYS
        if key in settings
    }
    if not patch:
        return

    for workspace_id in os.listdir(workspace_root):
        settings_path = os.path.join(workspace_root, workspace_id, "settings.json")
        if not os.path.isfile(settings_path):
            continue
        try:
            SettingsStore(settings_path).update(patch)
        except Exception as exc:
            log.warning("Unable to sync admin settings to workspace %s: %s", workspace_id, exc)


def _scoped_conversation_id(conversation_id: str | None) -> str | None:
    if not conversation_id:
        return None
    config = _workspace_config(current_app)
    return f"{config['WORKSPACE_ID']}:{conversation_id}"


def _resolve_system_prompt(system_prompt_id: str | None) -> str | None:
    """Resolve a system_prompt_id into its runtime template content.

    Looks up the prompt by ID (user personal first, then shared),
    resolves template variables, and returns the final text.
    Returns None when no prompt is selected or the ID is invalid.
    """
    if not system_prompt_id:
        return None
    system_prompt_id = str(system_prompt_id).strip()
    if not system_prompt_id:
        return None
    user = current_user()
    if not user:
        api_key = getattr(request, "api_key", None)
        api_user_id = api_key.get("user_id") if api_key else None
        if api_user_id:
            from utils.user_store import UserStore

            user = UserStore(current_app.config.get("USERS_FILE")).get(api_user_id)
    if not user:
        return None
    from utils.prompt_store import PromptStore

    ps = PromptStore(current_app.config.get("PROMPTS_DIR", "app/data"))
    prompt = ps.resolve(user["id"], system_prompt_id)
    if not prompt:
        return None
    return PromptStore.resolve_template(prompt["content"], user)


def _process_upload(app: Flask, config: dict | None = None) -> dict:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _process_upload_locked(app, config=config)


def _process_upload_locked(app: Flask, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    upload = _save_document_upload(app, config=config)

    def ocr_documents(file_path, parsed_documents=None, parse_error=""):
        return _ocr_documents_for_config(
            config,
            file_path,
            parsed_documents=parsed_documents,
            parse_error=parse_error,
        )

    return _index_saved_document_upload(config, **upload, ocr_documents_func=ocr_documents)


def _safe_relative_upload_path(raw_value: str | None, fallback_filename: str) -> str:
    fallback_filename = secure_filename(fallback_filename or "")
    raw_path = str(raw_value or fallback_filename or "").replace("\\", "/").strip()
    if not raw_path:
        raise ValidationError("Nome file non valido", "file")
    if raw_path.startswith("/") or raw_path.startswith("~"):
        raise ValidationError("Path relativo non valido", "relative_path")
    if len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha():
        raise ValidationError("Path relativo non valido", "relative_path")

    parts = []
    for raw_part in raw_path.split("/"):
        raw_part = raw_part.strip()
        if raw_part in {"", "."}:
            continue
        if raw_part == "..":
            raise ValidationError("Path relativo non valido", "relative_path")
        safe_part = secure_filename(raw_part)
        if not safe_part:
            raise ValidationError("Path relativo non valido", "relative_path")
        parts.append(safe_part)

    if not parts:
        raise ValidationError("Nome file non valido", "file")
    if fallback_filename and parts[-1] != fallback_filename:
        raise ValidationError("Path relativo e nome file non corrispondono", "relative_path")
    return "/".join(parts)


def _safe_file_index_key(raw_value: str | None) -> str:
    raw_path = str(raw_value or "").replace("\\", "/").strip()
    if not raw_path or raw_path.startswith("/") or raw_path.startswith("~"):
        return ""
    if len(raw_path) >= 2 and raw_path[1] == ":" and raw_path[0].isalpha():
        return ""

    parts = []
    for raw_part in raw_path.split("/"):
        raw_part = raw_part.strip()
        if raw_part in {"", "."}:
            continue
        if raw_part == "..":
            return ""
        safe_part = secure_filename(raw_part)
        if not safe_part:
            return ""
        parts.append(safe_part)
    return "/".join(parts)


def _upload_storage_path(config: dict, relative_path: str) -> str:
    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])
    file_path = os.path.abspath(os.path.join(upload_root, *relative_path.split("/")))
    if not file_path.startswith(upload_root + os.sep):
        raise ValidationError("Path relativo non valido", "relative_path")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    return file_path


def _save_document_upload(app: Flask, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    file = validate_file(
        file=request.files.get("file"),
        field_name="file",
        allowed_extensions=sorted(DOCUMENT_UPLOAD_EXTENSIONS),
        max_size_mb=app.config["MAX_UPLOAD_SIZE_MB"],
    )

    filename = secure_filename(file.filename)
    if not filename:
        raise ValidationError("Nome file non valido", "file")
    extension = filename.rsplit(".", 1)[1].lower()
    relative_path = _safe_relative_upload_path(request.form.get("relative_path"), filename)

    os.makedirs(config["UPLOAD_FOLDER"], exist_ok=True)
    file_path = _upload_storage_path(config, relative_path)
    file.save(file_path)
    return {
        "filename": relative_path,
        "file_path": file_path,
        "extension": extension,
        "relative_path": relative_path,
    }


def _index_saved_document_upload(
    config: dict,
    filename: str,
    file_path: str,
    extension: str,
    ocr_documents_func=None,
    extra_metadata: dict | None = None,
    relative_path: str = "",
) -> dict:
    from utils.document_indexer import index_saved_document

    if ocr_documents_func is None:
        def ocr_documents_func(file_path, parsed_documents=None, parse_error=""):
            return _ocr_documents_for_config(
                config,
                file_path,
                parsed_documents=parsed_documents,
                parse_error=parse_error,
            )

    index_extra = {**(extra_metadata or {})}
    if relative_path:
        index_extra["relative_path"] = relative_path

    result = index_saved_document(
        config,
        filename,
        file_path,
        extension,
        ocr_documents_func=ocr_documents_func,
        extra_metadata=index_extra,
    )
    if relative_path:
        result["relative_path"] = relative_path
    return result


def _source_type_for_upload_extension(extension: str) -> str:
    extension = extension.lower().lstrip(".")
    if extension == "pdf":
        return "pdf"
    if extension == "md":
        return "markdown"
    if extension == "csv":
        return "csv"
    if extension == "txt":
        return "text"
    if extension in AUDIO_UPLOAD_EXTENSIONS:
        return "audio"
    return "document"


def _ocr_extract_upload(app: Flask, persist: bool = False, config: dict | None = None) -> dict:
    from utils.ocr_provider import OCR_EXTENSIONS
    config = config or _workspace_config(app)

    file = validate_file(
        file=request.files.get("file"),
        field_name="file",
        allowed_extensions=sorted(OCR_EXTENSIONS),
        max_size_mb=app.config["MAX_UPLOAD_SIZE_MB"],
    )
    filename = secure_filename(file.filename)
    if not filename:
        raise ValidationError("Nome file non valido", "file")

    prefix = "" if persist else f".tmp_{uuid.uuid4().hex}_"
    os.makedirs(config["UPLOAD_FOLDER"], exist_ok=True)
    file_path = os.path.join(config["UPLOAD_FOLDER"], f"{prefix}{filename}")
    file.save(file_path)

    try:
        result = _extract_text_or_ocr(app, file_path, config=config)
        return {
            "filename": filename,
            "text": result["text"],
            "method": result["method"],
            "ocr_used": result["method"] == "ocr",
        }
    finally:
        if not persist and os.path.exists(file_path):
            os.remove(file_path)


def _extract_text_or_ocr(app: Flask, file_path: str, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    extension = os.path.splitext(file_path.lower())[1].lstrip(".")
    parse_error = ""
    if extension == "pdf":
        try:
            from utils.pdf_processor import extract_pdf_text

            text = extract_pdf_text(file_path)
            if text:
                return {"text": text, "method": "parsed"}
        except Exception as exc:
            parse_error = str(exc)
            log.warning("Transient PDF parser failed for OCR input: %s", exc)

    text = _run_ocr_for_config(config, file_path)
    if not text:
        raise ValidationError(parse_error or "OCR did not extract text", "file")
    return {"text": text, "method": "ocr"}


def _ocr_documents_for_file(
    app: Flask,
    file_path: str,
    parsed_documents: list | None = None,
    parse_error: str = "",
) -> tuple[list, dict, str]:
    return _ocr_documents_for_config(
        app.config,
        file_path,
        parsed_documents=parsed_documents,
        parse_error=parse_error,
    )


def _ocr_documents_for_config(
    config: dict,
    file_path: str,
    parsed_documents: list | None = None,
    parse_error: str = "",
) -> tuple[list, dict, str]:
    from utils.ocr_policy import decide_pdf_ocr_for_ingestion

    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    decision = decide_pdf_ocr_for_ingestion(settings, parsed_documents or [], parse_error=parse_error)
    if not decision.should_run:
        return [], {}, decision.error_message
    ocr_config = settings.get("ocr", {})

    try:
        text = _run_ocr_for_config(config, file_path, settings=settings)
    except Exception as exc:
        return [], {"ocr_used": False, "ocr_error": str(exc)}, str(exc)

    if not text:
        return [], {"ocr_used": True, "ocr_error": "OCR text is empty"}, "OCR text is empty"

    ocr_text_path = f"{file_path}.ocr.txt"
    with open(ocr_text_path, "w", encoding="utf-8") as ocr_file:
        ocr_file.write(text)
        ocr_file.write("\n")

    from utils.ocr_processor import process_ocr_text

    documents = process_ocr_text(
        file_path,
        text,
        settings_path=config["SETTINGS_FILE"],
        ocr_text_path=ocr_text_path,
    )
    provider_id = str(ocr_config.get("provider") or "")
    model = str(ocr_config.get("default_model") or "")
    for document in documents:
        document.metadata["source_type"] = "pdf"
        document.metadata["ocr_provider"] = provider_id
        document.metadata["ocr_model"] = model

    return documents, {
        "source_type": "pdf",
        "ocr_used": True,
        "ocr_provider": provider_id,
        "ocr_model": model,
        "ocr_text_path": ocr_text_path,
    }, ""


def _run_ocr_for_file(app: Flask, file_path: str, settings: dict | None = None) -> str:
    return _run_ocr_for_config(app.config, file_path, settings=settings)


def _run_ocr_for_config(config: dict, file_path: str, settings: dict | None = None) -> str:
    from utils.ocr_provider import get_ocr_provider

    settings = settings or SettingsStore(config["SETTINGS_FILE"]).load()
    try:
        provider = get_ocr_provider(settings)
        return provider.extract_text(file_path).strip()
    except Exception as exc:
        raise ValidationError(str(exc), "file") from exc


def _download_indexed_file(app: Flask, filename: str, config: dict | None = None) -> Response:
    config = config or _workspace_config(app)
    filename = _safe_file_index_key(filename)
    if not filename:
        raise ValidationError("Nome file non valido", "filename")
    file_index = FileIndex(config["FILE_INDEX"])
    entry = file_index.get(filename)
    if not entry:
        raise ValidationError("File non trovato", "filename", code="not_found")

    source = entry.get("path") or os.path.join(config["UPLOAD_FOLDER"], filename)
    source_path = os.path.abspath(source)
    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])

    if not source_path.startswith(upload_root + os.sep):
        raise ValidationError("Path non valida", "filename")

    if not os.path.isfile(source_path):
        raise ValidationError("File non trovato", "filename", code="not_found")

    from flask import send_file
    return send_file(
        source_path,
        as_attachment=True,
        download_name=os.path.basename(filename),
        mimetype=mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )


def _delete_indexed_file(app: Flask, filename: str | None, config: dict | None = None) -> dict:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _delete_indexed_file_locked(app, filename, config=config)


def _delete_indexed_file_locked(app: Flask, filename: str | None, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    filename = _safe_file_index_key(filename)
    if not filename:
        raise ValidationError("Nome file non valido", "filename")

    file_index = FileIndex(config["FILE_INDEX"])
    entry = file_index.get(filename)
    if not entry:
        raise ValidationError("File indicizzato non trovato", "filename", code="not_found")

    source = entry.get("path") or os.path.join(config["UPLOAD_FOLDER"], filename)

    from utils.chroma_manager import delete_documents_by_source
    from utils.rag_engine import clear_cache

    chunks_deleted = delete_documents_by_source(source, collection_name=config.get("CHROMA_COLLECTION"))
    file_index.remove(filename)
    uploaded_file_deleted = _delete_uploaded_file_if_safe(app, source, config=config)
    transcript_deleted = _delete_transcript_if_safe(app, entry.get("transcript_path", ""), config=config)
    ocr_text_deleted = _delete_transcript_if_safe(app, entry.get("ocr_text_path", ""), config=config)
    clear_cache()
    return {
        "message": f"{filename} rimosso dalla knowledge base",
        "filename": filename,
        "source": source,
        "chunks_deleted": chunks_deleted,
        "file_deleted": uploaded_file_deleted,
        "transcript_deleted": transcript_deleted,
        "ocr_text_deleted": ocr_text_deleted,
    }


def _delete_uploaded_file_if_safe(app: Flask, source: str, config: dict | None = None) -> bool:
    config = config or _workspace_config(app)
    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])
    source_path = os.path.abspath(source)
    if not source_path.startswith(upload_root + os.sep):
        return False
    if not os.path.isfile(source_path):
        return False
    os.remove(source_path)
    _delete_empty_upload_parents(source_path, upload_root)
    return True


def _delete_transcript_if_safe(app: Flask, source: str, config: dict | None = None) -> bool:
    config = config or _workspace_config(app)
    if not source:
        return False
    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])
    source_path = os.path.abspath(source)
    if not source_path.startswith(upload_root + os.sep):
        return False
    if not os.path.isfile(source_path):
        return False
    os.remove(source_path)
    _delete_empty_upload_parents(source_path, upload_root)
    return True


def _delete_empty_upload_parents(source_path: str, upload_root: str) -> None:
    parent = os.path.dirname(source_path)
    while parent and parent != upload_root and parent.startswith(upload_root + os.sep):
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)


def _upload_json_response(app: Flask):
    try:
        config = _workspace_config(app)
        if _async_upload_requested():
            payload, status_code = _start_upload_job(app, "file", config=config)
            return jsonify(payload), status_code
        return jsonify(_process_upload(app, config=config))
    except ValidationError as e:
        return jsonify(e.to_dict()), 400
    except Exception as e:
        log.error(f"Errore upload: {e}")
        return jsonify(error=str(e), status="server_error"), 500


def _process_audio_upload(app: Flask, config: dict | None = None) -> dict:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _process_audio_upload_locked(app, config=config)


def _process_audio_upload_locked(app: Flask, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    upload = _save_audio_upload(app, config=config)
    return _index_saved_audio_upload(config, **upload)


def _save_audio_upload(app: Flask, config: dict | None = None) -> dict:
    from utils.audio_processor import AUDIO_EXTENSIONS
    config = config or _workspace_config(app)

    file = validate_file(
        file=request.files.get("file"),
        field_name="file",
        allowed_extensions=sorted(AUDIO_EXTENSIONS),
        max_size_mb=app.config["MAX_AUDIO_UPLOAD_SIZE_MB"],
    )
    filename = secure_filename(file.filename)
    if not filename:
        raise ValidationError("Nome file non valido", "file")
    relative_path = _safe_relative_upload_path(request.form.get("relative_path"), filename)

    os.makedirs(config["UPLOAD_FOLDER"], exist_ok=True)
    file_path = _upload_storage_path(config, relative_path)
    file.save(file_path)
    language_override = _audio_language_override()
    return {
        "filename": relative_path,
        "file_path": file_path,
        "language_override": language_override,
        "relative_path": relative_path,
    }


def _index_saved_audio_upload(
    config: dict,
    filename: str,
    file_path: str,
    language_override: str | None = None,
    relative_path: str = "",
) -> dict:
    from utils.audio_processor import process_transcript
    from utils.chroma_manager import add_documents_to_chroma, delete_documents_by_source, find_document_by_id
    from utils.document_indexer import normalize_metadata_values
    from utils.rag_engine import clear_cache
    from utils.voice_provider import get_voice_provider

    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    language_hint = _audio_language_hint(settings, language_override)
    transcript = get_voice_provider(settings).transcribe(file_path, language=language_override)
    transcript_path = f"{file_path}.transcript.txt"
    with open(transcript_path, "w", encoding="utf-8") as transcript_file:
        transcript_file.write(transcript)
        transcript_file.write("\n")

    documents = process_transcript(
        file_path,
        transcript,
        settings_path=config["SETTINGS_FILE"],
        transcript_path=transcript_path,
    )
    index_extra = {
        "source_type": "audio",
        "transcript_path": transcript_path,
    }
    if relative_path:
        index_extra["relative_path"] = relative_path
    document_extra = normalize_metadata_values(index_extra)
    for document in documents:
        document.metadata = {**(document.metadata or {}), **document_extra}

    collection_name = config.get("CHROMA_COLLECTION")
    replaced_chunks = delete_documents_by_source(file_path, collection_name=collection_name)
    if not documents:
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            0,
            status="empty",
            error="Audio transcript is empty",
            metadata=_index_record_metadata_for_config(
                config,
                extra=index_extra,
            ),
        )
        if replaced_chunks:
            clear_cache()
        raise ValidationError("Audio transcript is empty", "file")

    document_id = str(documents[0].metadata.get("document_id") or "")
    source_id = str(documents[0].metadata.get("source_id") or "")
    duplicate = find_document_by_id(document_id, exclude_source=file_path, collection_name=collection_name) if document_id else None
    if duplicate:
        duplicate_source = duplicate.get("source") or ""
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            0,
            status="duplicate",
            error=f"Content already indexed from {duplicate_source}",
            metadata=_index_record_metadata_for_config(
                config,
                document_id=document_id,
                source_id=source_id,
                extra={
                    **index_extra,
                    "duplicate_of_source": duplicate_source,
                    "indexed_chunks": duplicate.get("chunks", 0),
                },
            ),
        )
        if replaced_chunks:
            clear_cache()
        return {
            "message": f"{filename} already present in the knowledge base; not reindexed",
            "filename": filename,
            "chunks": 0,
            "status": "duplicate",
            "document_id": document_id,
            "source_type": "audio",
            "transcript": transcript,
            "language_hint": language_hint,
            "duplicate_of_source": duplicate_source,
            "relative_path": relative_path,
        }

    add_documents_to_chroma(documents, collection_name=collection_name)
    clear_cache()
    FileIndex(config["FILE_INDEX"]).record(
        filename,
        file_path,
        len(documents),
        status="indexed",
        metadata=_index_record_metadata_for_config(
            config,
            document_id=document_id,
            source_id=source_id,
            extra=index_extra,
        ),
    )
    return {
        "message": f"{filename} transcribed and indexed",
        "filename": filename,
        "chunks": len(documents),
        "status": "indexed",
        "document_id": document_id,
        "source_type": "audio",
        "transcript": transcript,
        "language_hint": language_hint,
        "relative_path": relative_path,
    }


def _audio_language_override() -> str | None:
    if "language" not in request.form:
        return None
    return _validate_language_hint(request.form.get("language", ""))


def _audio_language_hint(settings: dict, language_override: str | None) -> str:
    if language_override is not None:
        return language_override
    return _validate_language_hint(settings.get("voice", {}).get("stt_language", ""))


def _audio_json_response(app: Flask):
    try:
        config = _workspace_config(app)
        if _async_upload_requested():
            payload, status_code = _start_upload_job(app, "audio", config=config)
            return jsonify(payload), status_code
        return jsonify(_process_audio_upload(app, config=config))
    except ValidationError as e:
        return jsonify(e.to_dict()), 400
    except Exception as e:
        log.error(f"Errore upload audio: {e}")
        return jsonify(error=str(e), status="server_error"), 500


def _async_upload_requested() -> bool:
    return validate_boolean(request.args.get("async", False), field_name="async")


def _start_upload_job(app: Flask, upload_type: str, config: dict | None = None) -> tuple[dict, int]:
    config = config or _workspace_config(app)
    if upload_type == "file":
        upload = _save_document_upload(app, config=config)
        message = f"{upload['filename']} upload in elaborazione"
    elif upload_type == "audio":
        upload = _save_audio_upload(app, config=config)
        message = f"{upload['filename']} audio upload in elaborazione"
    else:
        raise ValidationError("Tipo job non valido", "type")

    queued = configured_queue_backend() == "redis"
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "type": f"{upload_type}_upload",
        "status": "queued" if queued else "running",
        "message": f"{message} in coda" if queued else message,
        "processed": 0,
        "total": 1,
        "current_file": upload["filename"],
        "filename": upload["filename"],
        "user_id": config.get("USER_ID"),
        "workspace_id": config.get("WORKSPACE_ID"),
        "errors": [],
        "result": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    payload, status_code = get_job_store().create_job(job)
    if status_code >= 400:
        return payload, status_code

    if queued:
        try:
            _enqueue_upload_job(job_id, config, upload_type, upload)
        except Exception as exc:
            _finish_job(job_id, "failed", f"Errore accodamento upload: {exc}")
            raise
    else:
        thread = threading.Thread(
            target=_run_upload_job,
            args=(job_id, config, upload_type, upload),
            daemon=True,
        )
        thread.start()
    return {"job_id": job_id, **(get_job_store().get(job_id) or payload)}, 202


def _job_runtime_config(app: Flask) -> dict:
    return _workspace_config(app)


def _enqueue_upload_job(job_id: str, config: dict, upload_type: str, upload: dict) -> None:
    from rq import Queue

    queue = Queue(queue_name(), connection=redis_connection())
    queue.enqueue(
        _run_upload_job,
        job_id,
        config,
        upload_type,
        upload,
        job_timeout="2h",
        result_ttl=3600,
        failure_ttl=86400,
    )


def _run_upload_job(job_id: str, config: dict, upload_type: str, upload: dict) -> None:
    _update_job(
        job_id,
        status="running",
        message=f"Elaborazione upload {upload.get('filename', '')}".strip(),
        current_file=upload.get("filename", ""),
    )
    try:
        if upload_type == "file":
            result = _index_saved_document_upload(config, **upload)
        elif upload_type == "audio":
            result = _index_saved_audio_upload(config, **upload)
        else:
            raise ValidationError("Tipo job non valido", "type")

        _update_job(job_id, processed=1, result=result)
        _finish_job(job_id, "completed", result.get("message", "Upload completato"))
    except ValidationError as exc:
        _append_job_error(job_id, upload.get("filename", ""), exc.message)
        _update_job(job_id, result=exc.to_dict())
        _finish_job(job_id, "failed", exc.message)
    except Exception as exc:
        log.error("Errore job upload %s: %s", job_id, exc)
        _append_job_error(job_id, upload.get("filename", ""), str(exc))
        _update_job(job_id, result={"error": str(exc), "status": "server_error"})
        _finish_job(job_id, "failed", str(exc))


def _get_job(job_id: str) -> dict | None:
    return get_job_store().get(job_id)


def _update_job(job_id: str, **patch) -> None:
    get_job_store().update(job_id, **patch)


def _append_job_error(job_id: str, filename: str, message: str) -> None:
    get_job_store().append_error(job_id, filename, message)


def _finish_job(job_id: str, status: str, message: str) -> None:
    get_job_store().finish(job_id, status, message)


def _data_source_from_form(form) -> dict:
    from utils.data_ingestion.registry import get_ingester
    from utils.secret_store import SecretStore
    from utils.settings_store import normalize_data_source

    plugin = form.get("plugin") or "email_imap"
    config = _workspace_config(current_app)
    secret_store = SecretStore(config["SECRETS_FILE"], key=config["SECRET_KEY"])
    if plugin == "microsoft_drive":
        raw_source = {
            "id": form.get("id") or form.get("name"),
            "name": form.get("name") or form.get("id"),
            "plugin": plugin,
            "enabled": form.get("enabled") == "on",
            **_data_source_sync_fields_from_form(form),
            "config": {
                "drive_id": form.get("drive_id", ""),
                "folder_path": form.get("folder_path", ""),
                "item_id": form.get("item_id", ""),
                "recursive": form.get("recursive") == "on",
                "max_files": form.get("max_files", "50"),
                "max_file_size_mb": form.get("max_file_size_mb", "10"),
                "include_extensions": form.get("include_extensions", "pdf,txt,md"),
            },
            "secrets_env": {
                "token_env": form.get("token_env", ""),
            },
        }
        secret_name = "token"
        secret_value = form.get("token", "")
    elif plugin == "folder_watch":
        raw_source = {
            "id": form.get("id") or form.get("name"),
            "name": form.get("name") or form.get("id"),
            "plugin": plugin,
            "enabled": form.get("enabled") == "on",
            **_data_source_sync_fields_from_form(form),
            "config": {
                "folder_path": form.get("folder_path", ""),
                "recursive": form.get("recursive") == "on",
                "include_extensions": form.get("include_extensions", "pdf,txt,md,csv"),
                "exclude_patterns": form.get("exclude_patterns", ""),
                "max_files": form.get("max_files", "100"),
            },
            "secrets_env": {},
        }
        secret_name = ""
        secret_value = ""
    else:
        raw_source = {
            "id": form.get("id") or form.get("name"),
            "name": form.get("name") or form.get("id"),
            "plugin": plugin,
            "enabled": form.get("enabled") == "on",
            **_data_source_sync_fields_from_form(form),
            "config": {
                "host": form.get("host", ""),
                "port": form.get("port", "993"),
                "use_ssl": form.get("use_ssl") == "on",
                "username": form.get("username", ""),
                "folder": form.get("folder", "INBOX"),
                "from_contains": form.get("from_contains", ""),
                "subject_contains": form.get("subject_contains", ""),
                "since": form.get("since", ""),
                "max_messages": form.get("max_messages", "25"),
                "include_body": form.get("include_body") == "on",
                "include_attachments": form.get("include_attachments") == "on",
            },
            "secrets_env": {
                "password_env": form.get("password_env", ""),
            },
        }
        secret_name = "password"
        secret_value = form.get("password", "")
    source = normalize_data_source(raw_source)
    if not source.get("id"):
        raise ValidationError("id data source obbligatorio", "id")
    validation_config = {**source["config"], **source["secrets_env"]}
    if secret_value:
        ref = secret_store.set_secret(
            config["WORKSPACE_ID"],
            f"{source['id']}:{secret_name}",
            secret_value,
        )
        source["secrets"] = {secret_name: {"mode": "user_secret", "ref": ref}}
        validation_config[secret_name] = secret_value
    get_ingester(source["plugin"]).validate_config(validation_config)
    return source


def _data_source_sync_fields_from_form(form) -> dict:
    interval_minutes = validate_integer(
        form.get("sync_interval_minutes", "15"),
        "sync_interval_minutes",
        min_value=1,
        max_value=43200,
    )
    return {
        "sync_enabled": form.get("sync_enabled") == "on",
        "sync_interval_seconds": interval_minutes * 60,
    }


def _start_data_source_sync_job(app: Flask, data_source_id: str) -> tuple[dict, int]:
    from utils.data_ingestion.jobs import start_data_source_sync_job

    return start_data_source_sync_job(_workspace_config(app), data_source_id)


def _enqueue_data_source_sync_job(job_id: str, config: dict, data_source_id: str) -> None:
    from utils.data_ingestion.jobs import enqueue_data_source_sync_job

    enqueue_data_source_sync_job(job_id, config, data_source_id)


def _run_data_source_sync_job(job_id: str, config: dict, data_source_id: str) -> None:
    from utils.data_ingestion.jobs import run_data_source_sync_job

    run_data_source_sync_job(job_id, config, data_source_id)


def _parse_tts_payload() -> dict:
    if not request.is_json:
        raise ValidationError("Content-Type deve essere application/json")
    data = request.get_json(silent=True) or {}
    text = validate_string(data.get("text"), "text", min_length=1, max_length=4000)
    voice = validate_string(data.get("voice"), "voice", max_length=80, required=False)
    audio_format = validate_string(data.get("format"), "format", max_length=12, required=False)
    if audio_format:
        audio_format = audio_format.lower()
    allowed_formats = {"mp3", "wav", "opus", "aac", "flac"}
    if audio_format and audio_format not in allowed_formats:
        raise ValidationError("format non valido", "format")
    return {"text": text, "voice": voice, "format": audio_format}


def _tts_response(app: Flask, config: dict | None = None) -> Response:
    from utils.voice_provider import content_type_for_format, get_voice_provider
    config = config or _workspace_config(app)

    payload = _parse_tts_payload()
    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    provider = get_voice_provider(settings)
    selected_format = payload["format"] or settings["voice"]["format"]
    audio = provider.synthesize(payload["text"], voice=payload["voice"], audio_format=selected_format)
    return Response(audio, mimetype=content_type_for_format(selected_format))


def _transcribe_audio(app: Flask, config: dict | None = None) -> dict:
    from utils.audio_processor import AUDIO_EXTENSIONS
    from utils.voice_provider import get_voice_provider
    config = config or _workspace_config(app)

    file = validate_file(
        file=request.files.get("file"),
        field_name="file",
        allowed_extensions=sorted(AUDIO_EXTENSIONS),
        max_size_mb=app.config["MAX_AUDIO_UPLOAD_SIZE_MB"],
    )
    filename = secure_filename(file.filename)
    if not filename:
        raise ValidationError("Nome file non valido", "file")

    upload_folder = config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)
    temp_audio_path = os.path.join(upload_folder, f".tmp_{uuid.uuid4().hex}_{filename}")
    file.save(temp_audio_path)

    try:
        settings = SettingsStore(config["SETTINGS_FILE"]).load()
        voice_config = settings.get("voice", {})

        if not voice_config.get("enabled"):
            raise ValidationError("Voice provider is not configured", "voice")
        from utils.voice_provider import voice_has_api_key

        if voice_config.get("requires_api_key", False) and not voice_has_api_key(voice_config):
            raise ValidationError("Voice provider API key is not configured", "voice")
        if not voice_config.get("stt_model"):
            raise ValidationError("STT model is not configured", "voice")
        if not voice_config.get("base_url"):
            raise ValidationError("Voice provider base URL is not configured", "voice")

        provider = get_voice_provider(settings)
        transcript = provider.transcribe(temp_audio_path)

        return {
            "transcript": transcript.strip(),
            "filename": filename,
        }
    finally:
        if os.path.exists(temp_audio_path):
            os.remove(temp_audio_path)


def _start_rebuild_index_job(app: Flask, config: dict | None = None) -> tuple[dict, int]:
    config = config or _workspace_config(app)
    job_id = uuid.uuid4().hex
    entries = FileIndex(config["FILE_INDEX"]).list()
    profile = _current_index_profile_for_config(config)
    ocr_profile = _current_index_profile_for_config(config, include_ocr=True)
    job = {
        "id": job_id,
        "type": "rebuild_index",
        "status": "queued" if configured_queue_backend() == "redis" else "running",
        "message": "Ricostruzione indice in coda" if configured_queue_backend() == "redis" else "Ricostruzione indice avviata",
        "processed": 0,
        "total": len(entries),
        "current_file": "",
        "errors": [],
        "profile": profile,
        "user_id": config.get("USER_ID"),
        "workspace_id": config.get("WORKSPACE_ID"),
        "started_at": time.time(),
        "finished_at": None,
    }
    payload, status_code = get_job_store().create_rebuild_job(job)
    if status_code == 409:
        return payload, status_code

    if configured_queue_backend() == "redis":
        try:
            _enqueue_rebuild_index_job(job_id, config, profile, entries, ocr_profile)
        except Exception as exc:
            _finish_rebuild_job(job_id, "failed", f"Errore accodamento rebuild: {exc}")
            raise
    else:
        thread = threading.Thread(
            target=_run_rebuild_index_job,
            args=(job_id, config, profile, entries, ocr_profile),
            daemon=True,
        )
        thread.start()
    return {"job_id": job_id, **(get_job_store().get(job_id) or payload)}, 202


def _enqueue_rebuild_index_job(
    job_id: str,
    config: dict,
    profile: dict,
    entries: list[dict],
    ocr_profile: dict | None,
) -> None:
    from rq import Queue

    queue = Queue(queue_name(), connection=redis_connection())
    queue.enqueue(
        _run_rebuild_index_job,
        job_id,
        config,
        profile,
        entries,
        ocr_profile,
        job_timeout="2h",
        result_ttl=3600,
        failure_ttl=86400,
    )


def _get_rebuild_job(job_id: str) -> dict | None:
    return get_job_store().get(job_id)


def _active_rebuild_jobs_count() -> int:
    try:
        return get_job_store().active_jobs_count()
    except Exception as exc:
        log.warning("Unable to read active rebuild jobs count: %s", exc)
        return 0


def _update_rebuild_job(job_id: str, **patch) -> None:
    get_job_store().update(job_id, **patch)


def _append_rebuild_error(job_id: str, filename: str, message: str) -> None:
    get_job_store().append_error(job_id, filename, message)


def _finish_rebuild_job(job_id: str, status: str, message: str) -> None:
    get_job_store().finish(job_id, status, message)


def _run_rebuild_index_job(
    job_id: str,
    config: dict,
    profile: dict,
    entries: list[dict],
    ocr_profile: dict | None = None,
) -> None:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _run_rebuild_index_job_locked(job_id, config, profile, entries, ocr_profile)


def _run_rebuild_index_job_locked(
    job_id: str,
    config: dict,
    profile: dict,
    entries: list[dict],
    ocr_profile: dict | None = None,
) -> None:
    file_index = FileIndex(config["FILE_INDEX"])
    seen_document_ids: dict[str, str] = {}
    if ocr_profile is None:
        settings = SettingsStore(config["SETTINGS_FILE"]).load()
        ocr_profile = _index_profile_from_settings(settings, include_ocr=True)

    try:
        _update_rebuild_job(job_id, status="running", message="Ricostruzione indice avviata")
        from utils.chroma_manager import add_documents_to_chroma, reset_chroma_collection
        from utils.document_indexer import apply_extra_metadata_to_documents, ingestion_metadata_from_entry
        from utils.providers.embedding_factory import EmbeddingFactory
        from utils.rag_engine import clear_cache

        EmbeddingFactory.reset_cache()
        collection_name = config.get("CHROMA_COLLECTION")
        reset_chroma_collection(collection_name=collection_name)
        clear_cache()

        for index, entry in enumerate(entries):
            filename = _entry_filename(entry)
            _update_rebuild_job(
                job_id,
                processed=index,
                current_file=filename,
                message=f"Ricostruzione {filename}",
            )

            try:
                file_path, documents, source_type, rebuild_extra = _documents_for_rebuild(config, entry)
                ingestion_extra = ingestion_metadata_from_entry(entry)
                if ingestion_extra:
                    apply_extra_metadata_to_documents(documents, ingestion_extra)
                    source_type = ingestion_extra.get("source_type", source_type)
                    rebuild_extra = {**rebuild_extra, **ingestion_extra}
                if not documents:
                    file_index.record(
                        filename,
                        file_path,
                        0,
                        status="empty",
                        error="Il PDF non contiene testo indicizzabile",
                        metadata=_index_profile_metadata(
                            _profile_for_index_extra(profile, ocr_profile, rebuild_extra),
                            {
                                "source_type": source_type,
                                "transcript_path": entry.get("transcript_path", ""),
                                "ocr_text_path": rebuild_extra.get("ocr_text_path", ""),
                            },
                        ),
                    )
                    _append_rebuild_error(job_id, filename, "No indexable text found")
                    continue

                document_id = str(documents[0].metadata.get("document_id") or "")
                source_id = str(documents[0].metadata.get("source_id") or "")
                if document_id and document_id in seen_document_ids:
                    duplicate_source = seen_document_ids[document_id]
                    file_index.record(
                        filename,
                        file_path,
                        0,
                        status="duplicate",
                        error=f"Contenuto gia' indicizzato da {duplicate_source}",
                        metadata=_index_profile_metadata(
                            _profile_for_index_extra(profile, ocr_profile, rebuild_extra),
                            {
                                "document_id": document_id,
                                "source_id": source_id,
                                "source_type": source_type,
                                "transcript_path": entry.get("transcript_path", ""),
                                **rebuild_extra,
                                "duplicate_of_source": duplicate_source,
                            },
                        ),
                    )
                    continue

                add_documents_to_chroma(documents, collection_name=collection_name)
                if document_id:
                    seen_document_ids[document_id] = file_path
                file_index.record(
                    filename,
                    file_path,
                    len(documents),
                    status="indexed",
                    metadata=_index_profile_metadata(
                        _profile_for_index_extra(profile, ocr_profile, rebuild_extra),
                        {
                            "document_id": document_id,
                            "source_id": source_id,
                            "source_type": source_type,
                            "transcript_path": entry.get("transcript_path", ""),
                            **rebuild_extra,
                        },
                    ),
                )
            except Exception as e:
                message = str(e)
                source = str(entry.get("path") or os.path.join(config["UPLOAD_FOLDER"], filename))
                file_index.record(
                    filename,
                    source,
                    0,
                    status="error",
                    error=message,
                    metadata=_index_profile_metadata(profile),
                )
                _append_rebuild_error(job_id, filename, message)
                log.error(f"Errore ricostruzione indice per {filename}: {e}")
            finally:
                _update_rebuild_job(job_id, processed=index + 1)

        clear_cache()
        final_job = _get_rebuild_job(job_id) or {}
        errors = final_job.get("errors", [])
        if errors:
            _finish_rebuild_job(
                job_id,
                "completed_with_errors",
                f"Ricostruzione completata con {len(errors)} errore/i",
            )
        else:
            _finish_rebuild_job(job_id, "completed", "Ricostruzione completata")
    except Exception as e:
        log.error(f"Errore ricostruzione indice: {e}")
        _append_rebuild_error(job_id, "indice", str(e))
        _finish_rebuild_job(job_id, "failed", str(e))


def _safe_rebuild_source_path(config: dict, entry: dict) -> str:
    filename = _entry_filename(entry)
    source = entry.get("path") or os.path.join(config["UPLOAD_FOLDER"], filename)
    source_path = os.path.abspath(str(source))
    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])

    if not source_path.startswith(upload_root + os.sep):
        raise ValidationError("Path file non valida", "file")
    extension = os.path.splitext(source_path.lower())[1].lstrip(".")
    if extension not in DOCUMENT_UPLOAD_EXTENSIONS | AUDIO_UPLOAD_EXTENSIONS:
        raise ValidationError("Formato file non supportato", "file")
    if not os.path.isfile(source_path):
        raise ValidationError("File non trovato su disco", "file", code="not_found")
    return source_path


def _documents_for_rebuild(config: dict, entry: dict) -> tuple[str, list, str, dict]:
    from utils.audio_processor import AUDIO_EXTENSIONS, process_transcript
    from utils.pdf_processor import process_pdf
    from utils.text_processor import TEXT_EXTENSIONS, process_text_file

    file_path = _safe_rebuild_source_path(config, entry)
    extension = os.path.splitext(file_path.lower())[1].lstrip(".")
    source_type = entry.get("source_type") or ("audio" if extension in AUDIO_EXTENSIONS else "pdf")
    if source_type == "audio":
        transcript_path = entry.get("transcript_path") or f"{file_path}.transcript.txt"
        if not os.path.isfile(transcript_path):
            raise ValidationError("Transcript audio non trovato su disco", "file", code="not_found")
        with open(transcript_path, "r", encoding="utf-8") as transcript_file:
            transcript = transcript_file.read()
        return file_path, process_transcript(
            file_path,
            transcript,
            settings_path=config["SETTINGS_FILE"],
            transcript_path=transcript_path,
        ), "audio", {"transcript_path": transcript_path}

    if extension in TEXT_EXTENSIONS:
        source_type = _source_type_for_upload_extension(extension)
        return file_path, process_text_file(
            file_path,
            settings_path=config["SETTINGS_FILE"],
        ), source_type, {}

    parse_error = ""
    try:
        documents = process_pdf(file_path, settings_path=config["SETTINGS_FILE"])
    except Exception as exc:
        documents = []
        parse_error = str(exc)
        log.warning("PDF parser failed during rebuild for %s: %s", file_path, exc)
    ocr_documents, ocr_extra, ocr_error = _ocr_documents_for_config(
        config,
        file_path,
        parsed_documents=documents,
        parse_error=parse_error,
    )
    if ocr_documents:
        return file_path, ocr_documents, "pdf", ocr_extra
    if documents:
        return file_path, documents, "pdf", {}
    if parse_error or ocr_error:
        log.warning("No rebuild documents for %s: %s", file_path, ocr_error or parse_error)
    return file_path, documents, "pdf", ocr_extra


def _entry_filename(entry: dict) -> str:
    filename = _safe_file_index_key(str(entry.get("filename") or ""))
    if filename:
        return filename
    return secure_filename(os.path.basename(str(entry.get("path") or "documento.pdf"))) or "documento.pdf"


def _current_index_profile(app: Flask, include_ocr: bool = False) -> dict:
    return _current_index_profile_for_config(app.config, include_ocr=include_ocr)


def _current_index_profile_for_config(config: dict, include_ocr: bool = False) -> dict:
    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    return _index_profile_from_settings(settings, include_ocr=include_ocr)


def _index_profile_from_settings(settings: dict, include_ocr: bool = False) -> dict:
    rag = settings["rag"]
    profile = {
        "embedding_provider": rag["embedding_provider"],
        "embedding_model": rag["embedding_model"],
        "chunk_size": rag["chunk_size"],
        "chunk_overlap": rag["chunk_overlap"],
    }
    if include_ocr:
        ocr = settings.get("ocr", {})
        profile.update(
            {
                "ocr_enabled": bool(ocr.get("enabled")),
                "ocr_auto_on_empty_pdf": bool(ocr.get("auto_on_empty_pdf", True)),
                "ocr_provider": ocr.get("provider", ""),
                "ocr_model": ocr.get("default_model", ""),
                "ocr_mode": ocr.get("ocr_mode", ""),
                "ocr_output_format": ocr.get("output_format", ""),
            }
        )
    return profile


def _index_record_metadata(
    app: Flask,
    document_id: str = "",
    source_id: str = "",
    extra: dict | None = None,
) -> dict:
    return _index_record_metadata_for_config(app.config, document_id=document_id, source_id=source_id, extra=extra)


def _index_record_metadata_for_config(
    config: dict,
    document_id: str = "",
    source_id: str = "",
    extra: dict | None = None,
) -> dict:
    include_ocr = bool(extra and extra.get("ocr_used"))
    metadata = _index_profile_metadata(
        _current_index_profile_for_config(config, include_ocr=include_ocr),
        {"document_id": document_id, "source_id": source_id},
    )
    if extra:
        metadata.update(extra)
    return metadata


def _index_profile_metadata(profile: dict, extra: dict | None = None) -> dict:
    metadata = {"index_profile": dict(profile)}
    if extra:
        metadata.update(extra)
    return metadata


def _profile_for_index_extra(base_profile: dict, ocr_profile: dict, extra: dict | None = None) -> dict:
    return ocr_profile if extra and extra.get("ocr_used") else base_profile


def _profile_without_ocr(profile: dict) -> dict:
    return {key: value for key, value in profile.items() if key not in _OCR_INDEX_PROFILE_KEYS}


def _index_profile_matches_entry(entry: dict, base_profile: dict, ocr_profile: dict) -> bool:
    profile = entry.get("index_profile")
    if not isinstance(profile, dict):
        return False
    if entry.get("ocr_used"):
        return profile == ocr_profile
    return _profile_without_ocr(profile) == base_profile


def _index_rebuild_status(app: Flask, config: dict | None = None) -> dict:
    config = config or _workspace_config(app)
    profile = _current_index_profile_for_config(config)
    ocr_profile = _current_index_profile_for_config(config, include_ocr=True)
    entries = FileIndex(config["FILE_INDEX"]).list()
    indexed_entries = [entry for entry in entries if entry.get("status") == "indexed"]
    stale_entries = [
        entry for entry in indexed_entries
        if not _index_profile_matches_entry(entry, profile, ocr_profile)
    ]
    return {
        "current_profile": profile,
        "indexed_count": len(indexed_entries),
        "tracked_count": len(entries),
        "stale_count": len(stale_entries),
        "needs_rebuild": bool(stale_entries),
    }


def _parse_query_payload(require_json: bool = True) -> dict:
    if require_json and not request.is_json:
        raise ValidationError("Content-Type deve essere application/json")

    data = request.get_json(silent=True)
    if data is None:
        raise ValidationError("Body vuoto")

    query = validate_query(data.get("query"))
    stream = validate_boolean(data.get("stream", False), field_name="stream")
    stream_format = str(data.get("stream_format") or "text").strip().lower()
    if stream_format not in {"text", "ndjson"}:
        raise ValidationError("Formato stream non valido", "stream_format")
    temperature = validate_float(
        data.get("temperature"),
        field_name="temperature",
        min_value=0.0,
        max_value=1.0,
        required=False,
    )
    k = validate_integer(
        data.get("k"),
        field_name="k",
        min_value=1,
        max_value=50,
        required=False,
    )
    provider = data.get("provider") or None
    model = data.get("model") or None
    conversation_id = validate_conversation_id(data.get("conversation_id"), required=False)
    client_context = _validate_client_context(data.get("client_context"))
    response_language = _validate_response_language(data.get("response_language"))
    system_prompt_id = data.get("system_prompt_id") or None
    use_code_interpreter = validate_boolean(
        data.get("use_code_interpreter", False), field_name="use_code_interpreter"
    )
    attached_files = data.get("attached_files") or []
    if attached_files:
        if not isinstance(attached_files, list):
            raise ValidationError("attached_files deve essere una lista", "attached_files")
        for i, f in enumerate(attached_files):
            if not isinstance(f, dict):
                raise ValidationError(f"attached_files[{i}] deve essere un oggetto", "attached_files")
            file_id = f.get("id") or f.get("file_id")
            validate_string(
                file_id,
                f"attached_files[{i}].id",
                max_length=32,
                pattern=r"^[0-9a-f]{32}$",
                required=True,
            )
            validate_string(f.get("name"), f"attached_files[{i}].name", required=False)
    _validate_model_selection(model, provider, config=_workspace_config(current_app))

    return {
        "query": query,
        "model": model,
        "provider": provider,
        "conversation_id": conversation_id,
        "client_context": client_context,
        "response_language": response_language,
        "stream": stream,
        "stream_format": stream_format,
        "temperature": temperature,
        "k": k,
        "system_prompt_id": system_prompt_id,
        "use_code_interpreter": use_code_interpreter,
        "attached_files": attached_files,
    }


def _extract_code_block(text: str) -> str:
    """Extract Python code from a markdown code block."""
    import re as _re
    match = _re.search(r"```(?:python|py)?\n(.*?)```", text, _re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _validate_language_hint(value) -> str:
    language = str(value or "").strip().lower().replace("_", "-")
    if not language:
        return ""
    if len(language) > 16 or not all(char.isalnum() or char == "-" for char in language):
        raise ValidationError("language non valida", "language")
    return language


def _validate_response_language(value) -> str:
    language = str(value if value is not None else "auto").strip().lower().replace("_", "-")
    if not language or language == "auto":
        return "auto"
    if len(language) > 16 or not all(char.isalnum() or char == "-" for char in language):
        raise ValidationError("response_language non valida", "response_language")
    return language


def _validate_client_context(value) -> dict | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValidationError("client_context deve essere un oggetto", "client_context")

    allowed_fields = {
        "site_name": 120,
        "page_title": 180,
        "page_url": 500,
        "post_type": 80,
        "locale": 40,
        "instructions": 1200,
    }
    sanitized: dict[str, str] = {}
    remaining_chars = 2000
    for key, max_length in allowed_fields.items():
        if remaining_chars <= 0:
            break
        raw = value.get(key)
        if raw in (None, ""):
            continue
        text = validate_string(
            raw,
            field_name=f"client_context.{key}",
            max_length=10000,
            required=False,
        )
        text = " ".join(str(text).replace("\x00", "").split())
        if not text:
            continue
        text = text[:max_length]
        if len(text) > remaining_chars:
            text = text[:remaining_chars]
        sanitized[key] = text
        remaining_chars -= len(text)
    return sanitized or None


def _validate_model_selection(model: str | None, provider: str | None, config: dict | None = None) -> None:
    """
    Convalida la selezione di un modello. Accetta tre input:
      1. model="provider:model" (es. "mistral:mistral-medium")
      2. model="gemma3:4b", provider="ollama" (modello con : nei due campi separati)
      3. model="mistral-medium", provider="mistral" (semplice, senza : nel nome)
    """
    config = config or _workspace_config(current_app)
    registry = ProviderRegistry(SettingsStore(config["SETTINGS_FILE"]).load())
    providers = registry.providers()

    # Se il provider è specificato, deve esistere
    if provider and provider not in providers:
        raise ValidationError("Provider non valido", "provider")

    # Se il modello non è specificato, delega al default
    if not model:
        return

    # Caso 1+2: provider specificato → valida il modello direttamente
    if provider:
        if model not in providers[provider].get("models", []):
            raise ValidationError("Modello non valido per il provider selezionato", "model")
        return

    # Caso 3: provider non specificato → tenta di estrarlo dal modello o cerca globale
    if ":" in model:
        # Formato "provider:model" (es. "ollama:codellama:7b")
        # split(":") con maxsplit=1 gestisce anche "ollama:codellama:7b"
        # ma attenzione: "gemma3:4b" senza provider qui fallirebbe.
        # Quindi il ramo sotto funziona SOLO se ":" fa da separatore provider:model
        possible_provider = model.split(":", 1)[0]
        if possible_provider in providers:
            selected_model = model.split(":", 1)[1]
            if selected_model not in providers[possible_provider].get("models", []):
                raise ValidationError("Modello non valido", "model")
            return

    # Nessun provider, senza :, cerca in tutti i provider
    if model not in registry.model_ids():
        raise ValidationError("Modello non valido", "model")


def _handle_config_post(store: SettingsStore) -> None:
    settings = store.load()
    action = request.form.get("action")

    if action == "save_rag":
        embedding_model = request.form.get("embedding_model") or "local/sentence-transformers/all-MiniLM-L6-v2"
        # Estrarre provider e modello dal valore combinato "provider/model"
        if "/" in embedding_model:
            embedding_provider, embedding_model_name = embedding_model.split("/", 1)
        else:
            # Fallback per backward compatibility
            embedding_provider = "local"
            embedding_model_name = embedding_model
        
        settings["rag"].update(
            {
                "chunk_size": request.form.get("chunk_size"),
                "chunk_overlap": request.form.get("chunk_overlap"),
                "query_k": request.form.get("query_k"),
                "temperature": request.form.get("temperature"),
                "embedding_provider": embedding_provider,
                "embedding_model": embedding_model_name,
                "default_provider": request.form.get("default_provider") or settings["rag"].get("default_provider"),
                "default_model": request.form.get("default_model") or settings["rag"].get("default_model"),
                "enable_cache": "enable_cache" in request.form,
                "cache_ttl": request.form.get("cache_ttl"),
                "use_internal_knowledge": "use_internal_knowledge" in request.form,
            }
        )
        store.save(settings)
        return

    if action == "save_reranker":
        reranker_model = request.form.get("reranker_model") or settings["rag"].get("reranker_model")
        reranker_type, reranker_model_name = _split_provider_model_value(reranker_model)
        if not reranker_model_name:
            reranker_type = "local"
            reranker_model = f"local/{reranker_model}"

        current_rag = settings["rag"]
        reranker_api_key = current_rag.get("reranker_api_key") or current_rag.get("reranker_regolo_api_key", "")
        posted_reranker_key = request.form.get("reranker_api_key", "").strip()
        if posted_reranker_key:
            reranker_api_key = posted_reranker_key

        settings["rag"].update(
            {
                "reranker_enabled": "reranker_enabled" in request.form,
                "reranker_model": reranker_model,
                "reranker_type": reranker_type,
                "reranker_top_n": request.form.get("reranker_top_n"),
                "reranker_diversity_mode": request.form.get("reranker_diversity_mode"),
                "reranker_mmr_lambda": request.form.get("reranker_mmr_lambda"),
                "reranker_mmr_candidate_pool": request.form.get("reranker_mmr_candidate_pool"),
                "reranker_threshold": request.form.get("reranker_threshold"),
                "reranker_api_key": reranker_api_key,
                "reranker_regolo_api_key": current_rag.get("reranker_regolo_api_key", ""),
            }
        )
        store.save(settings)
        return

    if action == "save_voice":
        current_voice = settings.get("voice", {})
        selected_provider = request.form.get("voice_provider") or current_voice.get("provider")
        posted_key = request.form.get("voice_api_key", "").strip()
        requires_key = current_voice.get("requires_api_key")
        if "voice_requires_api_key" in request.form:
            requires_key = request.form.getlist("voice_requires_api_key")[-1]
        voice_settings = normalize_voice_settings(
            {
                "provider": selected_provider,
                "enabled": "voice_enabled" in request.form,
                "base_url": request.form.get("voice_base_url"),
                "api_key_env": current_voice.get("api_key_env"),
                "requires_api_key": requires_key,
                "api_key": posted_key or (
                    current_voice.get("api_key", "")
                    if selected_provider == current_voice.get("provider")
                    else ""
                ),
                "stt_model": request.form.get("voice_stt_model"),
                "stt_language": request.form.get("voice_stt_language"),
                "tts_model": request.form.get("voice_tts_model"),
                "voice": request.form.get("voice_default_voice"),
                "format": request.form.get("voice_format"),
            },
            settings.get("voice_providers", []),
        )
        if voice_settings["enabled"] and not voice_settings["base_url"]:
            raise ValidationError("Voice provider base URL is required", "voice_base_url")
        settings["voice"] = voice_settings
        store.save(settings)
        return

    if action == "save_ocr":
        current_ocr = settings.get("ocr", {})
        selected_provider = request.form.get("ocr_provider") or current_ocr.get("provider")
        posted_key = request.form.get("ocr_api_key", "").strip()
        requires_key = current_ocr.get("requires_api_key")
        if "ocr_requires_api_key" in request.form:
            requires_key = request.form.getlist("ocr_requires_api_key")[-1]
        ocr_settings = normalize_ocr_settings(
            {
                "provider": selected_provider,
                "enabled": "ocr_enabled" in request.form,
                "auto_on_empty_pdf": "ocr_auto_on_empty_pdf" in request.form,
                "base_url": request.form.get("ocr_base_url"),
                "api_key_env": current_ocr.get("api_key_env"),
                "requires_api_key": requires_key,
                "api_key": posted_key or (
                    current_ocr.get("api_key", "")
                    if selected_provider == current_ocr.get("provider")
                    else ""
                ),
                "default_model": request.form.get("ocr_default_model"),
                "ocr_mode": request.form.get("ocr_mode"),
                "input_types": request.form.getlist("ocr_input_types"),
                "output_format": request.form.get("ocr_output_format"),
                "supports_layout": "ocr_supports_layout" in request.form,
                "supports_tables": "ocr_supports_tables" in request.form,
            },
            settings.get("ocr_providers", []),
        )
        if ocr_settings["enabled"]:
            if not ocr_settings["provider"]:
                raise ValidationError("OCR provider is required", "ocr_provider")
            if not ocr_settings["base_url"]:
                raise ValidationError("OCR provider base URL is required", "ocr_base_url")
            if not ocr_settings["default_model"]:
                raise ValidationError("OCR model is required", "ocr_default_model")
        settings["ocr"] = ocr_settings
        store.save(settings)
        return

    if action == "save_provider":
        provider = normalize_custom_provider(
            {
                "id": request.form.get("provider_id"),
                "name": request.form.get("provider_name"),
                "base_url": request.form.get("base_url"),
                "api_key": request.form.get("provider_api_key"),
                "models": request.form.get("models"),
                "default_model": request.form.get("provider_default_model"),
                "enabled": "provider_enabled" in request.form,
            }
        )
        if not provider["id"]:
            raise ValidationError("ID provider obbligatorio", "provider_id")
        if not provider["base_url"]:
            raise ValidationError("Base URL obbligatorio", "base_url")
        if not provider["models"]:
            raise ValidationError("Inserisci almeno un modello", "models")

        existing = {
            item["id"]: item for item in settings.get("custom_providers", [])
        }
        if not provider["api_key"] and provider["id"] in existing:
            provider["api_key"] = existing[provider["id"]].get("api_key", "")
        # API key è opzionale per provider come Ollama che non richiedono autenticazione
        # Se l'utente ha inserito una nuova API key, la salviamo; altrimenti manteniamo quella esistente

        existing[provider["id"]] = provider
        settings["custom_providers"] = list(existing.values())
        store.save(settings)
        return

    if action == "delete_provider":
        provider_id = request.form.get("provider_id")
        settings["custom_providers"] = [
            item for item in settings.get("custom_providers", [])
            if item.get("id") != provider_id
        ]
        store.save(settings)
        return

    if action == "save_embedding_provider":
        provider = normalize_embedding_provider(
            {
                "id": request.form.get("embedding_provider_id"),
                "name": request.form.get("embedding_provider_name"),
                "base_url": request.form.get("embedding_base_url"),
                "api_key_env": request.form.get("embedding_provider_api_key_env"),
                "requires_api_key": request.form.get("embedding_provider_requires_api_key"),
                "api_key": request.form.get("embedding_provider_api_key"),
                "models": request.form.get("embedding_models"),
                "default_model": request.form.get("embedding_provider_default_model"),
                "dimensions": request.form.get("embedding_dimensions"),
                "enabled": "embedding_provider_enabled" in request.form,
            }
        )
        if not provider["id"]:
            raise ValidationError("ID provider embeddings obbligatorio", "embedding_provider_id")
        reserved_embedding_ids = {"local", "sentence-transformers"} | {
            item.get("id", "") for item in load_builtin_embedding_providers()
        }
        if provider["id"] in reserved_embedding_ids:
            raise ValidationError("ID provider embeddings riservato", "embedding_provider_id")
        if not provider["base_url"]:
            raise ValidationError("Base URL provider embeddings obbligatoria", "embedding_base_url")
        if not provider["models"]:
            raise ValidationError("Inserisci almeno un modello embeddings", "embedding_models")

        existing = {
            item["id"]: item for item in settings.get("embedding_providers", [])
        }
        if not provider["api_key"] and provider["id"] in existing:
            provider["api_key"] = existing[provider["id"]].get("api_key", "")
        existing[provider["id"]] = provider
        settings["embedding_providers"] = list(existing.values())
        store.save(settings)
        return

    if action == "delete_embedding_provider":
        provider_id = request.form.get("embedding_provider_id")
        settings["embedding_providers"] = [
            item for item in settings.get("embedding_providers", [])
            if item.get("id") != provider_id
        ]
        if settings["rag"].get("embedding_provider") == provider_id:
            fallback = _embedding_provider_payload(settings)[0]
            settings["rag"]["embedding_provider"] = fallback["id"]
            settings["rag"]["embedding_model"] = fallback["default_model"]
        store.save(settings)
        return

    if action == "save_reranker_provider":
        provider = normalize_reranker_provider(
            {
                "id": request.form.get("reranker_provider_id"),
                "name": request.form.get("reranker_provider_name"),
                "base_url": request.form.get("reranker_base_url"),
                "api_key": request.form.get("reranker_provider_api_key"),
                "models": request.form.get("reranker_models"),
                "default_model": request.form.get("reranker_provider_default_model"),
                "reranker_mode": request.form.get("reranker_mode"),
                "enabled": "reranker_provider_enabled" in request.form,
            }
        )
        if not provider["id"]:
            raise ValidationError("ID provider ReRanking obbligatorio", "reranker_provider_id")
        reserved_reranker_ids = {"local"} | {
            item.get("id", "") for item in load_builtin_reranker_providers()
        }
        if provider["id"] in reserved_reranker_ids:
            raise ValidationError("ID provider ReRanking riservato", "reranker_provider_id")
        if not provider["base_url"]:
            raise ValidationError("Base URL provider ReRanking obbligatoria", "reranker_base_url")
        if not provider["models"]:
            raise ValidationError("Inserisci almeno un modello ReRanking", "reranker_models")

        existing = {
            item["id"]: item for item in settings.get("reranker_providers", [])
        }
        if not provider["api_key"] and provider["id"] in existing:
            provider["api_key"] = existing[provider["id"]].get("api_key", "")
        existing[provider["id"]] = provider
        settings["reranker_providers"] = list(existing.values())
        store.save(settings)
        return

    if action == "delete_reranker_provider":
        provider_id = request.form.get("reranker_provider_id")
        settings["reranker_providers"] = [
            item for item in settings.get("reranker_providers", [])
            if item.get("id") != provider_id
        ]
        if settings["rag"].get("reranker_model", "").startswith(f"{provider_id}/"):
            settings["rag"]["reranker_type"] = "local"
            settings["rag"]["reranker_model"] = "local/BAAI/bge-reranker-v2-m3"
        store.save(settings)
        return

    if action == "save_voice_provider":
        provider = normalize_voice_provider(
            {
                "id": request.form.get("voice_provider_id"),
                "name": request.form.get("voice_provider_name"),
                "base_url": request.form.get("voice_provider_base_url"),
                "api_key": request.form.get("voice_provider_api_key"),
                "stt_model": request.form.get("voice_provider_stt_model"),
                "tts_model": request.form.get("voice_provider_tts_model"),
                "voice": request.form.get("voice_provider_default_voice"),
                "format": request.form.get("voice_provider_format"),
                "requires_api_key": (request.form.getlist("voice_provider_requires_api_key") or ["0"])[-1],
                "enabled": "voice_provider_enabled" in request.form,
            }
        )
        if not provider["id"]:
            raise ValidationError("ID provider Voice obbligatorio", "voice_provider_id")
        reserved_voice_ids = {
            item.get("id", "") for item in load_builtin_voice_providers()
        }
        if provider["id"] in reserved_voice_ids:
            raise ValidationError("ID provider Voice riservato", "voice_provider_id")
        if not provider["base_url"]:
            raise ValidationError("Base URL provider Voice obbligatoria", "voice_provider_base_url")
        if not provider["stt_model"] and not provider["tts_model"]:
            raise ValidationError("Inserisci almeno un modello STT o TTS", "voice_provider_stt_model")

        existing = {
            item["id"]: item for item in settings.get("voice_providers", [])
        }
        if not provider["api_key"] and provider["id"] in existing:
            provider["api_key"] = existing[provider["id"]].get("api_key", "")
        existing[provider["id"]] = provider
        settings["voice_providers"] = list(existing.values())
        if settings.get("voice", {}).get("provider") == provider["id"]:
            settings["voice"] = normalize_voice_settings(settings["voice"], settings["voice_providers"])
        store.save(settings)
        return

    if action == "delete_voice_provider":
        provider_id = request.form.get("voice_provider_id")
        settings["voice_providers"] = [
            item for item in settings.get("voice_providers", [])
            if item.get("id") != provider_id
        ]
        if settings.get("voice", {}).get("provider") == provider_id:
            fallback = next((item for item in load_builtin_voice_providers() if item.get("id")), {})
            settings["voice"] = normalize_voice_settings(
                {
                    "provider": fallback.get("id", ""),
                    "enabled": settings.get("voice", {}).get("enabled", True),
                },
                settings["voice_providers"],
            )
        store.save(settings)
        return

    if action == "save_ocr_provider":
        provider = normalize_ocr_provider(
            {
                "id": request.form.get("ocr_provider_id"),
                "name": request.form.get("ocr_provider_name"),
                "base_url": request.form.get("ocr_provider_base_url"),
                "api_key_env": request.form.get("ocr_provider_api_key_env"),
                "requires_api_key": (request.form.getlist("ocr_provider_requires_api_key") or ["0"])[-1],
                "api_key": request.form.get("ocr_provider_api_key"),
                "models": request.form.get("ocr_models"),
                "default_model": request.form.get("ocr_provider_default_model"),
                "ocr_mode": request.form.get("ocr_provider_mode"),
                "input_types": request.form.getlist("ocr_provider_input_types"),
                "output_format": request.form.get("ocr_provider_output_format"),
                "supports_layout": "ocr_provider_supports_layout" in request.form,
                "supports_tables": "ocr_provider_supports_tables" in request.form,
                "enabled": "ocr_provider_enabled" in request.form,
            }
        )
        if not provider["id"]:
            raise ValidationError("ID provider OCR obbligatorio", "ocr_provider_id")
        reserved_ocr_ids = {
            item.get("id", "") for item in load_builtin_ocr_providers()
        }
        if provider["id"] in reserved_ocr_ids:
            raise ValidationError("ID provider OCR riservato", "ocr_provider_id")
        if not provider["base_url"]:
            raise ValidationError("Base URL provider OCR obbligatoria", "ocr_provider_base_url")
        if not provider["models"]:
            raise ValidationError("Inserisci almeno un modello OCR", "ocr_models")

        existing = {
            item["id"]: item for item in settings.get("ocr_providers", [])
        }
        if not provider["api_key"] and provider["id"] in existing:
            provider["api_key"] = existing[provider["id"]].get("api_key", "")
        existing[provider["id"]] = provider
        settings["ocr_providers"] = list(existing.values())
        if settings.get("ocr", {}).get("provider") == provider["id"]:
            settings["ocr"] = normalize_ocr_settings(settings["ocr"], settings["ocr_providers"])
        store.save(settings)
        return

    if action == "delete_ocr_provider":
        provider_id = request.form.get("ocr_provider_id")
        settings["ocr_providers"] = [
            item for item in settings.get("ocr_providers", [])
            if item.get("id") != provider_id
        ]
        if settings.get("ocr", {}).get("provider") == provider_id:
            fallback = next((item for item in load_builtin_ocr_providers() if item.get("id")), {})
            settings["ocr"] = normalize_ocr_settings(
                {
                    "provider": fallback.get("id", ""),
                    "enabled": False,
                    "auto_on_empty_pdf": settings.get("ocr", {}).get("auto_on_empty_pdf", True),
                },
                settings["ocr_providers"],
            )
        store.save(settings)
        return

    raise ValidationError("Azione configurazione non valida", "action")


def _models_response_payload() -> dict:
    settings = SettingsStore(current_app.config.get("SETTINGS_FILE", Config.paths.settings_file)).load()
    registry = ProviderRegistry(settings)
    default_provider, default_model, _provider_config = registry.resolve()
    return {
        "models": _model_payload(settings, default_provider, default_model),
        "default_provider": default_provider,
        "default_model": default_model,
        "default_value": f"{default_provider}:{default_model}",
    }


def _model_payload(
    settings: dict | None = None,
    default_provider: str | None = None,
    default_model: str | None = None,
) -> list[dict]:
    settings = settings or SettingsStore(current_app.config.get("SETTINGS_FILE", Config.paths.settings_file)).load()
    registry = ProviderRegistry(settings)
    if default_provider is None or default_model is None:
        default_provider, default_model, _provider_config = registry.resolve()

    return [
        {
            "id": model.id,
            "name": model.name,
            "provider": model.provider,
            "provider_name": model.provider_name,
            "value": f"{model.provider}:{model.id}",
            "is_default": model.provider == default_provider and model.id == default_model,
        }
        for model in registry.list_models()
    ]


def _llm_provider_payload(settings: dict | None = None) -> list[dict]:
    registry = ProviderRegistry(settings)
    providers = []
    try:
        provider_configs = registry.providers()
    except ModelConfigurationError:
        return []
    for provider_id, config in provider_configs.items():
        providers.append(
            {
                "id": provider_id,
                "name": config.get("name") or provider_id,
                "models": list(config.get("models", [])),
                "default_model": config.get("default_model", ""),
            }
        )
    return providers


def _embedding_provider_payload(settings: dict | None = None) -> list[dict]:
    from utils.providers.embedding_factory import EmbeddingFactory

    return EmbeddingFactory.list_provider_models(settings)


def _reranker_provider_payload(settings: dict | None = None) -> list[dict]:
    settings = settings or SettingsStore(current_app.config.get("SETTINGS_FILE", Config.paths.settings_file)).load()
    providers = [
        {
            "id": "local",
            "name": "Local BGE",
            "models": ["BAAI/bge-reranker-v2-m3"],
            "default_model": "BAAI/bge-reranker-v2-m3",
            "privacy_note": "Esegue il ReRanking sulla macchina locale.",
        }
    ]
    for provider in load_builtin_reranker_providers():
        if not provider.get("id"):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "models": list(provider.get("models", [])),
                "default_model": provider.get("default_model", ""),
                "reranker_mode": provider.get("reranker_mode", "chat_completions"),
                "privacy_note": provider.get("privacy_note", ""),
            }
        )
    for provider in settings.get("reranker_providers", []):
        if not provider.get("id") or not provider.get("enabled", True):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "models": list(provider.get("models", [])),
                "default_model": provider.get("default_model", ""),
                "reranker_mode": provider.get("reranker_mode", "chat_completions"),
                "privacy_note": "Invia documenti e query al provider ReRanking configurato.",
            }
        )
    return providers


def _voice_provider_payload(settings: dict | None = None) -> list[dict]:
    settings = settings or SettingsStore(current_app.config.get("SETTINGS_FILE", Config.paths.settings_file)).load()
    providers = []
    for provider in load_builtin_voice_providers():
        if not provider.get("id"):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url", ""),
                "api_key_env": provider.get("api_key_env", ""),
                "requires_api_key": bool(provider.get("requires_api_key", True)),
                "stt_model": provider.get("stt_model", ""),
                "stt_language": provider.get("stt_language", ""),
                "tts_model": provider.get("tts_model", ""),
                "voice": provider.get("voice", "alloy"),
                "format": provider.get("format", "mp3"),
                "privacy_note": provider.get("privacy_note", ""),
            }
        )
    for provider in settings.get("voice_providers", []):
        if not provider.get("id") or not provider.get("enabled", True):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url", ""),
                "api_key_env": provider.get("api_key_env", ""),
                "requires_api_key": bool(provider.get("requires_api_key", True)),
                "stt_model": provider.get("stt_model", ""),
                "stt_language": provider.get("stt_language", ""),
                "tts_model": provider.get("tts_model", ""),
                "voice": provider.get("voice", "alloy"),
                "format": provider.get("format", "mp3"),
                "privacy_note": "Invia audio e testo al provider Voice configurato.",
            }
        )
    return providers


def _ocr_provider_payload(settings: dict | None = None) -> list[dict]:
    settings = settings or SettingsStore(current_app.config.get("SETTINGS_FILE", Config.paths.settings_file)).load()
    providers = []
    for provider in load_builtin_ocr_providers():
        if not provider.get("id"):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url", ""),
                "api_key_env": provider.get("api_key_env", ""),
                "requires_api_key": bool(provider.get("requires_api_key", True)),
                "models": list(provider.get("models", [])),
                "default_model": provider.get("default_model", ""),
                "ocr_mode": provider.get("ocr_mode", "vision_chat"),
                "input_types": list(provider.get("input_types", ["image", "pdf"])),
                "output_format": provider.get("output_format", "text"),
                "supports_layout": bool(provider.get("supports_layout", False)),
                "supports_tables": bool(provider.get("supports_tables", False)),
                "privacy_note": provider.get("privacy_note", ""),
            }
        )
    for provider in settings.get("ocr_providers", []):
        if not provider.get("id") or not provider.get("enabled", True):
            continue
        providers.append(
            {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url", ""),
                "api_key_env": provider.get("api_key_env", ""),
                "requires_api_key": bool(provider.get("requires_api_key", True)),
                "models": list(provider.get("models", [])),
                "default_model": provider.get("default_model", ""),
                "ocr_mode": provider.get("ocr_mode", "vision_chat"),
                "input_types": list(provider.get("input_types", ["image", "pdf"])),
                "output_format": provider.get("output_format", "text"),
                "supports_layout": bool(provider.get("supports_layout", False)),
                "supports_tables": bool(provider.get("supports_tables", False)),
                "privacy_note": "Invia immagini/PDF al provider OCR configurato.",
            }
        )
    return providers


def _split_provider_model_value(value: str | None) -> tuple[str, str]:
    provider_id, separator, model = str(value or "").partition("/")
    return provider_id, model if separator else ""


def _model_configuration_error_response(error: str) -> dict:
    return {
        "error": error,
        "status": "model_configuration_error",
        "provider_config_file": str(default_provider_config_path()),
        "configure_url": "/admin/config",
    }


def _health_status(app: Flask, deep: bool = True, config: dict | None = None) -> dict:
    if config is None:
        try:
            config = _workspace_config(app)
        except Exception:
            config = {
                "SETTINGS_FILE": app.config["SETTINGS_FILE"],
                "FILE_INDEX": app.config["FILE_INDEX"],
                "UPLOAD_FOLDER": app.config["UPLOAD_FOLDER"],
                "CHROMA_COLLECTION": None,
            }
    status = {
        "status": "healthy",
        "model_configuration_ready": False,
        "settings_ready": False,
        "embeddings_ready": False,
        "cache_enabled": False,
        "database_ready": False,
        "tracked_files_count": 0,
        "indexed_files_count": 0,
        "stale_index_files_count": 0,
        "needs_rebuild": False,
        "system_ready": False,
        "stt_ready": False,
        "tts_ready": False,
        "voice_provider": "",
        "ocr_ready": False,
        "ocr_provider": "",
        "state_backend": "memory",
        "queue_backend": "inline",
        "redis_ready": True,
        "queue_ready": True,
        "queue_depth": 0,
        "active_jobs_count": 0,
    }
    state_status = runtime_state_status(active_jobs_count=_active_rebuild_jobs_count())
    status.update(state_status)
    if not state_status["redis_ready"] or not state_status["queue_ready"]:
        status["status"] = "degraded"

    try:
        model_config_error = get_model_configuration_error()
        if model_config_error:
            status["status"] = "unhealthy"
            status["model_configuration_error"] = model_config_error
            status["provider_config_file"] = str(default_provider_config_path())
            return status
        status["model_configuration_ready"] = True

        settings = SettingsStore(config["SETTINGS_FILE"]).load()
        status["settings_ready"] = True
        status["cache_enabled"] = bool(settings["rag"]["enable_cache"])
        from utils.voice_provider import voice_readiness
        from utils.ocr_provider import ocr_readiness

        status.update(voice_readiness(settings))
        status.update(ocr_readiness(settings))
    except Exception as e:
        status["status"] = "unhealthy"
        status["settings_error"] = str(e)
        return status

    try:
        index_status = _index_rebuild_status(app, config=config)
        status["tracked_files_count"] = index_status["tracked_count"]
        status["indexed_files_count"] = index_status["indexed_count"]
        status["stale_index_files_count"] = index_status["stale_count"]
        status["needs_rebuild"] = index_status["needs_rebuild"]
        status["index_profile"] = index_status["current_profile"]
    except Exception as e:
        status["index_error"] = str(e)
        status["status"] = "degraded"

    if deep:
        try:
            from utils.providers.embedding_factory import EmbeddingFactory

            EmbeddingFactory.get_provider()
            status["embeddings_ready"] = True
        except Exception as e:
            status["embeddings_error"] = str(e)
            status["status"] = "degraded"

    try:
        from utils.chroma_manager import get_collection_status

        collection_name = config.get("CHROMA_COLLECTION")
        if collection_name:
            status.update(get_collection_status(collection_name=collection_name))
        else:
            status.update(get_collection_status())
        status["database_ready"] = True
    except Exception as e:
        status["database_error"] = str(e)
        status["status"] = "degraded"

    status["system_ready"] = bool(
        status["model_configuration_ready"]
        and status["settings_ready"]
        and status["database_ready"]
        and not status["needs_rebuild"]
        and int(status.get("documents_count") or 0) > 0
    )

    # Collect memory & metrics snapshot
    from utils.metrics import get_metrics
    metrics = get_metrics()
    metrics.refresh_memory()
    status["uptime_seconds"] = round(metrics.uptime_seconds, 1)
    status["memory_rss_bytes"] = metrics.last_memory_bytes
    status["active_queries"] = metrics.active_queries

    # Query latency summary (from histogram)
    with metrics._query_duration._lock:
        total_queries = metrics._query_duration._count
        total_duration = metrics._query_duration._sum
        average_query_ms = round(total_duration / total_queries * 1000, 1) if total_queries else 0

    status["metrics_snapshot"] = {
        "total_queries": total_queries,
        "average_query_duration_ms": average_query_ms,
        "cache_hit_rate": round(
            metrics._cache_hits.value / max(metrics._cache_hits.value + metrics._cache_misses.value, 1) * 100, 1,
        ),
        "active_queries": metrics.active_queries,
    }

    # Backup status
    try:
        from utils.vector_store.backup_manager import list_backups
        backups = list_backups()
        latest = backups[-1] if backups else None
        status["backup_enabled"] = os.getenv("BACKUP_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        status["backup_available"] = len(backups) > 0
        status["backup_count"] = len(backups)
        if latest:
            status["last_backup_id"] = latest.get("id", "")
            status["last_backup_timestamp"] = latest.get("created_at", "")
            status["last_backup_size_bytes"] = latest.get("size_bytes", 0)
            status["last_backup_documents"] = latest.get("document_count", 0)
        status["backup_retention_days"] = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
        from utils.vector_store.backup_manager import _default_schedule_hours
        status["backup_schedule_hours"] = _default_schedule_hours()

        # Scheduler state
        from utils.vector_store.backup_manager import _scheduler
        if _scheduler:
            status["backup_scheduler_running"] = _scheduler.is_running
        else:
            status["backup_scheduler_running"] = False
    except Exception as e:
        status["backup_status_error"] = str(e)

    return status


def _rate_limit_or_response(rate_limiter: RateLimiter):
    client_ip = request.remote_addr or "unknown"
    allowed, wait_time = rate_limiter.is_allowed(client_ip)
    if allowed:
        return None
    return jsonify(error="Rate limit exceeded", retry_after=wait_time, status="rate_limited"), 429


def _setup_ssl() -> None:
    cert_path = os.getenv("CA_CERT_PATH")
    if cert_path and os.path.exists(cert_path):
        os.environ["SSL_CERT_FILE"] = cert_path
        os.environ["REQUESTS_CA_BUNDLE"] = cert_path


app = create_app()


if __name__ == "__main__":
    import atexit
    from utils.vector_store.backup_manager import stop_scheduler
    atexit.register(stop_scheduler)
    debug = os.getenv("FLASK_DEBUG", "0").lower() in {"1", "true", "yes"}
    log.info("Avvio RAG service...")
    app.run(debug=debug, host=os.getenv("FLASK_HOST", "127.0.0.1"), port=int(os.getenv("FLASK_PORT", "5000")))
