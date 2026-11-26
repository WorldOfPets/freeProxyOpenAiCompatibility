from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from ..config import ProviderConfig
from ..errors import ProviderBusy, ProviderUnavailable, RateLimitExceeded
from ..metrics import Metrics
from ..rate_limit import DailyQuota, SlidingWindowLimiter
from .http import HealthResult, ProviderHTTP

logger = logging.getLogger("freeproxy.provider")


class _CircuitBreaker:
    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_status: int | None = None
        self.last_error = ""

    def ensure_closed(self, provider: str) -> None:
        remaining = self.open_until - time.monotonic()
        if remaining > 0:
            retry_after = max(1, math.ceil(remaining))
            if self.last_status == 429:
                raise RateLimitExceeded(
                    f"Provider '{provider}' is cooling down after an upstream rate limit",
                    retry_after=retry_after,
                    scope=f"provider:{provider}",
                    headers={"X-Provider": provider},
                    provider=provider,
                )
            raise ProviderUnavailable(
                f"Provider '{provider}' is cooling down after an upstream failure",
                provider=provider,
                retry_after=retry_after,
            )

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_status = 200
        self.last_error = ""

    def record_failure(
        self,
        provider: str,
        *,
        status_code: int | None,
        retry_after: int | None = None,
        detail: str = "",
    ) -> None:
        self.last_status = status_code
        self.last_error = detail or (f"HTTP {status_code}" if status_code else "network error")
        now = time.monotonic()

        if status_code in {401, 403}:
            self.open_until = max(self.open_until, now + 60)
            return
        if status_code == 429:
            self.open_until = max(self.open_until, now + (retry_after or 30))
            return
        if status_code is None or (status_code is not None and status_code >= 500):
            self.failures += 1
            if self.failures >= 3:
                cooldown = min(15 * (2 ** (self.failures - 3)), 300)
                self.open_until = max(self.open_until, now + cooldown)

    def status(self) -> dict[str, Any]:
        remaining = max(0, math.ceil(self.open_until - time.monotonic()))
        return {
            "open": remaining > 0,
            "open_for_seconds": remaining,
            "consecutive_failures": self.failures,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }

    def reset(self) -> None:
        self.failures = 0
        self.open_until = 0.0
        self.last_status = None
        self.last_error = ""


@dataclass
class ProviderSlot:
    runtime: ProviderRuntime
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.runtime._release_slot()


