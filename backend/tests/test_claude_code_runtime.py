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
from app.runtimes import AgentRuntimeKind, RuntimeUnavailableError, create_runtime
from app.runtimes.adapters.claude_code import ClaudeCodeAgentRuntime, claude_cli_available
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse

FAKE_CLI = str(Path(__file__).resolve().parents[1] / "mock_servers" / "fake_claude_cli.py")


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> AgentProfile:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    agent = AgentProfile(
        id="agent_claude",
        tenant_id="tenant_demo",
        name="Claude 员工",
        persona_prompt="严谨、克制，先验证再执行。",
        runtime="claude_code",
        runtime_config_json={"model": "fake-claude-model"},
    )
    db.add(agent)
    db.commit()
    return agent


@pytest.fixture
def claude_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "claude_code_cli_path", FAKE_CLI)
    monkeypatch.setattr(settings, "codex_workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "tool_base_url", "http://testserver")
    monkeypatch.setattr(settings, "claude_code_timeout_seconds", 30.0)
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "")
    return settings


def _request(**overrides: object) -> ChatTurnRequest:
    payload: dict[str, object] = {
        "tenant_id": "tenant_demo",
        "agent_id": "agent_claude",
        "message": "帮我生成一份销售报表",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)  # type: ignore[arg-type]


def _read_capture(tmp_path) -> dict:
    import json

    return json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def test_claude_unavailable_without_cli(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "claude_code_cli_path", "/nonexistent/claude.exe")
    assert claude_cli_available(settings) is False
    with _make_db() as db, pytest.raises(RuntimeUnavailableError):
        create_runtime(db, AgentRuntimeKind.CLAUDE_CODE)


# ---------------------------------------------------------------------------
# sync turn
# ---------------------------------------------------------------------------


def test_handle_turn_end_to_end_with_fake_cli(claude_settings, tmp_path) -> None:
    with _make_db() as db:
        _seed(db)
        response = ClaudeCodeAgentRuntime(db).handle_turn(_request())

        assert isinstance(response, ChatTurnResponse)
        assert response.reply == "假 Claude 回复：任务完成。"
        session = db.get(ChatSession, response.session_id)
        assert session is not None
        state = session.runtime_state_json
        assert state["thread_id"] == "claude_fake_session_1"
        assert state["runtime"] == "claude_code"
        assert state["turn_count"] == 1

        messages = list(db.exec(select(Message).where(Message.session_id == session.id)).all())
        assert [row.role for row in messages] == ["user", "assistant"]
        assistant = messages[-1]
        assert assistant.metadata_json["runtime"] == "claude_code"
        assert assistant.metadata_json["claude_session_id"] == "claude_fake_session_1"
        assert assistant.metadata_json["claude_usage"]["total_cost_usd"] == 0.001

        capture = _read_capture(tmp_path)
        argv_text = " ".join(capture["argv"])
        assert "stream-json" in capture["argv"]
        assert "--strict-mcp-config" in capture["argv"]
        assert "--append-system-prompt" in capture["argv"]
        assert "fake-claude-model" in argv_text
        assert "--resume" not in capture["argv"]
        assert "严谨、克制" in argv_text
        assert "帮我生成一份销售报表" in capture["prompt"]

        # 第二轮走 --resume
        second = ClaudeCodeAgentRuntime(db).handle_turn(
            _request(session_id=session.id, message="再加上环比数据")
        )
        assert second.reply == "假 Claude 回复：任务完成。"
        capture2 = _read_capture(tmp_path)
        assert "--resume" in capture2["argv"]
        assert "claude_fake_session_1" in capture2["argv"]
        db.refresh(session)
        assert session.runtime_state_json["turn_count"] == 2


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def test_handle_turn_stream_emits_normalized_events(claude_settings) -> None:
    with _make_db() as db:
        _seed(db)
        events = list(ClaudeCodeAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert kinds[0] == "session_created"
        assert "user_message_received" in kinds
        assert "tool_result" in kinds  # tool_use 映射为工具卡片
        assert "stream_delta" in kinds
        assert kinds[-2:] == ["stream_end", "complete"]

        complete = events[-1]["data"]
        response = ChatTurnResponse.model_validate(complete)
        assert response.reply == "假 Claude 回复：任务完成。"

        # 渐进式 text 只增量发送（"假 Claude" + " 回复：任务完成。" 两段，无重复）
        delta_text = "".join(
            event["data"]["content"] for event in events if event["event"] == "stream_delta"
        )
        assert delta_text == "假 Claude 回复：任务完成。"

        activity = list(
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id == response.session_id,
                    AgentEvent.event_type == "tool_result",
                )
            ).all()
        )
        assert activity
        assert activity[0].payload_json["toolId"] == "claude.Write"


# ---------------------------------------------------------------------------
# failure / timeout / cancellation
# ---------------------------------------------------------------------------


def test_error_result_is_a_clean_failure(claude_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "fail")
    with _make_db() as db:
        _seed(db)
        events = list(ClaudeCodeAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert "error_occurred" in kinds
        assert "complete" not in kinds
        response = ClaudeCodeAgentRuntime(db).handle_turn(_request())
        assert response.reply.startswith("Claude Code 执行失败")


def test_missing_result_is_a_clean_failure(claude_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "no_result")
    with _make_db() as db:
        _seed(db)
        events = list(ClaudeCodeAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert "error_occurred" in kinds


def test_timeout_kills_claude_process(claude_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLOW_SECONDS", "30")
    settings = get_settings()
    monkeypatch.setattr(settings, "claude_code_timeout_seconds", 1.0)
    started = time.monotonic()
    with _make_db() as db:
        _seed(db)
        events = list(ClaudeCodeAgentRuntime(db).handle_turn_stream(_request()))
    elapsed = time.monotonic() - started
    error = next(event for event in events if event["event"] == "error_occurred")
    assert error["data"]["code"] == "CLAUDE_TIMEOUT"
    assert elapsed < 15


def test_cancellation_stops_stream(claude_settings, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLOW_SECONDS", "30")
    with _make_db() as db:
        _seed(db)
        db.add(
            ChatSession(
                id="session_cancel_claude",
                tenant_id="tenant_demo",
                agent_id="agent_claude",
            )
        )
        db.commit()
        timer = threading.Timer(1.0, cancel_chat_turn, args=("session_cancel_claude", "ct_cancel"))
        timer.start()
        try:
            events = list(
                ClaudeCodeAgentRuntime(db).handle_turn_stream(
                    _request(session_id="session_cancel_claude", client_turn_id="ct_cancel")
                )
            )
        finally:
            timer.cancel()
        kinds = [event["event"] for event in events]
        assert "stream_cancelled" in kinds
        assert "complete" not in kinds
