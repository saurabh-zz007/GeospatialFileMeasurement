from typing import Optional
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from src.utils.settings import settings


limiter = Limiter(
    key_func=get_remote_address,
    headers_enabled=True,
    enabled=True,
)


def get_post_rate_limit() -> str:
    """Returns dynamic rate limit string for POST endpoints."""
    return settings.RATE_LIMIT_POST_FILES


def get_get_rate_limit() -> str:
    """Returns dynamic rate limit string for GET endpoints."""
    return settings.RATE_LIMIT_GET_FILES


def is_rate_limit_exempt() -> bool:
    """Exempt callback allowing dynamic toggle via RATE_LIMIT_ENABLED."""
    return not settings.RATE_LIMIT_ENABLED


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """
    Handles rate limit exceeded errors by returning HTTP 429
    with a Retry-After header and a JSON error message.
    """
    msg = f"Rate limit exceeded: {exc.detail}"
    response = JSONResponse(
        status_code=429,
        content={
            "detail": msg,
            "error": msg,
        },
    )
    # Inject standard rate limit headers (including Retry-After)
    view_rate_limit = getattr(request.state, "view_rate_limit", None)
    if view_rate_limit is not None:
        response = request.app.state.limiter._inject_headers(response, view_rate_limit)

    # Ensure Retry-After header is always present
    if "Retry-After" not in response.headers:
        response.headers["Retry-After"] = "60"

    return response
