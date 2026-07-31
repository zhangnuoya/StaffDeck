from __future__ import annotations

from typing import ClassVar

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.runtimes import (
    AgentRuntimeKind,
    NativeAgentRuntime,
    RuntimeUnavailableError,
    create_runtime,
    resolve_runtime_for_request,
    resolve_runtime_kind,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse, SessionPublic


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_request(**overrides: object) -> ChatTurnRequest:
    payload: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "session_id": "session_x",
        "agent_id": "agent_x",
        "message": "你好",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)  # type: ignore[arg-type]


def _make_response(request: ChatTurnRequest) -> ChatTurnResponse:
    session_id = request.session_id or "session_x"
    return ChatTurnResponse(
        reply="ok",
        session_id=session_id,
        session_state=SessionPublic(session_id=session_id, tenant_id=request.tenant_id),
    )


class FakeAgentLoop:
    instances: ClassVar[list[FakeAgentLoop]] = []

    def __init__(self, db: Session) -> None:
        self.db = db
        self.turn_requests: list[ChatTurnRequest] = []
        self.stream_requests: list[ChatTurnRequest] = []
        self.stale_calls: list[tuple[object, ...]] = []
        FakeAgentLoop.instances.append(self)

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        self.turn_requests.append(request)
        return _make_response(request)

    def handle_turn_stream(self, request: ChatTurnRequest):
        self.stream_requests.append(request)
        yield {"event": "complete", "data": _make_response(request).model_dump(mode="json")}

    def _finish_stale_completed_skill(self, tenant_id, chat_session, skills) -> None:
        self.stale_calls.append((tenant_id, chat_session, skills))


@pytest.fixture(autouse=True)
def _reset_fake_agent_loop():
    FakeAgentLoop.instances = []
    yield
    FakeAgentLoop.instances = []


def test_create_runtime_returns_native_adapter() -> None:
    with _make_db() as db:
        runtime = create_runtime(db, AgentRuntimeKind.NATIVE)
    assert isinstance(runtime, NativeAgentRuntime)
    assert runtime.runtime_kind == AgentRuntimeKind.NATIVE


def test_create_runtime_rejects_unavailable_kinds() -> None:
    with _make_db() as db:
        for kind in (AgentRuntimeKind.CODEX, AgentRuntimeKind.CLAUDE_CODE):
            with pytest.raises(RuntimeUnavailableError) as excinfo:
                create_runtime(db, kind)
            assert excinfo.value.kind == kind


def test_resolve_runtime_kind_falls_back_to_native() -> None:
    with _make_db() as db:
        assert resolve_runtime_kind(db, "tenant_demo", None) == AgentRuntimeKind.NATIVE
        assert (
            resolve_runtime_kind(db, "tenant_demo", "agent_x", "session_x")
            == AgentRuntimeKind.NATIVE
        )


def test_resolve_runtime_for_request_returns_native_adapter() -> None:
    with _make_db() as db:
        runtime = resolve_runtime_for_request(db, _make_request())
    assert isinstance(runtime, NativeAgentRuntime)


def test_native_adapter_handle_turn_delegates_to_agent_loop(monkeypatch) -> None:
    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)
    request = _make_request()
    with _make_db() as db:
        response = NativeAgentRuntime(db).handle_turn(request)
    assert response.reply == "ok"
    assert FakeAgentLoop.instances[0].turn_requests == [request]


def test_native_adapter_handle_turn_stream_delegates_to_agent_loop(monkeypatch) -> None:
    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)
    request = _make_request()
    with _make_db() as db:
        events = list(NativeAgentRuntime(db).handle_turn_stream(request))
    assert [event["event"] for event in events] == ["complete"]
    assert FakeAgentLoop.instances[0].stream_requests == [request]


def test_native_adapter_finish_stale_completed_skill_delegates(monkeypatch) -> None:
    monkeypatch.setattr("app.core.agent_loop.AgentLoop", FakeAgentLoop)
    marker = object()
    with _make_db() as db:
        NativeAgentRuntime(db).finish_stale_completed_skill("tenant_demo", marker, [])
    assert FakeAgentLoop.instances[0].stale_calls == [("tenant_demo", marker, [])]
