from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlmodel import Session

from app.db.models import ChatSession, Skill
from app.runtimes.contracts import AgentRuntimeKind
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse


class NativeAgentRuntime:
    """Adapter over the built-in AgentLoop engine.

    AgentLoop is imported lazily at call time so tests can keep monkeypatching
    ``app.core.agent_loop.AgentLoop`` (channel durable intake relies on this seam).
    """

    runtime_kind = AgentRuntimeKind.NATIVE

    def __init__(self, db: Session) -> None:
        self._db = db

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        from app.core.agent_loop import AgentLoop

        return AgentLoop(self._db).handle_turn(request)

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, Any]]:
        from app.core.agent_loop import AgentLoop

        yield from AgentLoop(self._db).handle_turn_stream(request)

    def finish_stale_completed_skill(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> None:
        """Maintenance pass used by session-list cleanup (a native-only concept)."""
        from app.core.agent_loop import AgentLoop

        AgentLoop(self._db)._finish_stale_completed_skill(tenant_id, chat_session, skills)
