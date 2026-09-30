"""MCP over Streamable HTTP, for agents holding a ReelForge API key.

Muse (Meta's assistant) is the first caller: it speaks MCP natively, so a
hosted endpoint plus a bearer key is the whole integration — custom
connectors aren't reviewed by Meta. Endpoint: `POST {public_api_base}/mcp`.

Stateless on purpose: every request carries its key, so there is no session
to keep or expire, nothing two agents can share, and an API restart can't
strand a connector mid-conversation. We answer with plain JSON and never
open an SSE stream, which Streamable HTTP allows for request/response
servers like this one.

Tools (apps/api/mcp/tools.py) dispatch through the normal API routes
in-process with the caller's key, so every route's own validation and
conflict checks apply exactly as they do for the dashboard.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_db
from apps.api.mcp.tools import TOOLS, TOOLS_BY_NAME, ToolContext, ToolError
from apps.api.services import api_keys

log = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "reelforge", "title": "ReelForge", "version": "1.0.0"}
INSTRUCTIONS = (
    "ReelForge turns raw footage into finished video: it watches the clips, reads what is "
    "said in them, picks the moments worth keeping, and renders them with captions, music "
    "and transitions. Start with overview to see which projects exist and what footage each "
    "holds. Cutting takes minutes to tens of minutes, so it runs as a job you poll rather "
    "than a call you wait on. You cannot publish anything to YouTube, Instagram or TikTok, "
    "and you cannot delete footage, projects or clips — say so plainly if asked."
)

# A wrong key is cheap to send and expensive to ignore, so bad keys from one
# address are capped. Successful calls are not limited: a working key is the
# user's own agent, and cutting is already gated by the pipeline's 409s.
BAD_KEY_LIMIT = 20
BAD_KEY_WINDOW_S = 600
_bad_key_hits: dict[str, deque[float]] = {}


def _record_bad_key(ip: str) -> bool:
    """Count one rejected key for this address; True once it's over the cap."""
    now = time.monotonic()
    hits = _bad_key_hits.setdefault(ip, deque())
    while hits and hits[0] < now - BAD_KEY_WINDOW_S:
        hits.popleft()
    hits.append(now)
    return len(hits) > BAD_KEY_LIMIT


def _rpc_result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": message},
        headers={"WWW-Authenticate": 'Bearer realm="reelforge"'},
    )


async def _handle(msg: dict, ctx: ToolContext) -> dict | None:
    """One JSON-RPC message -> response (None for notifications)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _rpc_error(
            msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request"
        )
    method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:  # notification (e.g. notifications/initialized)
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _rpc_result(
            msg_id,
            {
                "protocolVersion": requested
                if requested in SUPPORTED_VERSIONS
                else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _rpc_result(msg_id, {})
    if method == "tools/list":
        return _rpc_result(msg_id, {"tools": [t.listing() for t in TOOLS]})
    if method == "tools/call":
        tool = TOOLS_BY_NAME.get(params.get("name"))
        if tool is None:
            return _rpc_error(msg_id, -32602, f"Unknown tool: {params.get('name')!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, -32602, "arguments must be an object")
        try:
            result = await tool.run(ctx, args)
        except (ToolError, KeyError, ValueError, TypeError) as exc:
            # Returned as tool output, not a protocol error: the agent can read
            # it, explain it to the user, and retry.
            text = f"missing argument {exc}" if isinstance(exc, KeyError) else str(exc)
            return _rpc_result(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"ReelForge refused this: {text}"}],
                    "isError": True,
                },
            )
        body = json.dumps(result, indent=2, default=str)
        payload: dict[str, Any] = {"content": [{"type": "text", "text": body}], "isError": False}
        if isinstance(result, dict):
            payload["structuredContent"] = result
        return _rpc_result(msg_id, payload)
    return _rpc_error(msg_id, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    token = api_keys.parse_bearer(request.headers.get("authorization"))
    client_ip = request.client.host if request.client else "unknown"
    if token is None:
        return _unauthorized("A ReelForge API key is required (Settings -> Agent access).")
    if await api_keys.verify(db, token) is None:
        if _record_bad_key(client_ip):
            return JSONResponse(
                status_code=429, content={"error": "Too many bad keys; try later."}
            )
        return _unauthorized("Invalid or revoked API key.")
    # A key that works clears this address's tally of wrong ones.
    _bad_key_hits.pop(client_ip, None)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body
        return JSONResponse(status_code=400, content=_rpc_error(None, -32700, "Parse error"))

    # Tools call the regular API in-process with the same key. `request.app`
    # rather than an import of the module-level instance: this API is built by
    # create_app(), and tests build their own, which an import would bypass.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app),
        base_url="http://reelforge.internal",
        timeout=120.0,
    ) as client:
        ctx = ToolContext(authorization=request.headers["authorization"], client=client)
        if isinstance(body, list):
            responses = [r for r in [await _handle(m, ctx) for m in body] if r is not None]
            if not responses:
                return Response(status_code=202)
            return JSONResponse(responses)
        response = await _handle(body, ctx)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


@router.get("/mcp")
async def mcp_no_stream() -> JSONResponse:
    # Streamable HTTP lets a server offer a server-initiated SSE stream. This
    # one is stateless and has none; saying so beats a hanging connection.
    return JSONResponse(
        status_code=405,
        content={"error": "This MCP server does not open server-sent streams"},
        headers={"Allow": "POST"},
    )


@router.delete("/mcp")
async def mcp_no_session() -> JSONResponse:
    return JSONResponse(
        status_code=405,
        content={"error": "This MCP server is stateless; nothing to delete"},
        headers={"Allow": "POST"},
    )
