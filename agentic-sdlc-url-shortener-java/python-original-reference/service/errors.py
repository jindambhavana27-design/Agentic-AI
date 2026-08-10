"""Typed application errors that map cleanly onto HTTP responses.

Every error carries a stable machine-readable ``code`` so clients can branch on
behaviour without string-matching human-readable messages.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self, request_id: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
            }
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class ValidationError(AppError):
    status = 400
    code = "validation_error"


class UnsafeUrlError(ValidationError):
    code = "unsafe_url"


class AuthenticationError(AppError):
    status = 401
    code = "unauthenticated"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class GoneError(AppError):
    """The resource existed but is expired or deleted -- distinct from 404."""

    status = 410
    code = "gone"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class PayloadTooLargeError(AppError):
    status = 413
    code = "payload_too_large"


class RateLimitedError(AppError):
    status = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: float, details=None) -> None:
        super().__init__(message, details)
        self.retry_after = retry_after


class StorageError(AppError):
    status = 503
    code = "storage_unavailable"


class CodeExhaustionError(AppError):
    """Raised when random code generation could not find a free slot."""

    status = 503
    code = "code_generation_failed"
