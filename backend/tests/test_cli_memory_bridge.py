"""CLI 运行时(codex/claude_code)记忆桥接的回归测试。

覆盖:召回与 prompt 注入(首轮/resume 轮)、memory_recalled 事件、
轮后提取入队条件(visible/模型解析/失败轮不入队)、以及提取 job
对 CLI 会话消息的轮次定位(bookkeeping 落库形状)。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    MemoryRecord,
    Message,
    ModelConfig,
    Tenant,
    User,
)
from app.llm.client import LLMClient
from app.memory import jobs as memory_jobs
from app.observability.event_log import EventLog
from app.runtimes import memory_bridge
from app.runtimes.adapters.claude_code import ClaudeCodeAgentRuntime
from app.runtimes.adapters.codex import CodexAgentRuntime
from app.session.session_schema import ChatTurnRequest, StepAgentResult


def _test_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _test_session() -> Session:
    engine = _test_engine()
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            role="member",
            password_hash="x",
        )
    )
    db.add(AgentProfile(id="agent_codex", tenant_id="tenant_demo", name="Codex", runtime="codex"))
    db.add(
        ModelConfig(
            id="model_default",
            tenant_id="tenant_demo",
            name="default",
            api_key_encrypted="",
            model="gpt-test",
            is_default=True,
            enabled=True,
        )
    )
    db.commit()


def _seed_agent_memory(db: Session, *, agent_id: str, content: str, key: str) -> None:
    db.add(
        MemoryRecord(
            tenant_id="tenant_demo",
            user_id="user_demo",
            username="demo",
            kind="preference",
            content=content,
            importance=0.8,
            metadata_json={"agent_id": agent_id, "key": key},
        )
    )
    db.commit()


def _events(db: Session, event_type: str) -> list[AgentEvent]:
    return list(
        db.exec(select(AgentEvent).where(AgentEvent.event_type == event_type)).all()
    )


class _FakeJob:
    id = "job_1"
    name = "memory.capture_turn"


# ----------------------------------------------------------------------
# recall + render
# ----------------------------------------------------------------------


def test_recall_memory_context_filters_by_agent() -> None:
    with _test_session() as db:
        _seed(db)
        _seed_agent_memory(db, agent_id="agent_codex", content="偏好深色模式", key="ui_theme")
        _seed_agent_memory(db, agent_id="agent_other", content="喜欢简洁回复", key="reply_style")

        hits = memory_bridge.recall_memory_context(db, "tenant_demo", "user_demo", "agent_codex")
        assert [item["content"] for item in hits] == ["偏好深色模式"]
        assert hits[0]["kind"] == "preference"

        assert memory_bridge.recall_memory_context(db, "tenant_demo", None, "agent_codex") == []


def test_render_memory_section_formats_and_dedupes() -> None:
    memories = [
        {"kind": "preference", "content": "偏好深色模式"},
        {"kind": "preference", "content": "偏好深色模式"},
        {"kind": "profile", "content": "称呼: 小明"},
    ]
    section = memory_bridge.render_memory_section(memories)
    assert section.splitlines()[0] == "用户记忆："
    assert "- [preference] 偏好深色模式" in section
    assert section.count("偏好深色模式") == 1
    assert "- [profile] 称呼: 小明" in section

    latest = memory_bridge.render_memory_section(memories, latest=True)
    assert latest.splitlines()[0].startswith("最新用户记忆")

    assert memory_bridge.render_memory_section([]) == ""


# ----------------------------------------------------------------------
# enqueue conditions
# ----------------------------------------------------------------------


def test_enqueue_cli_memory_capture_visible_with_default_model(monkeypatch) -> None:
    captured = {}

    def fake_enqueue(request, session_id, step_result, tool_result, model_config_id):
        captured["session_id"] = session_id
        captured["step_result"] = step_result
        captured["tool_result"] = tool_result
        captured["model_config_id"] = model_config_id
        return _FakeJob()

    monkeypatch.setattr(memory_bridge, "enqueue_memory_capture", fake_enqueue)
    with _test_session() as db:
        _seed(db)
        events = EventLog(db)
        request = ChatTurnRequest(
            tenant_id="tenant_demo", user_id="user_demo", message="记住我喜欢深色模式"
        )
        result = memory_bridge.enqueue_cli_memory_capture(db, events, request, "session_x")
        db.commit()

        assert result == {"job_id": "job_1", "job_name": "memory.capture_turn"}
        assert captured["session_id"] == "session_x"
        assert captured["model_config_id"] == "model_default"
        assert isinstance(captured["step_result"], StepAgentResult)
        assert captured["tool_result"] is None
        assert _events(db, "async_job_enqueued"), "入队后应记录 async_job_enqueued 事件"


def test_enqueue_cli_memory_capture_skips_internal_and_missing_user(monkeypatch) -> None:
    called = []

    def fake_enqueue(*args, **kwargs):
        called.append(args)
        return _FakeJob()

    monkeypatch.setattr(memory_bridge, "enqueue_memory_capture", fake_enqueue)
    with _test_session() as db:
        _seed(db)
        events = EventLog(db)
        internal = ChatTurnRequest(
            tenant_id="tenant_demo", user_id="user_demo", message="x", message_visibility="internal"
        )
        assert memory_bridge.enqueue_cli_memory_capture(db, events, internal, "session_x") is None
        anonymous = ChatTurnRequest(tenant_id="tenant_demo", user_id=None, message="x")
        assert memory_bridge.enqueue_cli_memory_capture(db, events, anonymous, "session_x") is None
        assert called == []


def test_enqueue_cli_memory_capture_reports_missing_model(monkeypatch) -> None:
    with _test_session() as db:
        _seed(db)
        default = db.get(ModelConfig, "model_default")
        default.enabled = False
        db.add(default)
        db.commit()

        events = EventLog(db)
        request = ChatTurnRequest(tenant_id="tenant_demo", user_id="user_demo", message="x")
        assert memory_bridge.enqueue_cli_memory_capture(db, events, request, "session_x") is None
        db.commit()
        errors = _events(db, "memory_error")
        assert errors and "模型配置" in errors[0].payload_json["message"]


# ----------------------------------------------------------------------
# codex adapter wiring
# ----------------------------------------------------------------------


def test_codex_prepare_turn_injects_memory_first_and_resume(monkeypatch, tmp_path) -> None:
    with _test_session() as db:
        _seed(db)
        _seed_agent_memory(db, agent_id="agent_codex", content="偏好深色模式", key="ui_theme")
        runtime = CodexAgentRuntime(db)
        monkeypatch.setattr(runtime._settings, "codex_workspace_root", str(tmp_path))

        request = ChatTurnRequest(
            tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_codex", message="你好"
        )
        prepared = runtime._prepare_turn(request)
        db.commit()

        assert "用户记忆：" in prepared.prompt
        assert "偏好深色模式" in prepared.prompt
        recalled = _events(db, "memory_recalled")
        assert recalled and recalled[0].payload_json["runtime"] == "codex"
        assert recalled[0].payload_json["memories"][0]["content"] == "偏好深色模式"

        # resume 轮:thread_id 存在后,记忆段以「最新用户记忆」前置到裸消息。
        prepared.chat_session.runtime_state_json = {"thread_id": "th_1"}
        db.add(prepared.chat_session)
        db.commit()
        resume_request = ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            agent_id="agent_codex",
            session_id=prepared.chat_session.id,
            message="继续",
        )
        resumed = runtime._prepare_turn(resume_request)
        assert resumed.is_resume
        assert resumed.prompt.startswith("最新用户记忆")
        assert resumed.prompt.endswith("继续")


def test_codex_prepare_turn_without_memory_keeps_prompt_clean(monkeypatch, tmp_path) -> None:
    with _test_session() as db:
        _seed(db)
        runtime = CodexAgentRuntime(db)
        monkeypatch.setattr(runtime._settings, "codex_workspace_root", str(tmp_path))
        request = ChatTurnRequest(
            tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_codex", message="你好"
        )
        prepared = runtime._prepare_turn(request)
        db.commit()
        assert "用户记忆" not in prepared.prompt
        assert _events(db, "memory_recalled") == []


def test_codex_finalize_enqueues_memory_only_on_success(monkeypatch, tmp_path) -> None:
    from app.runtimes.adapters import codex as codex_module

    captured = []

    def fake_enqueue(db, events, request, session_id):
        captured.append((request.message, session_id))
        return {"job_id": "job_1"}

    monkeypatch.setattr(codex_module, "enqueue_cli_memory_capture", fake_enqueue)
    with _test_session() as db:
        _seed(db)
        session = ChatSession(
            id="session_x", tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_codex"
        )
        db.add(session)
        db.commit()
        runtime = CodexAgentRuntime(db)
        monkeypatch.setattr(runtime._settings, "codex_workspace_root", str(tmp_path))

        from app.runtimes.adapters.codex import _PreparedTurn

        def _prepared() -> _PreparedTurn:
            return _PreparedTurn(
                request=ChatTurnRequest(
                    tenant_id="tenant_demo", user_id="user_demo", message="记住我喜欢深色模式"
                ),
                chat_session=session,
                user_message_id="msg_u1",
                agent=None,
                runtime_config={},
                runtime_state={},
                workspace=Path(tmp_path),
                prompt="",
                is_resume=False,
                reply="好的",
            )

        runtime._finalize(_prepared(), memory_capture=True)
        db.commit()
        assert captured == [("记住我喜欢深色模式", "session_x")]
        # assistant 消息按 bookkeeping 形状落库,供提取 job 定位轮次。
        assistant = db.exec(
            select(Message).where(Message.session_id == "session_x", Message.role == "assistant")
        ).first()
        assert assistant is not None
        assert assistant.metadata_json.get("turn_id") == "msg_u1"

        runtime._finalize(_prepared(), cancelled=True, memory_capture=True)
        runtime._finalize(_prepared(), memory_capture=False)
        assert len(captured) == 1, "取消轮与失败轮不应入队记忆提取"


# ----------------------------------------------------------------------
# claude_code adapter wiring
# ----------------------------------------------------------------------


def test_claude_prepare_turn_injects_memory(monkeypatch, tmp_path) -> None:
    with _test_session() as db:
        _seed(db)
        _seed_agent_memory(db, agent_id="agent_codex", content="偏好深色模式", key="ui_theme")
        runtime = ClaudeCodeAgentRuntime(db)
        monkeypatch.setattr(runtime._settings, "codex_workspace_root", str(tmp_path))

        request = ChatTurnRequest(
            tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_codex", message="你好"
        )
        prepared = runtime._prepare_turn(request)
        db.commit()
        assert prepared.prompt.startswith("用户记忆：")
        assert "偏好深色模式" in prepared.prompt
        recalled = _events(db, "memory_recalled")
        assert recalled and recalled[0].payload_json["runtime"] == "claude_code"


# ----------------------------------------------------------------------
# capture job reads CLI session messages (turn 定位链路)
# ----------------------------------------------------------------------


def test_capture_job_reads_cli_session_turn_messages(monkeypatch) -> None:
    engine = _test_engine()
    SQLModel.metadata.create_all(engine)

    captured_payload = {}

    def fake_init(self, model_config):
        return None

    def fake_generate_json(self, system_prompt, payload):
        captured_payload.update(payload)
        return {"memories": [], "updated_summary": ""}

    monkeypatch.setattr(LLMClient, "__init__", fake_init)
    monkeypatch.setattr(LLMClient, "generate_json", fake_generate_json)
    monkeypatch.setattr(memory_jobs, "engine", engine)

    with Session(engine) as db:
        _seed(db)
        session = ChatSession(
            id="session_cli", tenant_id="tenant_demo", user_id="user_demo", agent_id="agent_codex"
        )
        db.add(session)
        db.commit()

        # 模拟 CLI 适配器的落库形状:两条 user/assistant 消息 + 轮次事件。
        user_message = Message(
            id="msg_u1",
            tenant_id="tenant_demo",
            session_id="session_cli",
            role="user",
            content="记住我喜欢深色模式",
            metadata_json={},
        )
        db.add(user_message)
        db.add(
            Message(
                tenant_id="tenant_demo",
                session_id="session_cli",
                role="assistant",
                content="好的，我会记住。",
                metadata_json={"turn_id": "msg_u1", "user_message_id": "msg_u1"},
            )
        )
        db.commit()
        events = EventLog(db)
        events.bind_turn("msg_u1", "turn_cli_1")
        events.record(
            "tenant_demo",
            "session_cli",
            "user_message_received",
            {"message_id": "msg_u1", "client_turn_id": "turn_cli_1"},
        )
        db.commit()

        request = ChatTurnRequest(
            tenant_id="tenant_demo",
            user_id="user_demo",
            session_id="session_cli",
            client_turn_id="turn_cli_1",
            message="记住我喜欢深色模式",
        )
        memory_jobs.run_memory_capture_job(
            {
                "request": request.model_dump(mode="json"),
                "session_id": "session_cli",
                "step_result": StepAgentResult().model_dump(mode="json"),
                "tool_result": None,
                "model_config_id": "model_default",
            }
        )

        messages = captured_payload["conversation_context"]["messages"]
        assert messages == [
            {"role": "user", "content": "记住我喜欢深色模式"},
            {"role": "assistant", "content": "好的，我会记住。"},
        ], "提取 job 应按 CLI 落库形状定位本轮对话"

        # 提取器输入形状完整(conversation_context/existing_memories/step_result),
        # 空提取不落 memory_saved,异常也不得抛出请求方。
        assert "existing_memories" in captured_payload
        assert "step_result" in captured_payload
