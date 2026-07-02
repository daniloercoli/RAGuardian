import json
import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class LogLevel:
    DEBUG = logging.DEBUG
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


_SECRET_FIELD_RE = re.compile(r"(api[_-]?key|x-api-key|authorization|bearer|token|secret|password)", re.I)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<label>api[_-]?key|x-api-key|authorization|bearer|token|secret|password)"
    r"(?P<sep>\s*[:=]\s*|\s+)"
    r"(?P<value>[^\s,;\"']{9,})",
    re.I,
)
_BEARER_RE = re.compile(r"\b(?P<label>Bearer\s+)(?P<value>[^\s,;\"']{9,})", re.I)


def mask_secret_for_log(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


def sanitize_log_text(value: object) -> str:
    text = str(value)

    def repl(match: re.Match) -> str:
        return f"{match.group('label')}{match.group('sep')}{mask_secret_for_log(match.group('value'))}"

    text = _BEARER_RE.sub(
        lambda match: f"{match.group('label')}{mask_secret_for_log(match.group('value'))}",
        text,
    )
    return _SECRET_ASSIGNMENT_RE.sub(repl, text)


def sanitize_log_value(key: str, value):
    if isinstance(value, dict):
        return {name: sanitize_log_value(str(name), item) for name, item in value.items()}
    if isinstance(value, list):
        return [sanitize_log_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_log_value(key, item) for item in value)
    if isinstance(value, str):
        if _SECRET_FIELD_RE.search(str(key)):
            return mask_secret_for_log(value)
        return sanitize_log_text(value)
    return value


class SanitizingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_args = record.args
        record.msg = sanitize_log_text(record.getMessage())
        record.args = ()
        try:
            return super().format(record)
        finally:
            record.msg = original_msg
            record.args = original_args


class JsonLogFormatter(logging.Formatter):
    """Structured JSON formatter for file logs.

    Each log line is a single JSON object with:
      - timestamp (ISO 8601)
      - level
      - logger (name)
      - message
      - exception (optional, traceback string)
      - any extra fields passed via extra={}
    """

    _STANDARD_ATTRS = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "funcName", "lineno", "file", "msecs", "relativeCreated",
        "thread", "threadName", "process", "processName", "message",
        "exc_info", "exc_text", "stack_info", "created", "taskName",
        "processName",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_log_text(record.getMessage()),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Append extra fields (not standard LogRecord attrs)
        for key, value in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in self._STANDARD_ATTRS:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                log_data[key] = sanitize_log_value(key, value)

        # Exception info
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)

        # Stack info
        if record.stack_info:
            log_data["stack_info"] = record.stack_info

        return json.dumps(log_data, default=str, ensure_ascii=False)


def setup_logger(
    name: str = "rag_service",
    level: int | None = None,
) -> logging.Logger:
    """Configure and return an idempotent logger for console and file output.

    Console logs always use human-readable format.
    File logs use JSON structured format when LOG_FORMAT=json (or LOG_FORMAT=1).
    """
    effective_level = level if level is not None else _env_log_level()
    logger = logging.getLogger(name)
    logger.setLevel(effective_level)

    if _console_logging_enabled() and not _has_handler(logger, "console"):
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(effective_level)
        console_handler.setFormatter(_human_formatter("%H:%M"))
        console_handler._rag_handler_kind = "console"
        logger.addHandler(console_handler)

    if _file_logging_enabled() and not _has_handler(logger, "file"):
        log_dir = Path(os.getenv("LOG_DIR", "app/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / os.getenv("LOG_FILE", "rag_service.log"),
            maxBytes=_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
            backupCount=_env_int("LOG_BACKUP_COUNT", 10),
            encoding="utf-8",
        )
        file_handler.setLevel(effective_level)
        if _json_logging_enabled():
            file_handler.setFormatter(
                JsonLogFormatter(datefmt="%Y-%m-%dT%H:%M:%S")
            )
        else:
            file_handler.setFormatter(_human_formatter("%Y-%m-%d %H:%M:%S"))
        file_handler._rag_handler_kind = "file"
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def configure_external_loggers() -> None:
    """Route framework loggers through the same persistent logging setup."""
    setup_logger("werkzeug")


def _has_handler(logger: logging.Logger, kind: str) -> bool:
    return any(
        getattr(handler, "_rag_handler_kind", "") == kind
        for handler in logger.handlers
    )


def _human_formatter(datefmt: str) -> logging.Formatter:
    return SanitizingFormatter(
        fmt="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt=datefmt,
    )


def _json_logging_enabled() -> bool:
    """Return True if LOG_FORMAT is set to json (or '1', 'true', etc.)."""
    return os.getenv("LOG_FORMAT", "text").strip().lower() in {
        "1", "true", "yes", "on", "json",
    }


def _env_log_level() -> int:
    configured = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, configured, logging.INFO)


def _file_logging_enabled() -> bool:
    return os.getenv("ENABLE_FILE_LOG", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _console_logging_enabled() -> bool:
    return os.getenv("LOG_TO_CONSOLE", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


CHROMA_LOGGER = setup_logger("rag_service.chroma")
PDF_LOGGER = setup_logger("rag_service.pdf")
RAG_LOGGER = setup_logger("rag_service.rag")
APP_LOGGER = setup_logger("rag_service.app")
PROVIDER_LOGGER = setup_logger("rag_service.provider")
EMBEDDING_LOGGER = setup_logger("rag_service.embedding")
RERANKER_LOGGER = setup_logger("rag_service.reranker")
