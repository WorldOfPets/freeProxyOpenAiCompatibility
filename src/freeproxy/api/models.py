from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..rate_limit import ClientLimitResult
from .dependencies import ClientIdentity, authenticate, enforce_client_limits

router = APIRouter()


@router.get(
    "/v1/models",
    tags=["OpenAI compatible"],
    summary="List available models",
    responses={
        401: {"description": "Invalid or missing API key"},
        429: {"description": "Client rate limit"},
        500: {"description": "Internal error"},
    },
)
@router.get("/models", include_in_schema=False)
async def list_models(
    request: Request,
    _identity: Annotated[ClientIdentity, Depends(authenticate)],
    limits: Annotated[ClientLimitResult, Depends(enforce_client_limits)],
) -> JSONResponse:
    models = request.app.state.router.available_models()
    return JSONResponse(
        content={"object": "list", "data": models},
        headers=limits.headers(),
    )
