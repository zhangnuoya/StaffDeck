from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.config import get_settings
from app.core.cancellation import cancel_chat_turn
from app.db.models import AgentEvent, AgentProfile, ChatSession, Message, ModelConfig, Tenant
from app.mcp_gateway import verify_capability_token
from app.runtimes import (
    AgentRuntimeKind,
    RuntimeUnavailableError,
    create_runtime,
    resolve_runtime_for_request,
)
from app.runtimes.adapters import codex as codex_mod
from app.runtimes.adapters.codex import (
    _PreparedTurn,
    CodexAgentRuntime,
    codex_cli_available,
)
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
        # 沙箱默认值跟随宿主平台：Linux/容器=workspace-write（-s 显式沙箱，
        # 不用 --approve-for-me——其自动批准会让越界写脱离沙箱执行），
        # Windows=bypass（完全绕过）。显式 runtime_config.sandbox 可覆盖。
        if codex_mod._DEFAULT_SANDBOX == "bypass":
            assert "--dangerously-bypass-approvals-and-sandbox" in capture["argv"]
        else:
            assert "-s" in capture["argv"]
            assert "workspace-write" in capture["argv"]
            assert "--approve-for-me" not in capture["argv"]
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


def test_model_follows_models_page_default(codex_settings, monkeypatch) -> None:
    """不设 runtime_config.model 时，-m 跟随 Models 页的租户默认模型。"""
    with _make_db() as db:
        _seed(db)
        # 清掉 runtime_config.model，让适配器走 model_for_agent 回退
        agent = db.get(AgentProfile, "agent_codex")
        agent.runtime_config_json = {}
        db.add(
            ModelConfig(
                id="model_default",
                tenant_id="tenant_demo",
                name="默认模型",
                model="staffdeck-default-model",
                is_default=True,
                enabled=True,
                api_protocol="openai_chat_completions",
                api_key_encrypted="encrypted",
                trust_status="legacy_trusted",
            )
        )
        db.commit()
        list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        # capture 由 fake CLI 写入 FAKE_CODEX_CAPTURE
        import json as _json
        from pathlib import Path as _Path

        capture_path = _Path(__import__("os").environ["FAKE_CODEX_CAPTURE"])
        capture = _json.loads(capture_path.read_text(encoding="utf-8"))
        assert "-m" in capture["argv"]
        idx = capture["argv"].index("-m")
        assert capture["argv"][idx + 1] == "staffdeck-default-model"


def test_runtime_config_model_overrides_default(codex_settings, monkeypatch) -> None:
    """runtime_config.model 优先于 model_for_agent。"""
    with _make_db() as db:
        _seed(db)  # runtime_config_json={"model": "fake-codex-model"}
        db.add(
            ModelConfig(
                id="model_default",
                tenant_id="tenant_demo",
                name="默认模型",
                model="staffdeck-default-model",
                is_default=True,
                enabled=True,
                api_protocol="openai_chat_completions",
                api_key_encrypted="encrypted",
                trust_status="legacy_trusted",
            )
        )
        db.commit()
        list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        import json as _json
        from pathlib import Path as _Path

        capture_path = _Path(__import__("os").environ["FAKE_CODEX_CAPTURE"])
        capture = _json.loads(capture_path.read_text(encoding="utf-8"))
        idx = capture["argv"].index("-m")
        assert capture["argv"][idx + 1] == "fake-codex-model"


# ---------------------------------------------------------------------------
# sandbox 参数矩阵（不跑 CLI，直测 _build_args）
# ---------------------------------------------------------------------------


def _prepared(*, runtime_config=None, is_resume: bool = False) -> _PreparedTurn:
    """构造 _build_args 所需的最小 _PreparedTurn（不落库、不跑 CLI）。"""
    return _PreparedTurn(
        request=ChatTurnRequest(tenant_id="tenant_demo", agent_id="agent_codex", message="hi"),
        chat_session=ChatSession(id="session_x", tenant_id="tenant_demo", agent_id="agent_codex"),
        user_message_id="msg_1",
        agent=None,
        runtime_config=runtime_config if runtime_config is not None else {"model": "fake-model"},
        runtime_state={"thread_id": "thread_1"} if is_resume else {},
        workspace=Path("/tmp/ws"),
        prompt="hi",
        is_resume=is_resume,
    )


def test_explicit_sandbox_modes_matrix(codex_settings, monkeypatch) -> None:
    """runtime_config.sandbox 显式配置 → 对应 CLI 参数（与宿主平台无关）。"""
    with _make_db() as db:
        runtime = CodexAgentRuntime(db)
        cases = {
            "bypass": ["--dangerously-bypass-approvals-and-sandbox"],
            "workspace-write": ["-s", "workspace-write"],
            "read-only": ["-s", "read-only"],
            "danger-full-access": ["-s", "danger-full-access"],
        }
        for mode, expected in cases.items():
            args = runtime._build_args(_prepared(runtime_config={"sandbox": mode}))
            for token in expected:
                assert token in args, f"mode={mode} 缺 {token}"
            if mode in {"read-only", "danger-full-access", "workspace-write"}:
                assert "--approve-for-me" not in args
                assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_unknown_sandbox_falls_back_to_default(codex_settings, monkeypatch) -> None:
    monkeypatch.setattr(codex_mod, "_DEFAULT_SANDBOX", "workspace-write")
    with _make_db() as db:
        args = CodexAgentRuntime(db)._build_args(_prepared(runtime_config={"sandbox": "nope"}))
    assert "-s" in args and "workspace-write" in args


