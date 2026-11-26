from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request

from ..errors import AdminAuthorizationError, AuthenticationError, RateLimitExceeded
from ..rate_limit import ClientLimitResult


@dataclass(frozen=True)
class ClientIdentity:
    id: str
    label: str
    anonymous: bool


def _tokens_from_headers(request: Request) -> list[str]:
    tokens: list[str] = []
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            tokens.append(value.strip())
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key and api_key not in tokens:
        tokens.append(api_key)
    return tokens


def _identity_from_token(token: str, *, anonymous: bool) -> ClientIdentity:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    label = "anonymous" if anonymous else f"key:{digest[:8]}"
    return ClientIdentity(id=digest[:16], label=label, anonymous=anonymous)


async def authenticate(request: Request) -> ClientIdentity:
    settings = request.app.state.settings
    configured = settings.api_key_list
    tokens = _tokens_from_headers(request)
    if any(len(token) > 4096 for token in tokens):
        raise AuthenticationError()

    if configured:
        for token in tokens:
            for expected in configured:
                if secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
                    return _identity_from_token(token, anonymous=False)
        raise AuthenticationError()

    if not settings.allow_anonymous:
        raise AuthenticationError("API key required")

    # Anonymous identity is based on the peer address, never on a client-chosen
    # token: otherwise rotating Authorization headers would bypass per-client
    # limits. The global limit still applies behind a reverse proxy.
    client_host = request.client.host if request.client else "unknown"
    return _identity_from_token(f"anonymous:{client_host}", anonymous=True)


async def enforce_client_limits(
    request: Request,
    identity: Annotated[ClientIdentity, Depends(authenticate)],
) -> ClientLimitResult:
    limiter = request.app.state.client_limiter
    try:
        result = limiter.check(identity.id)
    except RateLimitExceeded:
        request.app.state.metrics.record_client_rate_limit()
        raise
    request.state.rate_limit_headers = result.headers()
    return result


async def enforce_client_concurrency(
    request: Request,
    identity: Annotated[ClientIdentity, Depends(authenticate)],
) -> AsyncIterator[None]:
    limiter = request.app.state.client_limiter
    limiter.acquire_concurrency(identity.id)
    try:
        yield
    finally:
        limiter.release_concurrency(identity.id)


async def require_admin(request: Request) -> None:
    settings = request.app.state.settings
    if settings.admin_key:
        candidates = _tokens_from_headers(request)
        admin_header = request.headers.get("x-admin-key", "").strip()
        if admin_header:
            candidates.append(admin_header)
        if not any(
            secrets.compare_digest(candidate.encode("utf-8"), settings.admin_key.encode("utf-8"))
            for candidate in candidates
        ):
            raise AdminAuthorizationError()
        return

    forwarded = request.headers.get("forwarded") or request.headers.get("x-forwarded-for")
    peer = request.client.host if request.client else ""
    host_header = request.headers.get("host", "")
    host_name = urlsplit(f"//{host_header}").hostname or ""
    if (
        forwarded
        or peer not in {"127.0.0.1", "::1", "localhost", "testclient"}
        or host_name not in {"127.0.0.1", "::1", "localhost", "testclient"}
    ):
        raise AdminAuthorizationError(
            "Admin endpoints are loopback-only when FP_ADMIN_API_KEY is not set; "
            "set FP_ADMIN_API_KEY when using a reverse proxy or custom hostname"
        )
    # A browser cannot send this header on a simple cross-site request without
    # a CORS preflight, which blocks loopback CSRF.
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(
        "x-freeproxy-admin", ""
    ).strip().lower() not in {"1", "true", "yes"}:
        raise AdminAuthorizationError(
            "State-changing admin requests require FP_ADMIN_API_KEY or X-FreeProxy-Admin: true"
        )
