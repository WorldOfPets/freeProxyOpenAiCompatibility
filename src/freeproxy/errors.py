from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse


class ProxyError(Exception):
    """Base error with an OpenAI-compatible representation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        code: str = "internal_error",
        error_type: str = "api_error",
        retry_after: int | None = None,
        headers: Mapping[str, str] | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.error_type = error_type
        self.retry_after = retry_after
        self.headers = dict(headers or {})
        self.provider = provider
        if retry_after is not None:
            self.headers.setdefault("Retry-After", str(max(1, retry_after)))

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "message": self.message,
            "type": self.error_type,
            "code": self.code,
            "param": None,
        }
        if self.provider:
            error["provider"] = self.provider
        return {"error": error}

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=self.payload(),
            headers=self.headers,
        )


class AuthenticationError(ProxyError):
    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(
            message,
            status_code=401,
            code="invalid_api_key",
            error_type="authentication_error",
            headers={"WWW-Authenticate": "Bearer"},
        )


class AdminAuthorizationError(ProxyError):
    def __init__(self, message: str = "Admin access denied") -> None:
        super().__init__(
            message,
            status_code=403,
            code="admin_forbidden",
            error_type="permission_error",
        )


class InvalidRequest(ProxyError):
    def __init__(self, message: str, *, code: str = "invalid_request_error") -> None:
        super().__init__(
            message,
            status_code=400,
            code=code,
            error_type="invalid_request_error",
        )


class PayloadTooLarge(ProxyError):
    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Request body exceeds the {limit} byte limit",
            status_code=413,
            code="payload_too_large",
            error_type="invalid_request_error",
        )


class ModelNotFound(ProxyError):
    def __init__(self, model: str) -> None:
        super().__init__(
            f"Model '{model}' is not available through this gateway",
            status_code=404,
            code="model_not_found",
            error_type="invalid_request_error",
        )


class ProviderNotFound(ProxyError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"Provider '{provider}' is not configured",
            status_code=404,
            code="provider_not_found",
            error_type="invalid_request_error",
        )


class RateLimitExceeded(ProxyError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: int,
        scope: str,
        headers: Mapping[str, str] | None = None,
        provider: str | None = None,
    ) -> None:
        merged = dict(headers or {})
        merged.setdefault("X-RateLimit-Scope", scope)
        super().__init__(
            message,
            status_code=429,
            code="rate_limit_exceeded",
            error_type="rate_limit_error",
            retry_after=retry_after,
            headers=merged,
            provider=provider,
        )
        self.scope = scope


class ProviderUnavailable(ProxyError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=503,
            code="provider_unavailable",
            error_type="service_unavailable_error",
            retry_after=retry_after,
            provider=provider,
        )


class ProviderBusy(ProxyError):
    def __init__(self, provider: str, retry_after: int = 2) -> None:
        super().__init__(
            f"Provider '{provider}' is at its concurrency limit; retry shortly",
            status_code=503,
            code="provider_busy",
            error_type="service_unavailable_error",
            retry_after=retry_after,
            provider=provider,
        )


class GatewayBusy(ProxyError):
    def __init__(self, retry_after: int = 2) -> None:
        super().__init__(
            "The gateway is at its concurrency limit; retry shortly",
            status_code=503,
            code="gateway_busy",
            error_type="service_unavailable_error",
            retry_after=retry_after,
        )


class UpstreamError(ProxyError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        status_code: int = 502,
        code: str = "upstream_error",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            code=code,
            error_type="upstream_error",
            retry_after=retry_after,
            provider=provider,
        )


class ConfigurationError(RuntimeError):
    """Raised when configuration cannot be loaded safely."""
