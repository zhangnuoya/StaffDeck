from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session

from app.db import get_session
from app.mcp_gateway.tokens import verify_capability_token
from app.mcp_gateway.tools import GatewayToolError, execute_gateway_tool, gateway_tool_descriptors

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp-gateway"])

PROTOCOL_VERSION = "2024-11-05"
_SERVER_INFO = {"name": "staffdeck-capability-gateway", "version": "1.0.0"}

_JSONRPC_PARSE_ERROR = -32700
_JSONRPC_INVALID_REQUEST = -32600
_JSONRPC_METHOD_NOT_FOUND = -32601
_JSONRPC_INVALID_PARAMS = -32602
_JSONRPC_INTERNAL_ERROR = -32603


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _session_header(token: str) -> dict[str, str]:
    return {"Mcp-Session-Id": hashlib.sha256(token.encode()).hexdigest()[:32]}


@router.post("/{token}")
async def mcp_endpoint(
    token: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),  # noqa: B008 - FastAPI dependency idiom
) -> Any:
    """Streamable-HTTP MCP endpoint scoped by a capability token.

    External agent runtimes (Codex, Claude Code) connect here to reach the
    tenant's knowledge bases, tools, and general skills under the calling
    employee's resource bindings.
    """
    grant = verify_capability_token(token)
    if grant is None:
        raise HTTPException(status_code=401, detail="MCP_TOKEN_INVALID")
    response.headers.update(_session_header(token))
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any body parse failure maps to a JSON-RPC parse error
        return _jsonrpc_error(None, _JSONRPC_PARSE_ERROR, "invalid JSON body")
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, _JSONRPC_INVALID_REQUEST, "batch requests are not supported")

    method = payload.get("method")
    request_id = payload.get("id")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _SERVER_INFO,
            },
        )
    if isinstance(method, str) and method.startswith("notifications/"):
        # JSON-RPC notification: acknowledge with 202 and no body, per MCP spec.
        response.status_code = 202
        return None
    if method == "ping":
        return _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": gateway_tool_descriptors(db, grant)})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            tool_result = execute_gateway_tool(db, grant, name, arguments)
        except GatewayToolError as exc:
            return _jsonrpc_error(request_id, _JSONRPC_INVALID_PARAMS, str(exc))
        except Exception as exc:
            logger.exception("MCP gateway tool failed: %s (agent=%s)", name, grant.agent_id)
            return _jsonrpc_error(
                request_id, _JSONRPC_INTERNAL_ERROR, f"tool execution failed: {exc}"
            )
        return _jsonrpc_result(request_id, tool_result)
    return _jsonrpc_error(request_id, _JSONRPC_METHOD_NOT_FOUND, f"unknown method: {method}")


@router.get("/{token}")
async def mcp_endpoint_stream(token: str) -> Response:
    """Server-initiated SSE channel is not supported; clients must tolerate 405."""
    if verify_capability_token(token) is None:
        raise HTTPException(status_code=401, detail="MCP_TOKEN_INVALID")
    return Response(status_code=405)
