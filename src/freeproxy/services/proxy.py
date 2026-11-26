from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import Settings, is_safe_model_id
from ..errors import (
    GatewayBusy,
    InvalidRequest,
    ProviderBusy,
    ProviderUnavailable,
    RateLimitExceeded,
    UpstreamError,
)
from ..metrics import Metrics
from ..providers.base import ProviderRuntime, ProviderSlot
from ..providers.http import ProviderHTTP
from ..rate_limit import ClientLimitResult
from .routing import ModelRouter, ModelTarget

logger = logging.getLogger("freeproxy.proxy")
stream_logger = logging.getLogger("freeproxy.stream")
_MAX_ERROR_BODY_BYTES = 64 * 1024


class ProxyStreamingResponse(StreamingResponse):
    """Streaming response that always releases provider and gateway slots."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        upstream: httpx.Response,
        slot: ProviderSlot,
        release_global: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._upstream = upstream
        self._slot = slot
        self._release_global = release_global
        self._cleaned = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._cleaned:
                self._cleaned = True
                self._slot.release()
                self._release_global()
                try:
                    await self._upstream.aclose()
                except Exception:  # noqa: BLE001 - cleanup must not mask the response
                    stream_logger.debug("error closing upstream stream", exc_info=True)


def _extract_text(content: Any, limit: int = 4000) -> str:
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts: list[str] = []
        total = 0
        for index, part in enumerate(content):
            if index >= 1000 or total >= limit:
                break
            text = part.get("text") if isinstance(part, dict) else part
            if not isinstance(text, str) or not text:
                continue
            taken = text[: limit - total]
            parts.append(taken)
            total += len(taken)
        return "".join(parts)[:limit]
    return ""


def derive_session_id(
    client_id: str,
    payload: dict[str, Any],
    inbound_headers: Any,
    requested_model: str,
) -> str:
    explicit = inbound_headers.get("x-freeproxy-session") or inbound_headers.get("x-agent-session")
    if explicit:
        safe = "".join(
            ch for ch in str(explicit) if ch.isascii() and (ch.isalnum() or ch in "._:-")
        )[:128]
        if safe:
            digest = hashlib.sha256(f"{client_id}|explicit|{safe}".encode()).hexdigest()
            return "fp-" + digest[:32]

    first_user = ""
    for message in payload.get("messages", []):
        if isinstance(message, dict) and message.get("role") == "user":
            first_user = _extract_text(message.get("content"))
            break
    basis = f"{client_id}|{requested_model}|{first_user}".encode()
    return "fp-" + hashlib.sha256(basis).hexdigest()[:32]


def validate_chat_payload(payload: Any, settings: Settings) -> None:
    if not isinstance(payload, dict):
        raise InvalidRequest("Request body must be a JSON object")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("'messages' must be a non-empty array")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        role = message.get("role")
        if not isinstance(role, str) or not role:
            raise InvalidRequest(f"messages[{index}].role must be a non-empty string")
        if "content" not in message and "tool_calls" not in message:
            raise InvalidRequest(f"messages[{index}] must contain content or tool_calls")

    model = payload.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise InvalidRequest("'model' must be a non-empty string")
        model = model.strip()
        payload["model"] = model
        if not is_safe_model_id(model):
            raise InvalidRequest("'model' must be a safe ASCII model ID")

    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("'stream' must be a boolean")

    for field in ("max_tokens", "max_completion_tokens"):
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidRequest(f"'{field}' must be a positive integer")
        if value > settings.max_output_tokens:
            raise InvalidRequest(
                f"'{field}' exceeds the gateway limit of {settings.max_output_tokens} tokens",
                code="max_tokens_too_large",
            )

    tools = payload.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise InvalidRequest("'tools' must be an array")


class ProxyService:
    def __init__(
        self,
        settings: Settings,
        router: ModelRouter,
        runtimes: dict[str, ProviderRuntime],
        metrics: Metrics,
    ) -> None:
        self.settings = settings
        self.router = router
        self.runtimes = runtimes
        self.metrics = metrics
        self._global_semaphore = asyncio.Semaphore(settings.global_max_concurrency)

    def _release_global(self) -> None:
        self._global_semaphore.release()

    async def chat(
        self,
        request: Request,
        payload: dict[str, Any],
        *,
        identity_id: str,
        limits: ClientLimitResult,
        request_id: str,
    ) -> Response:
        self.metrics.gateway_requests += 1
        stream = bool(payload.get("stream", False))
        requested_model = str(payload.get("model") or self.settings.default_alias)
        forced_provider = request.headers.get("x-freeproxy-provider")
        targets = self.router.resolve(requested_model, forced_provider)
        session_id = derive_session_id(identity_id, payload, request.headers, requested_model)

        try:
            await asyncio.wait_for(
                self._global_semaphore.acquire(),
                timeout=self.settings.global_queue_timeout_seconds,
            )
        except TimeoutError:
            raise GatewayBusy() from None

        global_owned = True
        failures: list[dict[str, Any]] = []
        retry_afters: list[int] = []
        had_rate_limit = False
        had_other_failure = False
        try:
            for target_index, target in enumerate(targets):
                runtime = self.runtimes[target.provider_id]
                upstream_payload = dict(payload)
                upstream_payload["model"] = target.model
                if (
                    runtime.config.inject_max_tokens
                    and "max_tokens" not in upstream_payload
                    and "max_completion_tokens" not in upstream_payload
                ):
                    upstream_payload["max_tokens"] = self.settings.default_max_tokens

                headers = runtime.http.chat_headers(
                    stream=stream,
                    session_id=session_id if runtime.config.session_header else None,
                )
                upstream_request = runtime.http.client.build_request(
                    "POST",
                    runtime.config.chat_url,
                    json=upstream_payload,
                    headers=headers,
                    timeout=httpx.Timeout(
                        read=runtime.config.timeout_seconds,
                        connect=self.settings.request_connect_timeout_seconds,
                        write=30.0,
                        pool=10.0,
                    ),
                )

                request.state.provider = target.provider_id
                request.state.model = target.model
                request.state.route = target.route_label

                for attempt in range(runtime.config.max_retries + 1):
                    slot: ProviderSlot | None = None
                    try:
                        try:
                            slot = await runtime.acquire()
                        except RateLimitExceeded as exc:
                            had_rate_limit = True
                            failures.append(
                                {
                                    "provider": target.provider_id,
                                    "status": exc.status_code,
                                    "kind": exc.code,
                                }
                            )
                            if exc.retry_after:
                                retry_afters.append(exc.retry_after)
                            break
                        except (ProviderUnavailable, ProviderBusy) as exc:
                            had_other_failure = True
                            failures.append(
                                {
                                    "provider": target.provider_id,
                                    "status": exc.status_code,
                                    "kind": exc.code,
                                }
                            )
                            if exc.retry_after:
                                retry_afters.append(exc.retry_after)
                            break

                        self.metrics.record_attempt(target.provider_id, stream=stream)
                        started = time.perf_counter()
                        try:
                            upstream = await runtime.http.client.send(upstream_request, stream=True)
                        except httpx.TimeoutException:
                            had_other_failure = True
                            runtime.record_failure(status_code=None, detail="timeout")
                            self.metrics.record_error(target.provider_id)
                            failures.append(
                                {"provider": target.provider_id, "status": None, "kind": "timeout"}
                            )
                            if attempt < runtime.config.max_retries:
                                await asyncio.sleep(0.5 * (attempt + 1))
                                continue
                            break
                        except httpx.RequestError as exc:
                            had_other_failure = True
                            runtime.record_failure(
                                status_code=None,
                                detail=type(exc).__name__,
                            )
                            self.metrics.record_error(target.provider_id)
                            failures.append(
                                {
                                    "provider": target.provider_id,
                                    "status": None,
                                    "kind": type(exc).__name__,
                                }
                            )
                            if attempt < runtime.config.max_retries:
                                await asyncio.sleep(0.5 * (attempt + 1))
                                continue
                            break

                        if upstream.status_code >= 300:
                            raw = await self._read_response(upstream)
                            retry_after = ProviderHTTP.retry_after_seconds(upstream)
                            runtime.record_failure(
                                status_code=upstream.status_code,
                                retry_after=retry_after,
                                detail=f"HTTP {upstream.status_code}",
                            )
                            self.metrics.record_error(
                                target.provider_id,
                                rate_limited=upstream.status_code == 429,
                            )
                            if upstream.status_code == 429:
                                had_rate_limit = True
                            else:
                                had_other_failure = True
                            if retry_after:
                                retry_afters.append(retry_after)
                            failures.append(
                                {
                                    "provider": target.provider_id,
                                    "status": upstream.status_code,
                                    "kind": "http",
                                }
                            )
                            if 400 <= upstream.status_code < 500 and upstream.status_code not in {
                                401,
                                403,
                                408,
                                429,
                            }:
                                return self._client_error_response(
                                    upstream.status_code,
                                    raw,
                                    target,
                                    request_id,
                                    limits,
                                    target_index,
                                )
                            if upstream.status_code == 429:
                                break
                            if attempt < runtime.config.max_retries:
                                await asyncio.sleep(0.5 * (attempt + 1))
                                continue
                            break

                        if stream:
                            headers_out = self._response_headers(
                                target, request_id, limits, target_index, stream=True
                            )
                            generator = self._stream_body(
                                upstream,
                                runtime,
                                target,
                                started,
                                request_id,
                                self.settings.max_upstream_bytes,
                            )
                            streaming_response = ProxyStreamingResponse(
                                generator,
                                upstream=upstream,
                                slot=slot,
                                release_global=self._release_global,
                                status_code=upstream.status_code,
                                media_type=upstream.headers.get(
                                    "content-type", "text/event-stream"
                                ),
                                headers=headers_out,
                            )
                            slot = None
                            global_owned = False
                            return streaming_response

                        try:
                            raw = await self._read_full_response(
                                upstream,
                                self.settings.max_upstream_bytes,
                                target.provider_id,
                            )
                        except UpstreamError:
                            runtime.record_failure(status_code=502, detail="response_read")
                            self.metrics.record_error(target.provider_id)
                            raise
                        latency_ms = (time.perf_counter() - started) * 1000
                        try:
                            response_payload = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            runtime.record_failure(status_code=502, detail="invalid_json")
                            self.metrics.record_error(target.provider_id)
                            raise UpstreamError(
                                "Upstream returned a non-JSON response",
                                provider=target.provider_id,
                            ) from None
                        if not isinstance(response_payload, dict):
                            runtime.record_failure(status_code=502, detail="invalid_json")
                            self.metrics.record_error(target.provider_id)
                            raise UpstreamError(
                                "Upstream returned an unexpected JSON payload",
                                provider=target.provider_id,
                            )
                        runtime.record_success()
                        self.metrics.record_success(target.provider_id, latency_ms)
                        self._record_usage(target.provider_id, response_payload)
                        return JSONResponse(
                            content=response_payload,
                            status_code=upstream.status_code,
                            headers=self._response_headers(
                                target, request_id, limits, target_index, stream=False
                            ),
                        )
                    finally:
                        if slot is not None:
                            slot.release()

            summary = ", ".join(
                f"{item['provider']}={item.get('status') or item.get('kind')}"
                for item in failures[:4]
            )
            if had_rate_limit and not had_other_failure:
                raise RateLimitExceeded(
                    f"All candidate providers are rate limited ({summary})",
                    retry_after=max(1, min(retry_afters or [5])),
                    scope="providers",
                    headers={"X-FreeProxy-Attempts": str(len(failures))},
                )
            raise ProviderUnavailable(
                f"No provider could serve the request ({summary or 'no attempts'})",
                provider="gateway",
                retry_after=min(retry_afters) if retry_afters else 5,
            )
        finally:
            if global_owned:
                self._release_global()

    @staticmethod
    async def _read_response(response: httpx.Response) -> bytes:
        """Read a bounded upstream error body; never let it escape as a 500."""
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                if total >= _MAX_ERROR_BODY_BYTES:
                    break
                part = chunk[: _MAX_ERROR_BODY_BYTES - total]
                chunks.append(part)
                total += len(part)
        except httpx.HTTPError:
            pass
        finally:
            with suppress(httpx.HTTPError):
                await response.aclose()
        return b"".join(chunks)

    @staticmethod
    async def _read_full_response(
        response: httpx.Response,
        limit: int,
        provider: str,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise UpstreamError(
                        f"Upstream response exceeds the {limit} byte limit",
                        provider=provider,
                        code="upstream_response_too_large",
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Failed to read the upstream response",
                provider=provider,
            ) from exc
        finally:
            with suppress(httpx.HTTPError):
                await response.aclose()
        return b"".join(chunks)

    async def _stream_body(
        self,
        upstream: httpx.Response,
        runtime: ProviderRuntime,
        target: ModelTarget,
        started: float,
        request_id: str,
        max_bytes: int,
    ) -> AsyncIterator[bytes]:
        ok = False
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes():
                if not chunk:
                    continue
                sent += len(chunk)
                if sent > max_bytes:
                    runtime.record_failure(status_code=502, detail="stream_too_large")
                    self.metrics.record_error(target.provider_id)
                    error_payload = {
                        "error": {
                            "message": "Upstream stream exceeded the byte limit",
                            "type": "upstream_error",
                            "code": "stream_too_large",
                            "param": None,
                            "provider": target.provider_id,
                        }
                    }
                    yield (b"event: error\ndata: " + json.dumps(error_payload).encode() + b"\n\n")
                    return
                yield chunk
            ok = True
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            runtime.record_failure(status_code=None, detail=type(exc).__name__)
            self.metrics.record_error(target.provider_id)
            stream_logger.warning(
                "upstream stream interrupted",
                extra={
                    "request_id": request_id,
                    "provider": target.provider_id,
                    "model": target.model,
                    "route": target.route_label,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "upstream_status": type(exc).__name__,
                },
            )
            # Do not end a 200 SSE response silently: emit a terminal error
            # event so OpenAI-compatible clients can distinguish truncation.
            error_payload = {
                "error": {
                    "message": "Upstream stream was interrupted",
                    "type": "upstream_error",
                    "code": "stream_interrupted",
                    "param": None,
                    "provider": target.provider_id,
                }
            }
            yield b"event: error\ndata: " + json.dumps(error_payload).encode() + b"\n\n"
        finally:
            if ok:
                latency_ms = (time.perf_counter() - started) * 1000
                runtime.record_success()
                self.metrics.record_success(target.provider_id, latency_ms)
            stream_logger.info(
                "stream finished",
                extra={
                    "request_id": request_id,
                    "provider": target.provider_id,
                    "model": target.model,
                    "route": target.route_label,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "stream": True,
                },
            )

    def _record_usage(self, provider_id: str, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
        completion = usage.get("completion_tokens") or usage.get("output_tokens")
        self.metrics.record_usage(
            provider_id,
            prompt_tokens=prompt if isinstance(prompt, int) else None,
            completion_tokens=completion if isinstance(completion, int) else None,
        )

    def _response_headers(
        self,
        target: ModelTarget,
        request_id: str,
        limits: ClientLimitResult,
        target_index: int,
        *,
        stream: bool,
    ) -> dict[str, str]:
        headers = {
            "X-Request-ID": request_id,
            "X-FreeProxy-Provider": target.provider_id,
            "X-FreeProxy-Model": target.model,
            "X-FreeProxy-Route": target.route_label,
            "X-FreeProxy-Requested-Model": target.requested_model,
            "X-FreeProxy-Fallback": "true" if target_index else "false",
        }
        headers.update(limits.headers())
        if stream:
            headers["Cache-Control"] = "no-cache"
            headers["X-Accel-Buffering"] = "no"
        else:
            headers["Cache-Control"] = "no-store"
        return headers

    def _client_error_response(
        self,
        status_code: int,
        raw: bytes,
        target: ModelTarget,
        request_id: str,
        limits: ClientLimitResult,
        target_index: int,
    ) -> JSONResponse:
        payload: Any = None
        try:
            payload = json.loads(raw[:65536])
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            content = payload
        else:
            message = "Upstream rejected the request"
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, str) and error.strip():
                    message = error[:500]
                else:
                    message = str(payload.get("message") or payload.get("detail") or message)[:500]
            content = {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "code": f"upstream_http_{status_code}",
                    "param": None,
                    "provider": target.provider_id,
                }
            }
        headers = self._response_headers(target, request_id, limits, target_index, stream=False)
        headers["X-FreeProxy-Upstream-Status"] = str(status_code)
        return JSONResponse(content=content, status_code=status_code, headers=headers)
