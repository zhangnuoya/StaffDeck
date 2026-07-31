from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from app.config import get_settings

_TOKEN_PURPOSE = "mcp-capability-gateway"
DEFAULT_TOKEN_TTL_SECONDS = 3600


@dataclass(frozen=True)
class CapabilityGrant:
    """Verified caller identity carried by a gateway capability token."""

    tenant_id: str
    agent_id: str
    session_id: str
    turn_id: str
    expires_at: int


def _signing_key() -> bytes:
    secret = get_settings().app_secret
    return hashlib.sha256(f"{_TOKEN_PURPOSE}:{secret}".encode()).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signature(body: str) -> str:
    return _b64encode(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())


def issue_capability_token(
    *,
    tenant_id: str,
    agent_id: str,
    session_id: str,
    turn_id: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Mint a short-lived token binding one tenant/agent/session/turn scope.

    Stateless by design: external runtimes receive the token at turn start and
    every gateway call re-checks resource authorization against the database,
    so revocation never depends on token invalidation.
    """
    payload = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "exp": int(time.time()) + ttl_seconds,
        "nonce": secrets.token_hex(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_signature(body)}"


def verify_capability_token(token: str, *, now: float | None = None) -> CapabilityGrant | None:
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _signature(body)):
        return None
    try:
        payload = json.loads(_b64decode(body))
        expires_at = int(payload["exp"])
        grant = CapabilityGrant(
            tenant_id=str(payload["tenant_id"]),
            agent_id=str(payload["agent_id"]),
            session_id=str(payload["session_id"]),
            turn_id=str(payload["turn_id"]),
            expires_at=expires_at,
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    current = time.time() if now is None else now
    if current >= grant.expires_at:
        return None
    return grant
