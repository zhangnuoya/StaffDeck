from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any, Protocol

from app.session.session_schema import ChatTurnRequest, ChatTurnResponse


class AgentRuntimeKind(StrEnum):
    """Pluggable agent execution runtimes behind the bridge layer."""

    NATIVE = "native"
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"


def parse_runtime_kind(value: str | None) -> AgentRuntimeKind:
    """Best-effort parse; unknown/empty values fall back to the native engine."""
    if not value:
        return AgentRuntimeKind.NATIVE
    try:
        return AgentRuntimeKind(value)
    except ValueError:
        return AgentRuntimeKind.NATIVE


class RuntimeUnavailableError(Exception):
    """Raised when a runtime kind has no adapter registered yet."""

    def __init__(self, kind: AgentRuntimeKind, message: str | None = None) -> None:
        self.kind = kind
        super().__init__(message or f"agent runtime '{kind}' is not available")


class AgentRuntime(Protocol):
    """Bridge contract every agent execution runtime must satisfy.

    The surface intentionally mirrors AgentLoop: runtimes consume the existing
    ChatTurnRequest and emit the existing stream event dicts, so the SSE relay,
    traces, channel outbox, and scheduled tasks stay unchanged.
    """

    runtime_kind: AgentRuntimeKind

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse: ...

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, Any]]: ...
