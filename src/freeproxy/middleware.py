from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("freeproxy.request")


def _safe_request_id(raw: str | None) -> str:
    if raw:
        cleaned = "".join(ch for ch in raw if ch.isascii() and (ch.isalnum() or ch in "._:-"))
        if cleaned:
            return cleaned[:128]
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Adds a request ID, security headers and one structured access log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state: dict[str, Any] = scope.setdefault("state", {})
        request_id = _safe_request_id(
            next(
                (
                    value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                    if key.lower() == b"x-request-id"
                ),
                None,
            )
        )
        state["request_id"] = request_id
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                if "x-request-id" not in headers:
                    headers["x-request-id"] = request_id
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "no-referrer")
                headers.setdefault("x-frame-options", "DENY")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            path = scope.get("path", "")
            client = scope.get("client")
            client_host = client[0] if client else "unknown"
            extra = {
                "request_id": request_id,
                "client": client_host,
                "method": scope.get("method", ""),
                "path": path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "provider": state.get("provider"),
                "model": state.get("model"),
                "route": state.get("route"),
            }
            level = logging.DEBUG if path in {"/health", "/readyz"} else logging.INFO
            logger.log(
                level,
                "request",
                extra={key: value for key, value in extra.items() if value is not None},
            )
