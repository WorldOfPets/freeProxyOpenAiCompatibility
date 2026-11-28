from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import admin, chat, health, models
from .config import ProxyConfig, Settings, load_proxy_config
from .errors import ConfigurationError, ProxyError
from .logging import configure_logging
from .metrics import Metrics
from .middleware import RequestContextMiddleware
from .native.deepseek import create_deepseek_app
from .native.glm import create_glm_app
from .native.qwen import create_qwen_app
from .providers.base import ProviderRuntime
from .providers.http import ProviderHTTP
from .rate_limit import ClientLimiter
from .services.proxy import ProxyService
from .services.routing import ModelRouter

logger = logging.getLogger("freeproxy.lifecycle")


async def _discover_loop(
    runtimes: dict[str, ProviderRuntime],
    ttl_seconds: int,
    timeout_seconds: float,
) -> None:
    while True:
        await asyncio.gather(
            *(runtime.refresh_models(timeout_seconds) for runtime in runtimes.values()),
            return_exceptions=True,
        )
        await asyncio.sleep(ttl_seconds)


def _build_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(
            30.0,
            connect=settings.request_connect_timeout_seconds,
            write=30.0,
            pool=10.0,
        ),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        follow_redirects=False,
        trust_env=False,
    )


def create_app(
    settings: Settings | None = None,
    proxy_config: ProxyConfig | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    native_http_transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    native_transports: dict[str, Any] | None = None,
    start_discovery: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    proxy_config = proxy_config or load_proxy_config(settings.providers_file)
    if settings.default_alias not in proxy_config.aliases:
        available = ", ".join(sorted(proxy_config.aliases)) or "none"
        raise ConfigurationError(
            f"FP_DEFAULT_ALIAS='{settings.default_alias}' is not defined in "
            f"providers config aliases (available: {available})"
        )
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = _build_client(settings, http_transport)
        native_clients: list[httpx.AsyncClient] = []
        native_closers: list[Any] = []
        metrics = Metrics(started_at=time.time())
        runtimes: dict[str, ProviderRuntime] = {}
        for provider in proxy_config.providers:
            provider_client = client
            if provider.native is not None:
                if provider.native == "deepseek":
                    native_app = create_deepseek_app(
                        settings.auth_store_path,
                        transport=(native_http_transports or {}).get("deepseek"),
                    )
                elif provider.native == "qwen":
                    native_app = create_qwen_app(
                        settings.auth_store_path,
                        transport=(native_transports or {}).get("qwen"),
                        headless=settings.qwen_headless,
                        model_ttl_seconds=settings.model_discovery_ttl_seconds,
                    )
                elif provider.native == "glm":
                    native_app = create_glm_app(
                        settings.auth_store_path,
                        transport=(native_transports or {}).get("glm"),
                        http_transport=(native_http_transports or {}).get("glm"),
                        captcha_access_key=(settings.aliyun_captcha_access_key.get_secret_value()),
                        captcha_secret_key=(settings.aliyun_captcha_secret_key.get_secret_value()),
                        tokens_db=settings.glm_tokens_db,
                    )
                else:
                    raise ConfigurationError(
                        f"Native provider '{provider.native}' is not implemented"
                    )
                provider_client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=native_app),
                    base_url=provider.base_url,
                    trust_env=False,
                )
                native_clients.append(provider_client)
                closer = getattr(native_app.state, "close", None)
                if closer is not None:
                    native_closers.append(closer)
            http = ProviderHTTP(provider_client, provider)
            runtimes[provider.id] = ProviderRuntime(
                provider,
                http,
                metrics,
                health_timeout_seconds=settings.model_discovery_timeout_seconds,
            )

        router = ModelRouter(
            proxy_config,
            runtimes,
            default_alias=settings.default_alias,
            strict_models=settings.strict_models,
        )
        client_limiter = ClientLimiter(
            client_rpm=settings.client_rpm,
            daily_requests=settings.client_daily_requests,
            global_rpm=settings.global_rpm,
            max_concurrency=settings.client_max_concurrency,
        )
        proxy = ProxyService(settings, router, runtimes, metrics)

        app.state.settings = settings
        app.state.proxy_config = proxy_config
        app.state.metrics = metrics
        app.state.runtimes = runtimes
        app.state.router = router
        app.state.client_limiter = client_limiter
        app.state.proxy = proxy
        app.state.started_at = time.time()

        discovery_task: asyncio.Task[None] | None = None
        if start_discovery:
            discovery_task = asyncio.create_task(
                _discover_loop(
                    runtimes,
                    settings.model_discovery_ttl_seconds,
                    settings.model_discovery_timeout_seconds,
                )
            )

        logger.info(
            "gateway started with providers: %s",
            ", ".join(
                f"{provider.id}({provider.route_prefix})"
                for provider in proxy_config.providers
                if provider.enabled
            ),
        )
        try:
            yield
        finally:
            if discovery_task is not None:
                discovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await discovery_task
            for closer in native_closers:
                with suppress(Exception):
                    await closer()
            for native_client in native_clients:
                await native_client.aclose()
            await client.aclose()
            logger.info("gateway stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Unified OpenAI-compatible gateway for the GLM, Qwen and DeepSeek "
            "web-chat proxies. Client keys, model routing, aliases and "
            "rate limits are enforced here."
        ),
        lifespan=lifespan,
    )

    # Available to early error handlers even before lifespan completes.
    app.state.settings = settings
    app.state.proxy_config = proxy_config
    app.state.started_at = time.time()

    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-API-Key",
                "X-Admin-Key",
                "X-FreeProxy-Provider",
                "X-FreeProxy-Session",
                "X-Agent-Session",
                "X-Request-ID",
            ],
            expose_headers=[
                "X-Request-ID",
                "X-FreeProxy-Provider",
                "X-FreeProxy-Model",
                "X-FreeProxy-Route",
                "X-FreeProxy-Requested-Model",
                "X-FreeProxy-Fallback",
                "X-FreeProxy-Upstream-Status",
                "X-FreeProxy-Attempts",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "X-RateLimit-Policy",
                "X-RateLimit-Scope",
                "X-RateLimit-Daily-Limit",
                "X-RateLimit-Daily-Remaining",
                "X-RateLimit-Daily-Reset",
                "X-RateLimit-Global-Limit",
                "X-RateLimit-Global-Remaining",
                "X-RateLimit-Global-Reset",
                "X-RateLimit-Provider-Limit",
                "X-RateLimit-Provider-Remaining",
                "X-RateLimit-Provider-Reset",
                "X-Provider",
                "Retry-After",
            ],
        )

    @app.exception_handler(ProxyError)
    async def proxy_error_handler(request: Request, exc: ProxyError) -> JSONResponse:
        headers = dict(exc.headers)
        rate_headers = getattr(request.state, "rate_limit_headers", None)
        if isinstance(rate_headers, dict):
            for name, value in rate_headers.items():
                headers.setdefault(name, value)
        if exc.status_code >= 500:
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None:
                metrics.gateway_errors += 1
        logger.warning(
            "proxy error",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "provider": exc.provider,
                "status_code": exc.status_code,
                "route": getattr(request.state, "route", None),
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.payload(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": "Request validation failed",
                    "type": "invalid_request_error",
                    "code": "validation_error",
                    "param": None,
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        error_type = "invalid_request_error" if 400 <= exc.status_code < 500 else "api_error"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": str(exc.detail),
                    "type": error_type,
                    "code": code,
                    "param": None,
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled error",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "An unexpected error occurred",
                    "type": "api_error",
                    "code": "internal_error",
                    "param": None,
                }
            },
        )

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(admin.router)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {}).update(
            {
                "BearerAuth": {"type": "http", "scheme": "bearer"},
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                },
                "AdminKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Admin-Key",
                },
            }
        )
        client_security = [{"BearerAuth": []}, {"ApiKeyAuth": []}]
        admin_security = [
            {"BearerAuth": []},
            {"ApiKeyAuth": []},
            {"AdminKeyAuth": []},
        ]
        for path, operations in schema.get("paths", {}).items():
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                if path.startswith("/admin"):
                    operation["security"] = admin_security
                elif path.startswith("/v1") or path == "/models":
                    operation["security"] = client_security
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def main() -> None:
    try:
        settings = Settings()
    except (ValidationError, ConfigurationError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    uvicorn.run(
        "freeproxy.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
        proxy_headers=False,
        limit_concurrency=settings.server_limit_concurrency,
        timeout_graceful_shutdown=int(settings.shutdown_timeout_seconds),
    )


if __name__ == "__main__":
    main()
