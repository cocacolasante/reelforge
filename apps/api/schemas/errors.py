"""Stable error envelope shared by every non-2xx response.

Frontend dispatches on `error.code` — stable SCREAMING_SNAKE_CASE. Never match
on `error.message` (may change across releases).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody
    request_id: str | None = None


class ApiError(HTTPException):
    """Raising this anywhere produces the canonical error envelope."""

    def __init__(self, status_code: int, code: str, message: str, **details: Any):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details


def api_error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str | None = None,
    **details: Any,
) -> JSONResponse:
    body = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, details=details),
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers={"X-Request-Id": request_id} if request_id else {},
    )
