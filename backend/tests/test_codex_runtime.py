from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.config import get_settings
from app.core.cancellation import cancel_chat_turn
from app.db.models import AgentEvent, AgentProfile, ChatSession, Message, Tenant
from app.mcp_gateway import verify_capability_token
from app.runtimes import (
    AgentRuntimeKind,
    RuntimeUnavailableError,
    create_runtime,
    resolve_runtime_for_request,
)
from app.runtimes.adapters.codex import CodexAgentRuntime, codex_cli_available
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse

FAKE_CLI = str(Path(__file__).resolve().parents[1] / "mock_servers" / "fake_codex_cli.py")


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session, runtime: str = "codex") -> AgentProfile:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    agent = AgentProfile(
        id="agent_codex",
        tenant_id="tenant_demo",
        name="Codex 员工",
        persona_prompt="严谨、克制，先验证再执行。",
        runtime=runtime,
        runtime_config_json={"model": "fake-codex-model"},
    )
    db.add(agent)
    db.commit()
    return agent


@pytest.fixture
def codex_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "codex_cli_path", FAKE_CLI)
    monkeypatch.setattr(settings, "codex_workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "tool_base_url", "http://testserver")
    monkeypatch.setattr(settings, "codex_timeout_seconds", 30.0)
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "")
    return settings


def _request(**overrides: object) -> ChatTurnRequest:
    payload: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "agent_id": "agent_codex",
        "message": "帮我生成一份销售报表",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)  # type: ignore[arg-type]


def _read_capture(tmp_path) -> dict:
    import json

    return json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# availability / registry
# ---------------------------------------------------------------------------


def test_codex_unavailable_without_cli(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "codex_cli_path", "/nonexistent/codex.exe")
    assert codex_cli_available(settings) is False
    with _make_db() as db, pytest.raises(RuntimeUnavailableError):
        create_runtime(db, AgentRuntimeKind.CODEX)


def test_registry_resolves_codex_runtime(codex_settings) -> None:
    with _make_db() as db:
        _seed(db)
        runtime = resolve_runtime_for_request(db, _request())
        assert isinstance(runtime, CodexAgentRuntime)


# ---------------------------------------------------------------------------
# sync turn
# ---------------------------------------------------------------------------


def test_handle_turn_end_to_end_with_fake_cli(codex_settings, tmp_path) -> None:
    with _make_db() as db:
        _seed(db)
        response = CodexAgentRuntime(db).handle_turn(_request())

        assert isinstance(response, ChatTurnResponse)
        assert response.reply == "假 Codex 回复：任务完成。"
        session = db.get(ChatSession, response.session_id)
        assert session is not None
        assert session.agent_id == "agent_codex"
        assert session.status == "active"
        assert session.title
        state = session.runtime_state_json
        assert state["thread_id"] == "thread_fake_1"
        assert state["runtime"] == "codex"
        assert state["turn_count"] == 1

        messages = list(db.exec(select(Message).where(Message.session_id == session.id)).all())
        assert [row.role for row in messages] == ["user", "assistant"]
        assistant = messages[-1]
        assert assistant.metadata_json["runtime"] == "codex"
        assert assistant.metadata_json["codex_thread_id"] == "thread_fake_1"
        assert assistant.metadata_json["codex_usage"]["output_tokens"] == 5

        capture = _read_capture(tmp_path)
        argv_text = " ".join(capture["argv"])
        assert "exec" in capture["argv"]
        assert "--json" in capture["argv"]
        assert "resume" not in capture["argv"]
        assert "fake-codex-model" in argv_text
        assert "staffdeck" in capture["prompt"] or "企业知识库" in capture["prompt"]
        assert "严谨、克制" in capture["prompt"]
        assert "帮我生成一份销售报表" in capture["prompt"]

        url_arg = next(
            arg for arg in capture["argv"] if arg.startswith("mcp_servers.staffdeck.url=")
        )
        token = url_arg.split("/mcp/", 1)[1].rstrip('"')
        grant = verify_capability_token(token)
        assert grant is not None
        assert grant.tenant_id == "tenant_demo"
        assert grant.agent_id == "agent_codex"
        assert grant.session_id == session.id

        # 第二轮：沿用 thread_id 走 codex exec resume
        second = CodexAgentRuntime(db).handle_turn(
            _request(session_id=session.id, message="再加上环比数据")
        )
        assert second.reply == "假 Codex 回复：任务完成。"
        capture2 = _read_capture(tmp_path)
        assert "resume" in capture2["argv"]
        assert "thread_fake_1" in capture2["argv"]
        db.refresh(session)
        assert session.runtime_state_json["turn_count"] == 2


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def test_handle_turn_stream_emits_normalized_events(codex_settings) -> None:
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert kinds[0] == "session_created"
        assert "user_message_received" in kinds
        assert "tool_result" in kinds  # command_execution 映射为工具卡片
        assert "stream_delta" in kinds
        assert kinds[-2:] == ["stream_end", "complete"]

        complete = events[-1]["data"]
        response = ChatTurnResponse.model_validate(complete)
        assert response.reply == "假 Codex 回复：任务完成。"

        session_id = response.session_id
        deltas = list(
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "stream_delta",
                )
            ).all()
        )
        assert deltas
        assert all(row.payload_json.get("turn_id") for row in deltas)
        activity = list(
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "tool_result",
                )
            ).all()
        )
        assert activity
        assert activity[0].payload_json["toolId"] == "codex.command"


def test_multi_message_marks_progress_and_final_reply(codex_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "multi_message")
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        phases = [event["data"].get("phase") for event in events if event["event"] == "status"]
        assert "codex_progress" in phases
        complete = events[-1]["data"]
        assert complete["reply"] == "最终答复：报告已生成。"


# ---------------------------------------------------------------------------
# failure / timeout / cancellation
# ---------------------------------------------------------------------------


def test_no_agent_message_is_a_clean_error(codex_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "no_message")
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert "error_occurred" in kinds
        assert "complete" not in kinds
        response = CodexAgentRuntime(db).handle_turn(_request())
        assert response.reply.startswith("Codex 执行失败")


def test_timeout_kills_codex_process(codex_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    settings = get_settings()
    monkeypatch.setattr(settings, "codex_timeout_seconds", 1.0)
    started = time.monotonic()
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
    elapsed = time.monotonic() - started
    kinds = [event["event"] for event in events]
    assert "error_occurred" in kinds
    error = next(event for event in events if event["event"] == "error_occurred")
    assert error["data"]["code"] == "CODEX_TIMEOUT"
    assert elapsed < 15


def test_cancellation_stops_stream(codex_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CODEX_SLOW_SECONDS", "30")
    with _make_db() as db:
        _seed(db)
        db.add(
            ChatSession(
                id="session_cancel",
                tenant_id="tenant_demo",
                agent_id="agent_codex",
            )
        )
        db.commit()
        timer = threading.Timer(1.0, cancel_chat_turn, args=("session_cancel", "ct_cancel"))
        timer.start()
        try:
            events = list(
                CodexAgentRuntime(db).handle_turn_stream(
                    _request(session_id="session_cancel", client_turn_id="ct_cancel")
                )
            )
        finally:
            timer.cancel()
        kinds = [event["event"] for event in events]
        assert "stream_cancelled" in kinds
        assert "complete" not in kinds
