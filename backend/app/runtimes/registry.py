from __future__ import annotations

from sqlmodel import Session

from app.db.models import AgentProfile, ChatSession
from app.runtimes.adapters.native import NativeAgentRuntime
from app.runtimes.contracts import (
    AgentRuntime,
    AgentRuntimeKind,
    RuntimeUnavailableError,
    parse_runtime_kind,
)
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

    The turn's agent wins (explicit request binding), then the session-bound
    agent; anything missing, mismatched, or unknown falls back to native.
    """
    resolved_agent_id = agent_id
    if not resolved_agent_id and session_id:
        chat_session = db.get(ChatSession, session_id)
        if chat_session and chat_session.tenant_id == tenant_id:
            resolved_agent_id = chat_session.agent_id
    if resolved_agent_id:
        agent = db.get(AgentProfile, resolved_agent_id)
        if agent and agent.tenant_id == tenant_id:
            return parse_runtime_kind(agent.runtime)
    return AgentRuntimeKind.NATIVE


def resolve_runtime_for_request(db: Session, request: ChatTurnRequest) -> AgentRuntime:
    kind = resolve_runtime_kind(db, request.tenant_id, request.agent_id, request.session_id)
    return create_runtime(db, kind)
