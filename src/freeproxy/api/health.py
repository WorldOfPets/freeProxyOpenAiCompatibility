from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "service": settings.app_name,
        "version": settings.version,
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
        "ready": "/readyz",
        "models": "/v1/models",
        "chat": "/v1/chat/completions",
    }


@router.get("/health", tags=["Operations"], summary="Liveness probe")
async def health(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {
        "status": "ok",
        "service": state.settings.app_name,
        "version": state.settings.version,
        "environment": state.settings.environment,
        "uptime_seconds": round(time.time() - state.started_at, 2),
        "providers": len(state.runtimes),
    }


@router.get("/readyz", tags=["Operations"], summary="Readiness probe for upstreams")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    runtimes = [runtime for runtime in state.runtimes.values() if runtime.config.enabled]
    results = await asyncio.gather(
        *(runtime.check_health() for runtime in runtimes),
        return_exceptions=True,
    )
    providers: dict[str, Any] = {}
    ready = False
    for runtime, result in zip(runtimes, results, strict=True):
        if isinstance(result, BaseException):
            providers[runtime.id] = {"ok": False}
            continue
        # Keep the public probe aggregate-only: upstream status/mode details
        # for account-backed bridges belong behind /admin.
        providers[runtime.id] = {"ok": result.ok}
        ready = ready or result.ok
    status_code = 200 if ready else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if ready else "not_ready",
            "providers": providers,
        },
    )
