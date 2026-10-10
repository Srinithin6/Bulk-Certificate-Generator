"""Application errors and the single JSON error format used by every endpoint:

{"error": {"code": "not_found", "message": "...", "details": [...]}}
"""

from typing import Any


class AppError(Exception):
    """An expected error that is safe to show to the client."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code, code = 404, "not_found"


class ConflictError(AppError):
    status_code, code = 409, "conflict"


class GoneError(AppError):
    status_code, code = 410, "gone"


class UnauthorizedError(AppError):
    status_code, code = 401, "unauthorized"


class PayloadTooLargeError(AppError):
    status_code, code = 413, "payload_too_large"


class GenerationError(Exception):
    """Raised while rendering a certificate for an *expected* reason. The message is stored
    on the certificate and shown to the client, so it must never contain internal details."""


def error_body(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}
