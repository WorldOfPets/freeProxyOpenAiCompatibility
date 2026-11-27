from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..auth.exporters import export_all
from ..auth.paths import AuthPaths
from ..auth.store import TokenStore
from ..errors import ProviderNotFound
from .dependencies import require_admin

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])


@router.get("/status")
async def admin_status(request: Request) -> dict[str, Any]:
    state = request.app.state
    settings = state.settings
    return {
        "service": settings.app_name,
        "version": settings.version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - state.started_at, 2),
        "default_alias": settings.default_alias,
        "strict_models": settings.strict_models,
        "aliases": state.proxy_config.aliases,
        "client_limits": {
            "rpm": settings.client_rpm,
            "daily_requests": settings.client_daily_requests,
            "global_rpm": settings.global_rpm,
            "global_max_concurrency": settings.global_max_concurrency,
        },
        "generation_limits": {
            "max_body_bytes": settings.max_body_bytes,
            "max_output_tokens": settings.max_output_tokens,
            "default_max_tokens": settings.default_max_tokens,
        },
        "providers": [runtime.status() for runtime in state.runtimes.values()],
    }


@router.get("/providers")
async def admin_providers(request: Request) -> dict[str, Any]:
    return {"providers": [runtime.status() for runtime in request.app.state.runtimes.values()]}


@router.get("/stats")
async def admin_stats(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {
        "uptime_seconds": round(time.time() - state.started_at, 2),
        **state.metrics.snapshot(),
    }


@router.get("/limits")
async def admin_limits(request: Request) -> dict[str, Any]:
    return request.app.state.client_limiter.snapshot()


@router.get("/auth")
async def admin_auth() -> dict[str, Any]:
    paths = await asyncio.to_thread(AuthPaths.from_env)
    store = TokenStore(paths.store)
    providers = await asyncio.to_thread(store.status)
    return {
        "store": str(paths.store),
        "providers": providers,
        "repos": {
            "deepseek": str(paths.deepseek_repo),
            "qwen": str(paths.qwen_repo),
            "glm": str(paths.glm_repo),
        },
    }


@router.post("/auth/export")
async def admin_auth_export() -> dict[str, Any]:
    paths = await asyncio.to_thread(AuthPaths.from_env)
    store = TokenStore(paths.store)
    exported = await asyncio.to_thread(export_all, store, paths)
    return {"exported": exported}


@router.post("/providers/{provider_id}/reset")
async def reset_provider(request: Request, provider_id: str) -> dict[str, Any]:
    runtime = request.app.state.runtimes.get(provider_id)
    if runtime is None:
        raise ProviderNotFound(provider_id)
    runtime.reset()
    return {"ok": True, "provider": provider_id, "reset": ["breaker", "rpm", "daily"]}


@router.post("/limits/reset")
async def reset_limits(request: Request) -> dict[str, Any]:
    request.app.state.client_limiter.reset()
    return {"ok": True, "reset": ["client_rpm", "client_daily", "global_rpm"]}
