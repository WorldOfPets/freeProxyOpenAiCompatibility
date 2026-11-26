from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..errors import InvalidRequest
from ..rate_limit import ClientLimitResult
from ..services.proxy import validate_chat_payload
from .dependencies import (
    ClientIdentity,
    authenticate,
    enforce_client_concurrency,
    enforce_client_limits,
)
from .utils import parse_json_object, read_limited_body

router = APIRouter()

_ERROR_RESPONSES = {
    400: {"description": "Invalid request"},
    401: {"description": "Invalid or missing API key"},
    404: {"description": "Unknown model or provider"},
    413: {"description": "Request body too large"},
    429: {"description": "Client or provider rate limit"},
    500: {"description": "Internal error"},
    502: {"description": "Invalid upstream response"},
    503: {"description": "Provider unavailable or busy"},
}

_CHAT_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["messages"],
    "properties": {
        "model": {
            "type": "string",
            "description": "Alias such as auto/coding, or a namespaced ID such as glm/glm-4.7",
        },
        "messages": {
            "type": "array",
            "items": {"type": "object"},
            "description": "OpenAI chat messages, including tool calls and tool results",
        },
        "stream": {"type": "boolean", "default": False},
        "max_tokens": {"type": "integer", "minimum": 1},
        "max_completion_tokens": {"type": "integer", "minimum": 1},
        "temperature": {"type": "number"},
        "tools": {"type": "array", "items": {"type": "object"}},
        "tool_choice": {},
    },
    "additionalProperties": True,
}


@router.post(
    "/v1/chat/completions",
    response_model=None,
    tags=["OpenAI compatible"],
    summary="Create a chat completion",
    responses=_ERROR_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _CHAT_REQUEST_SCHEMA}},
        }
    },
)
@router.post(
    "/chat/completions",
    response_model=None,
    include_in_schema=False,
    responses=_ERROR_RESPONSES,
)
async def chat_completions(
    request: Request,
    _identity: Annotated[ClientIdentity, Depends(authenticate)],
    limits: Annotated[ClientLimitResult, Depends(enforce_client_limits)],
    _concurrency: Annotated[None, Depends(enforce_client_concurrency)],
) -> Response:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise InvalidRequest("Content-Type must be application/json")
    settings = request.app.state.settings
    raw = await read_limited_body(
        request,
        settings.max_body_bytes,
        timeout_seconds=settings.body_read_timeout_seconds,
    )
    payload = parse_json_object(raw)
    validate_chat_payload(payload, settings)
    request_id = getattr(request.state, "request_id", "")
    return await request.app.state.proxy.chat(
        request,
        payload,
        identity_id=_identity.id,
        limits=limits,
        request_id=request_id,
    )
