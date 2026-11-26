from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import Request

from ..errors import InvalidRequest, PayloadTooLarge


async def read_limited_body(
    request: Request,
    limit: int,
    *,
    timeout_seconds: float = 30.0,
) -> bytes:
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise PayloadTooLarge(limit)
        except ValueError:
            pass

    buffer = bytearray()
    total = 0
    chunks = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in request.stream():
                chunks += 1
                if chunks > 10_000:
                    raise InvalidRequest("Request body uses too many chunks")
                total += len(chunk)
                if total > limit:
                    raise PayloadTooLarge(limit)
                buffer.extend(chunk)
    except TimeoutError:
        raise InvalidRequest("Timed out while reading the request body") from None
    return bytes(buffer)


def parse_json_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise InvalidRequest("Request body is empty")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise InvalidRequest("Request body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise InvalidRequest("Request body must be a JSON object")
    return payload
