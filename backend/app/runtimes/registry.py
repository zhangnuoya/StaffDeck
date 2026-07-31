from __future__ import annotations

from sqlmodel import Session

from app.runtimes.adapters.native import NativeAgentRuntime
from app.runtimes.contracts import AgentRuntime, AgentRuntimeKind, RuntimeUnavailableError
from app.session.session_schema import ChatTurnRequest


def create_runtime(db: Session, kind: AgentRuntimeKind) -> AgentRuntime:
    """Instantiate the adapter for `kind`.

    Adapters are created per call because db sessions are thread-bound at every
    entry point (chat stream worker, channel intake daemon, scheduled tasks).
    """
    if kind == AgentRuntimeKind.NATIVE:
        return NativeAgentRuntime(db)
    raise RuntimeUnavailableError(kind)


def resolve_runtime_kind(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    session_id: str | None = None,
) -> AgentRuntimeKind:
    """Resolve which runtime should execute the turn.

    Everything falls back to the native engine until per-agent selection is
    wired to AgentProfile.runtime.
    """
    return AgentRuntimeKind.NATIVE


def resolve_runtime_for_request(db: Session, request: ChatTurnRequest) -> AgentRuntime:
    kind = resolve_runtime_kind(db, request.tenant_id, request.agent_id, request.session_id)
    return create_runtime(db, kind)
