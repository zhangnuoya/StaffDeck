from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.channels.service_outbox import stage_channel_delivery
from app.core.conversation_projection import ConversationProjection
from app.db.models import ChatSession, Message, new_id, utc_now
from app.observability.event_log import EventLog
from app.session.helpers import public_session
from app.session.origin import PILOTDECK_GROUP_CHAT_CHANNEL
from app.session.session_schema import ChatTurnRequest


def get_or_create_session(db: Session, request: ChatTurnRequest) -> ChatSession:
    """Mirror of AgentLoop._get_or_create_session (shared by all runtimes)."""
    session_id = request.session_id or new_id("session")
    chat_session = db.get(ChatSession, session_id)
    if not chat_session:
        chat_session = ChatSession(
            id=session_id,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            channel=(
                request.channel
                if request.channel in {PILOTDECK_GROUP_CHAT_CHANNEL, "skill_test"}
                else None
            ),
        )
        db.add(chat_session)
        db.flush()
    elif not chat_session.agent_id and request.agent_id:
        chat_session.agent_id = request.agent_id
    return chat_session


def append_message(
    db: Session,
    tenant_id: str,
    session_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    """Mirror of AgentLoop._append_message."""
    message = Message(
        tenant_id=tenant_id,
        session_id=session_id,
        role=role,
        content=content,
        metadata_json=metadata or {},
    )
    db.add(message)
    return message


def mark_session_running(db: Session, chat_session: ChatSession) -> None:
    """Mirror of AgentLoop._mark_session_running."""
    if chat_session.status == "handoff":
        return
    chat_session.status = "running"
    chat_session.updated_at = utc_now()
    db.add(chat_session)


def fallback_session_title(message: str) -> str:
    return ConversationProjection.fallback_session_title(message)


def finalize_simple_turn(
    db: Session,
    events: EventLog,
    chat_session: ChatSession,
    tenant_id: str,
    reply: str,
    *,
    source_message: str | None,
    user_message_id: str | None,
    extra_metadata: dict[str, Any] | None = None,
) -> Message:
    """Persist the assistant reply and broadcast the standard turn-final events.

    Mirrors the citation-free tail of AgentLoop._finalize_turn: session status
    back to active, fallback title + summary, assistant message with channel
    outbox staging, then assistant_message_created / session_state_changed.
    """
    chat_session.updated_at = utc_now()
    if chat_session.status != "handoff":
        chat_session.status = "active"
    if not chat_session.title and source_message:
        title = fallback_session_title(source_message)
        if title:
            chat_session.title = title
    chat_session.summary = f"最近回复：{reply[:120]}"
    assistant_metadata: dict[str, Any] = dict(extra_metadata or {})
    if user_message_id:
        assistant_metadata.setdefault("user_message_id", user_message_id)
        assistant_metadata.setdefault("turn_id", user_message_id)
    assistant_message = append_message(
        db,
        tenant_id,
        chat_session.id,
        "assistant",
        reply,
        metadata=assistant_metadata,
    )
    stage_channel_delivery(db, chat_session, assistant_message)
    event_payload: dict[str, Any] = {
        "message_id": assistant_message.id,
        "assistant_message_id": assistant_message.id,
        "reply": reply,
    }
    if user_message_id:
        event_payload["user_message_id"] = user_message_id
        event_payload["turn_id"] = user_message_id
    events.record(tenant_id, chat_session.id, "assistant_message_created", event_payload)
    events.record(
        tenant_id,
        chat_session.id,
        "session_state_changed",
        public_session(chat_session).model_dump(),
    )
    return assistant_message