def test_posix_default_sandbox_is_workspace_write(codex_settings, monkeypatch) -> None:
    """Linux/容器默认：-s workspace-write + staffdeck MCP 自动放行。"""
    monkeypatch.setattr(codex_mod, "_DEFAULT_SANDBOX", "workspace-write")
    with _make_db() as db:
        args = CodexAgentRuntime(db)._build_args(_prepared())
    assert "-s" in args
    assert "workspace-write" in args
    assert "--approve-for-me" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert 'mcp_servers.staffdeck.default_tools_approval_mode="approve"' in args
    assert any(a.startswith("mcp_servers.staffdeck.url=") for a in args)


def test_resume_uses_sandbox_mode_config_override(codex_settings, monkeypatch) -> None:
    """resume：不带 -C 与审批 flag，用 -c sandbox_mode 覆盖 thread 沙箱。"""
    monkeypatch.setattr(codex_mod, "_DEFAULT_SANDBOX", "workspace-write")
    with _make_db() as db:
        args = CodexAgentRuntime(db)._build_args(_prepared(is_resume=True))
    assert "resume" in args
    assert "thread_1" in args
    assert 'sandbox_mode="workspace-write"' in args
    assert "-C" not in args
    assert "--approve-for-me" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert 'mcp_servers.staffdeck.default_tools_approval_mode="approve"' in args


def test_default_sandbox_follows_host_platform(codex_settings) -> None:
    """默认值与宿主平台绑定：nt=bypass、其他=workspace-write。"""
    expected = "bypass" if os.name == "nt" else "workspace-write"
    assert codex_mod._DEFAULT_SANDBOX == expected
    with _make_db() as db:
        args = CodexAgentRuntime(db)._build_args(_prepared())
    if expected == "bypass":
        assert "--dangerously-bypass-approvals-and-sandbox" in args
    else:
        assert "-s" in args and "workspace-write" in args


# ---------------------------------------------------------------------------
# 知识引用：query_knowledge 结构化证据 → 回复重编号 + 消息元数据
# ---------------------------------------------------------------------------


def test_knowledge_citations_injected_from_mcp_results(codex_settings, monkeypatch) -> None:
    """codex 调用 query_knowledge 后，证据包生成 knowledge_citations 并按回复
    文本中 [n] 首次出现顺序重编号，写入 assistant message metadata。"""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "knowledge_citation")
    with _make_db() as db:
        _seed(db)
        response = CodexAgentRuntime(db).handle_turn(_request())

        messages = list(
            db.exec(select(Message).where(Message.session_id == response.session_id)).all()
        )
        assistant = messages[-1]
        citations = assistant.metadata_json.get("knowledge_citations")
        assert isinstance(citations, list) and len(citations) == 2
        # 回复文本中 [2]（请假流程）先出现 → 重编号为 [1]
        assert citations[0]["label"] == "[1]"
        assert "请假" in str(citations[0].get("title") or "")
        assert citations[1]["label"] == "[2]"
        assert "迟到" in str(citations[1].get("title") or "")
        # 回复文本标签同步重编号
        assert "根据 [1] 请假需提前一天" in assistant.content
        assert "[1] 提到迟到" not in assistant.content


def test_knowledge_citations_skipped_without_mcp_results(codex_settings) -> None:
    """没有 query_knowledge 调用的回合不生成引用元数据。"""
    with _make_db() as db:
        _seed(db)
        response = CodexAgentRuntime(db).handle_turn(_request())
        messages = list(
            db.exec(select(Message).where(Message.session_id == response.session_id)).all()
        )
        assistant = messages[-1]
        assert "knowledge_citations" not in assistant.metadata_json


# ---------------------------------------------------------------------------
# staffdeck MCP 调用去重：成功调用由网关审计记录，适配器不重复转发
# ---------------------------------------------------------------------------


def test_staffdeck_mcp_success_not_forwarded_as_tool_result(codex_settings, monkeypatch) -> None:
    """staffdeck 的成功 MCP 调用由网关侧审计记录 tool_result（execute_gateway_tool），
    适配器不再从 codex 转录重复转发，避免同一次调用显示两张工具卡片。"""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "mcp_tool_success")
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        kinds = [event["event"] for event in events]
        assert "tool_result" not in kinds
        assert kinds[-2:] == ["stream_end", "complete"]

        session_id = events[-1]["data"]["session_id"]
        activity = list(
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "tool_result",
                )
            ).all()
        )
        assert activity == []


def test_staffdeck_mcp_error_still_forwarded(codex_settings, monkeypatch) -> None:
    """JSON-RPC 级失败（如未知工具）网关不落审计事件，仍需转录兜底展示，
    否则失败调用在界面完全不可见。"""
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "mcp_tool_error")
    with _make_db() as db:
        _seed(db)
        events = list(CodexAgentRuntime(db).handle_turn_stream(_request()))
        session_id = events[-1]["data"]["session_id"]
        activity = list(
            db.exec(
                select(AgentEvent).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "tool_result",
                )
            ).all()
        )
        assert len(activity) == 1
        payload = activity[0].payload_json
        assert payload["toolId"] == "mcp.staffdeck.unknown_tool"
        assert payload["isError"] is True
