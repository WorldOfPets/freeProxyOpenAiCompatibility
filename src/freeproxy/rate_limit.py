from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time

from .errors import RateLimitExceeded


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_at: int
    retry_after: int = 0
    enabled: bool = True


@dataclass(frozen=True)
class ClientLimitResult:
    rpm: LimitDecision
    daily: LimitDecision
    global_rpm: LimitDecision

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.rpm.limit),
            "X-RateLimit-Remaining": str(self.rpm.remaining),
            "X-RateLimit-Reset": str(self.rpm.reset_at),
            "X-RateLimit-Policy": f"{self.rpm.limit};w=60",
        }
        if self.daily.enabled:
            headers.update(
                {
                    "X-RateLimit-Daily-Limit": str(self.daily.limit),
                    "X-RateLimit-Daily-Remaining": str(self.daily.remaining),
                    "X-RateLimit-Daily-Reset": str(self.daily.reset_at),
                }
            )
        headers.update(
            {
                "X-RateLimit-Global-Limit": str(self.global_rpm.limit),
                "X-RateLimit-Global-Remaining": str(self.global_rpm.remaining),
                "X-RateLimit-Global-Reset": str(self.global_rpm.reset_at),
            }
        )
        return headers


class SlidingWindowLimiter:
    """In-process sliding-window limiter.

    Only one event loop is expected to mutate the limiter. Methods are
    synchronous so a check and its following consume cannot be interleaved by
    another coroutine.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._events: dict[str, deque[float]] = {}
        self._ops = 0

    def _prune(self, key: str, now: float, *, mutate: bool) -> deque[float]:
        events = self._events.setdefault(key, deque())
        cutoff = now - self.window_seconds
        if mutate:
            while events and events[0] <= cutoff:
                events.popleft()
        else:
            # Preview must not mutate the deque.
            return deque(event for event in events if event > cutoff)
        return events

    def preview(self, key: str, cost: int = 1) -> LimitDecision:
        now = self._monotonic()
        events = self._prune(key, now, mutate=False)
        allowed = len(events) + cost <= self.limit
        remaining = max(0, self.limit - len(events) - cost)
        oldest = events[0] if events else None
        if oldest is None:
            retry_after = 0 if allowed else 1
            reset_in = self.window_seconds
        else:
            reset_in = max(0.0, oldest + self.window_seconds - now)
            retry_after = 0 if allowed else max(1, math.ceil(reset_in))
        return LimitDecision(
            allowed=allowed,
            limit=self.limit,
            remaining=remaining,
            reset_at=math.floor(self._wall_clock() + reset_in),
            retry_after=retry_after,
        )

    def consume(self, key: str, cost: int = 1) -> LimitDecision:
        now = self._monotonic()
        self._ops += 1
        if self._ops % 512 == 0:
            self._prune_all(now)
        events = self._prune(key, now, mutate=True)
        if len(events) + cost > self.limit:
            decision = self.preview(key, cost)
            return LimitDecision(
                allowed=False,
                limit=decision.limit,
                remaining=decision.remaining,
                reset_at=decision.reset_at,
                retry_after=decision.retry_after,
            )
        for _ in range(cost):
            events.append(now)
        reset_in = max(0.0, events[0] + self.window_seconds - now)
        return LimitDecision(
            allowed=True,
            limit=self.limit,
            remaining=max(0, self.limit - len(events)),
            reset_at=math.floor(self._wall_clock() + reset_in),
            retry_after=0,
        )

    def _prune_all(self, now: float) -> None:
        cutoff = now - self.window_seconds
        empty: list[str] = []
        for key, events in self._events.items():
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                empty.append(key)
        for key in empty:
            self._events.pop(key, None)

    def used(self, key: str) -> int:
        now = self._monotonic()
        events = self._prune(key, now, mutate=False)
        return len(events)

    def snapshot(self, limit: int = 50) -> list[dict[str, int | str]]:
        now = self._monotonic()
        rows: list[dict[str, int | str]] = []
        for key in self._events:
            used = len(self._prune(key, now, mutate=False))
            if used:
                rows.append({"key": key, "used": used, "limit": self.limit})
        rows.sort(key=lambda row: int(row["used"]), reverse=True)
        return rows[:limit]

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)


class DailyQuota:
    """Per-key UTC-day request quota."""

    def __init__(
        self,
        limit: int,
        *,
        wall_clock: Callable[[], float] = time.time,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.limit = max(0, limit)
        self._wall_clock = wall_clock
        self._today = today or (lambda: datetime.now(UTC).date())
        self._entries: dict[str, tuple[date, int]] = {}

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _current(self, key: str) -> tuple[date, int]:
        today = self._today()
        current_date, count = self._entries.get(key, (today, 0))
        if current_date != today:
            return today, 0
        return current_date, count

    def _reset_at(self) -> int:
        today = self._today()
        tomorrow = datetime.combine(today + timedelta(days=1), datetime_time.min, tzinfo=UTC)
        return int(tomorrow.timestamp())

    def preview(self, key: str) -> LimitDecision:
        if not self.enabled:
            return LimitDecision(
                allowed=True,
                limit=0,
                remaining=0,
                reset_at=0,
                enabled=False,
            )
        _current_date, count = self._current(key)
        allowed = count < self.limit
        return LimitDecision(
            allowed=allowed,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            reset_at=self._reset_at(),
            retry_after=0 if allowed else max(1, self._reset_at() - int(self._wall_clock())),
        )

    def consume(self, key: str) -> LimitDecision:
        if not self.enabled:
            return self.preview(key)
        today = self._today()
        _current_date, count = self._current(key)
        if count >= self.limit:
            return self.preview(key)
        count += 1
        self._entries[key] = (today, count)
        self._prune_old(today)
        return LimitDecision(
            allowed=True,
            limit=self.limit,
            remaining=max(0, self.limit - count),
            reset_at=self._reset_at(),
            retry_after=0,
        )

    def _prune_old(self, today: date) -> None:
        if len(self._entries) < 1024:
            return
        self._entries = {key: entry for key, entry in self._entries.items() if entry[0] == today}

    def snapshot(self, limit: int = 50) -> list[dict[str, int | str]]:
        if not self.enabled:
            return []
        today = self._today()
        rows = [
            {"key": key, "used": count, "limit": self.limit}
            for key, (entry_date, count) in self._entries.items()
            if entry_date == today
        ]
        rows.sort(key=lambda row: int(row["used"]), reverse=True)
        return rows[:limit]

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)


class ClientLimiter:
    """Combines gateway-wide, per-client and daily limits."""

    def __init__(
        self,
        *,
        client_rpm: int,
        daily_requests: int,
        global_rpm: int,
        max_concurrency: int = 2,
    ) -> None:
        self.rpm = SlidingWindowLimiter(client_rpm)
        self.daily = DailyQuota(daily_requests)
        self.global_rpm = SlidingWindowLimiter(global_rpm)
        self.max_concurrency = max(1, max_concurrency)
        self._active: dict[str, int] = {}

    def acquire_concurrency(self, client_id: str) -> None:
        active = self._active.get(client_id, 0)
        if active >= self.max_concurrency:
            raise RateLimitExceeded(
                f"Client concurrency limit exceeded ({self.max_concurrency} active requests)",
                retry_after=2,
                scope="client_concurrency",
                headers={
                    "X-RateLimit-Concurrency-Limit": str(self.max_concurrency),
                    "X-RateLimit-Concurrency-Remaining": "0",
                },
            )
        self._active[client_id] = active + 1

    def release_concurrency(self, client_id: str) -> None:
        active = self._active.get(client_id, 0)
        if active <= 1:
            self._active.pop(client_id, None)
        else:
            self._active[client_id] = active - 1

    def check(self, client_id: str) -> ClientLimitResult:
        rpm_preview = self.rpm.preview(client_id)
        daily_preview = self.daily.preview(client_id)
        global_preview = self.global_rpm.preview("__global__")

        denied = [
            (scope, decision)
            for scope, decision in (
                ("gateway_rpm", global_preview),
                ("client_daily", daily_preview),
                ("client_rpm", rpm_preview),
            )
            if not decision.allowed
        ]
        if denied:
            scope, _decision = denied[0]
            retry_after = max(decision.retry_after for _scope, decision in denied)
            headers = self._headers_for(rpm_preview, daily_preview, global_preview)
            if scope == "gateway_rpm":
                message = f"Gateway rate limit exceeded ({self.global_rpm.limit} requests/minute)"
            elif scope == "client_daily":
                message = f"Daily request quota exceeded ({self.daily.limit} requests/day)"
            else:
                message = f"Client rate limit exceeded ({self.rpm.limit} requests/minute)"
            raise RateLimitExceeded(
                message,
                retry_after=retry_after,
                scope=scope,
                headers=headers,
            )

        # No await occurs between preview and consume, so these cannot race.
        rpm = self.rpm.consume(client_id)
        daily = self.daily.consume(client_id)
        global_rpm = self.global_rpm.consume("__global__")
        if not (rpm.allowed and daily.allowed and global_rpm.allowed):
            raise RateLimitExceeded(
                "Rate limit changed while admitting the request; retry shortly",
                retry_after=1,
                scope="race",
                headers=self._headers_for(rpm, daily, global_rpm),
            )
        return ClientLimitResult(rpm=rpm, daily=daily, global_rpm=global_rpm)

    @staticmethod
    def _headers_for(
        rpm: LimitDecision,
        daily: LimitDecision,
        global_rpm: LimitDecision,
    ) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(rpm.limit),
            "X-RateLimit-Remaining": str(rpm.remaining),
            "X-RateLimit-Reset": str(rpm.reset_at),
            "X-RateLimit-Policy": f"{rpm.limit};w=60",
            "X-RateLimit-Global-Limit": str(global_rpm.limit),
            "X-RateLimit-Global-Remaining": str(global_rpm.remaining),
            "X-RateLimit-Global-Reset": str(global_rpm.reset_at),
        }
        if daily.enabled:
            headers.update(
                {
                    "X-RateLimit-Daily-Limit": str(daily.limit),
                    "X-RateLimit-Daily-Remaining": str(daily.remaining),
                    "X-RateLimit-Daily-Reset": str(daily.reset_at),
                }
            )
        return headers

    def snapshot(self) -> dict[str, list[dict[str, int | str]]]:
        return {
            "clients_rpm": self.rpm.snapshot(),
            "clients_daily": self.daily.snapshot(),
            "global": self.global_rpm.snapshot(),
            "active_concurrency": [
                {"key": key, "used": count, "limit": self.max_concurrency}
                for key, count in sorted(
                    self._active.items(), key=lambda item: item[1], reverse=True
                )[:50]
            ],
        }

    def reset(self) -> None:
        self.rpm.reset()
        self.daily.reset()
        self.global_rpm.reset()