class ProviderRuntime:
    """Live state for one configured upstream."""

    def __init__(
        self,
        config: ProviderConfig,
        http: ProviderHTTP,
        metrics: Metrics,
        *,
        health_timeout_seconds: float = 5.0,
    ) -> None:
        self.config = config
        self.http = http
        self.metrics = metrics
        self.rpm = SlidingWindowLimiter(config.max_rpm)
        self.daily = DailyQuota(config.max_requests_per_day)
        self.breaker = _CircuitBreaker()
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._active_requests = 0
        self._discovered_models: set[str] = set()
        self._health_checked_at = 0.0
        self._health = HealthResult(ok=False, status_code=None, latency_ms=0, detail="not checked")
        self._health_lock = asyncio.Lock()
        self._health_timeout = health_timeout_seconds
        self._last_discovery_error = ""

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def known_models(self) -> list[str]:
        return sorted(set(self.config.models) | self._discovered_models)

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def _rate_limit_error(self) -> RateLimitExceeded:
        rpm_preview = self.rpm.preview(self.id)
        daily_preview = self.daily.preview(self.id)
        self.metrics.record_provider_rate_limit(self.id)
        if not rpm_preview.allowed:
            return RateLimitExceeded(
                f"Provider '{self.id}' rate limit exceeded ({self.config.max_rpm} requests/minute)",
                retry_after=rpm_preview.retry_after,
                scope=f"provider:{self.id}",
                headers={
                    "X-RateLimit-Provider-Limit": str(self.config.max_rpm),
                    "X-RateLimit-Provider-Remaining": "0",
                    "X-RateLimit-Provider-Reset": str(rpm_preview.reset_at),
                    "X-Provider": self.id,
                },
                provider=self.id,
            )
        return RateLimitExceeded(
            f"Provider '{self.id}' daily quota exceeded "
            f"({self.config.max_requests_per_day} requests/day)",
            retry_after=daily_preview.retry_after,
            scope=f"provider:{self.id}:daily",
            headers={"X-Provider": self.id},
            provider=self.id,
        )

    async def acquire(self) -> ProviderSlot:
        self.breaker.ensure_closed(self.id)

        # Fast-fail before waiting for a concurrency slot. The counters are
        # consumed only after the slot is acquired, so a queue timeout does not
        # burn an upstream request.
        rpm_preview = self.rpm.preview(self.id)
        daily_preview = self.daily.preview(self.id)
        if not rpm_preview.allowed or not daily_preview.allowed:
            raise self._rate_limit_error()

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.config.queue_timeout_seconds,
            )
        except TimeoutError:
            raise ProviderBusy(self.id, retry_after=2) from None

        try:
            # The breaker or limits may have changed while queued. Preview and
            # consume both limits without an await in between.
            self.breaker.ensure_closed(self.id)
            rpm_preview = self.rpm.preview(self.id)
            daily_preview = self.daily.preview(self.id)
            if not rpm_preview.allowed or not daily_preview.allowed:
                raise self._rate_limit_error()
            rpm = self.rpm.consume(self.id)
            daily = self.daily.consume(self.id)
            if not (rpm.allowed and daily.allowed):
                raise self._rate_limit_error()
        except BaseException:
            self._semaphore.release()
            raise

        self._active_requests += 1
        return ProviderSlot(self)

    def _release_slot(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)
        self._semaphore.release()

    def record_success(self) -> None:
        self.breaker.record_success()

    def record_failure(
        self,
        *,
        status_code: int | None,
        retry_after: int | None = None,
        detail: str = "",
    ) -> None:
        self.breaker.record_failure(
            self.id,
            status_code=status_code,
            retry_after=retry_after,
            detail=detail,
        )

    async def refresh_models(self, timeout_seconds: float) -> list[str]:
        try:
            models = await self.http.list_models(timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - discovery must never break serving
            self._last_discovery_error = type(exc).__name__
            logger.warning(
                "model discovery failed",
                extra={"provider": self.id, "route": type(exc).__name__},
            )
            return []
        if models:
            self._discovered_models = set(models)
            self._last_discovery_error = ""
        return models

    async def check_health(self, *, force: bool = False) -> HealthResult:
        async with self._health_lock:
            now = time.monotonic()
            if not force and now - self._health_checked_at < 5.0:
                return self._health
            self._health = await self.http.health(self._health_timeout)
            self._health_checked_at = time.monotonic()
            return self._health

    def used_rpm(self) -> int:
        return self.rpm.used(self.id)

    def used_daily(self) -> int:
        today = self.daily._today()  # noqa: SLF001 - same-package introspection for status
        entry_date, count = self.daily._entries.get(self.id, (today, 0))  # noqa: SLF001
        return count if entry_date == today else 0

    def status(self) -> dict[str, Any]:
        rpm_used = self.used_rpm()
        daily_used = self.used_daily()
        return {
            "id": self.config.id,
            "name": self.config.name,
            "enabled": self.config.enabled,
            "base_url": self.config.base_url,
            "prefix": self.config.route_prefix,
            "auth_mode": self.config.auth_mode,
            "models": self.known_models,
            "default_model": self.config.default_model,
            "supports_tools": self.config.supports_tools,
            "health": {
                "ok": self._health.ok,
                "status_code": self._health.status_code,
                "latency_ms": self._health.latency_ms,
                "detail": self._health.detail,
            },
            "breaker": self.breaker.status(),
            "rpm": {
                "limit": self.config.max_rpm,
                "used": rpm_used,
                "remaining": max(0, self.config.max_rpm - rpm_used),
            },
            "daily": {
                "limit": self.config.max_requests_per_day,
                "used": daily_used,
                "remaining": (
                    max(0, self.config.max_requests_per_day - daily_used)
                    if self.config.max_requests_per_day
                    else None
                ),
            },
            "concurrency": {
                "limit": self.config.max_concurrency,
                "active": self._active_requests,
            },
            "last_discovery_error": self._last_discovery_error,
        }

    def reset(self) -> None:
        self.breaker.reset()
        self.rpm.reset()
        self.daily.reset()
