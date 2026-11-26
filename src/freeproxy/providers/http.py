from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import ProviderConfig, is_safe_model_id


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status_code: int | None
    latency_ms: float
    detail: str = ""


def build_auth_headers(config: ProviderConfig) -> dict[str, str]:
    key = config.api_key.get_secret_value().strip()
    if config.auth_mode == "none" or not key:
        return {}
    if config.auth_mode == "x-api-key":
        return {"x-api-key": key}
    return {"Authorization": f"Bearer {key}"}


class ProviderHTTP:
    """Small adapter around one OpenAI-compatible upstream."""

    def __init__(self, client: httpx.AsyncClient, config: ProviderConfig) -> None:
        self.client = client
        self.config = config

    def auth_headers(self) -> dict[str, str]:
        return build_auth_headers(self.config)

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "FreeProxyAPI/0.1",
            **self.config.extra_headers,
            **self.auth_headers(),
        }
        return headers

    def chat_headers(
        self,
        *,
        stream: bool,
        session_id: str | None,
    ) -> dict[str, str]:
        headers = {
            **self._base_headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.config.session_header and session_id:
            headers["x-agent-session"] = session_id
        return headers

    async def list_models(self, timeout_seconds: float) -> list[str]:
        response = await self.client.get(
            self.config.models_url,
            headers=self._base_headers(),
            timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        ids: list[str] = []

        if isinstance(payload, dict):
            raw_items: Any = payload.get("data", payload.get("models", []))
        else:
            raw_items = payload
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("data", [])

        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, str):
                    model_id = item
                elif isinstance(item, dict):
                    model_id = item.get("id") or item.get("name")
                else:
                    continue
                if not isinstance(model_id, str):
                    continue
                model_id = model_id.strip()
                if is_safe_model_id(model_id) and model_id not in ids:
                    ids.append(model_id)
        return ids[:500]

    async def health(self, timeout_seconds: float) -> HealthResult:
        started = time.perf_counter()
        try:
            response = await self.client.get(
                self.config.health_check_url,
                headers=self._base_headers(),
                timeout=httpx.Timeout(timeout_seconds, connect=timeout_seconds),
            )
        except httpx.HTTPError as exc:
            return HealthResult(
                ok=False,
                status_code=None,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                detail=type(exc).__name__,
            )

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        detail = ""
        ok = 200 <= response.status_code < 300
        try:
            payload = response.json()
            if isinstance(payload, dict):
                if payload.get("healthy") is False or payload.get("ok") is False:
                    ok = False
                detail = str(payload.get("mode") or payload.get("status") or "")[:80]
        except (json.JSONDecodeError, ValueError):
            pass
        return HealthResult(
            ok=ok,
            status_code=response.status_code,
            latency_ms=latency_ms,
            detail=detail,
        )

    @staticmethod
    def retry_after_seconds(response: httpx.Response) -> int | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(1, int(raw))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = math.ceil((retry_at - datetime.now(UTC)).total_seconds())
        return max(1, seconds)
