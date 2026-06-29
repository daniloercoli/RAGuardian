import logging
from typing import Any, Optional

from ..logging_config import PROVIDER_LOGGER


def log_http_error(
    provider: str,
    model: str,
    base_url: Optional[str],
    error: Exception,
    stream: bool = False,
    **extra: Any,
) -> None:
    """Log detailed HTTP error info for debugging provider calls.

    Captures status code, request ID, response body and any extra context
    so that users can troubleshoot network failures without adding their
    own logging.
    """
    status_code = None
    request_id = None
    body: Optional[str] = None

    # OpenAI SDK: APIStatusError / APIConnectionError attributes
    if hasattr(error, "status_code"):
        status_code = error.status_code
    if hasattr(error, "message"):
        body = error.message
    if hasattr(error, "body"):
        body = error.body

    # request id
    if error.__class__.__name__ in {"APIStatusError", "APIConnectionError", "APIResponseError"}:
        if hasattr(error, "request_id"):
            request_id = error.request_id

    PROVIDER_LOGGER.error(
        "HTTP error: provider=%s, model=%s, stream=%s, base_url=%s, "
        "status=%s, request_id=%s, response_body=%s, error=%s",
        provider,
        model,
        stream,
        base_url,
        status_code,
        request_id,
        body,
        error,
        **extra,
    )
