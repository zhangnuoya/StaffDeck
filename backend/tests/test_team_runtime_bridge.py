"""团队协作接入 runtime bridge 的回归测试。

覆盖:成员任务执行(run_agent_turn)经 bridge 分发到 codex 等 CLI 运行时、
运行时不可用时的明确失败语义、TL 对话端点的 409 快速失败、
codex 提示词的团队上下文注入。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    AgentProfile,
    ChatSession,
    Message,
    Team,
    Tenant,
    User,
    new_id,
)
from app.runtimes.contracts import AgentRuntimeKind, RuntimeUnavailableError
from app.session.session_schema import ChatTurnResponse, SessionPublic
from app.teams import wakeup
from app.teams.service import add_member, create_team


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session, *, worker_runtime: str = "native") -> tuple[Team, AgentProfile]:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(User(id="user_admin", tenant_id="tenant_demo", username="ops", role="admin", password_hash="test"))
    db.add(AgentProfile(id="agent_tl", tenant_id="tenant_demo", name="TL"))
    db.add(
        AgentProfile(
            id="agent_worker",
            tenant_id="tenant_demo",
            name="Worker",
            runtime=worker_runtime,
        )
    )
    team = create_team(
        db,
        tenant_id="tenant_demo",
        name="增长团队",
        description=None,
        owner_user_id="user_admin",
    )
    add_member(db, team, agent_id="agent_tl", role="leader")
    add_member(db, team, agent_id="agent_worker")
    return team, db.get(AgentProfile, "agent_worker")


class _FakeCliRuntime:
    """模拟 codex/claude 适配器:流式产出 complete 事件并落一条助手消息。"""

    runtime_kind = AgentRuntimeKind.CODEX

    def __init__(self, db: Session):
        self._db = db
        self.requests: list[object] = []

    def handle_turn(self, request, *, event_sink=None):
        raise AssertionError("run_agent_turn 应使用流式接口")

    def handle_turn_stream(self, request):
        self.requests.append(request)
        self._db.add(
            Message(
                id=new_id("msg"),
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                role="assistant",
                content="codex 成员已完成团队任务。",
                metadata_json={"runtime": "codex"},
            )
        )
        self._db.commit()
        response = ChatTurnResponse(
            reply="codex 成员已完成团队任务。",
            session_id=request.session_id,
            session_state=SessionPublic(session_id=request.session_id, tenant_id=request.tenant_id),
        )
        yield {
            "event": "complete",
            "data": response.model_dump(mode="json"),
        }


def test_run_agent_turn_dispatches_cli_runtime_via_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team, worker = _seed(db, worker_runtime="codex")
        session = ChatSession(
            id="session_team_worker",
            tenant_id=team.tenant_id,
            user_id="user_admin",
            agent_id=worker.id,
            team_id=team.id,
        )
        db.add(session)
        db.commit()
        fake = _FakeCliRuntime(db)
        monkeypatch.setattr(wakeup, "resolve_runtime_for_request", lambda db_, req: fake)

        result = wakeup.run_agent_turn(
            db,
            team=team,
            agent=worker,
            session_id=session.id,
            wake_event_id="wake_1",
            message="执行任务:输出调研报告",
            interaction_mode="team_task",
        )

        assert result.reply == "codex 成员已完成团队任务。"
        assert result.message_id is not None
        dispatched = fake.requests[0]
        assert dispatched.channel == "team"
        assert dispatched.interaction_mode == "team_task"


def test_run_agent_turn_reports_unavailable_runtime_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        team, worker = _seed(db, worker_runtime="codex")

        def _unavailable(db_, request):
            raise RuntimeUnavailableError(AgentRuntimeKind.CODEX, "codex CLI 未安装")

        monkeypatch.setattr(wakeup, "resolve_runtime_for_request", _unavailable)

        with pytest.raises(RuntimeError, match="运行时不可用"):
            wakeup.run_agent_turn(
                db,
                team=team,
                agent=worker,
                session_id="session_any",
                wake_event_id="wake_1",
                message="执行任务",
                interaction_mode="team_task",
            )


def test_tl_chat_endpoint_fails_fast_when_runtime_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _test_session() as db:
        from app.api import teams as teams_api
        from app.teams.schema import TeamTLChatRequest

        team, _ = _seed(db)

        def _unavailable(db_, request):
            raise RuntimeUnavailableError(AgentRuntimeKind.CODEX, "codex CLI 未安装")

        monkeypatch.setattr(teams_api, "resolve_runtime_for_request", _unavailable)

        with pytest.raises(HTTPException) as rejected:
            teams_api.tl_chat_endpoint(
                team.id,
                TeamTLChatRequest(tenant_id="tenant_demo", message="安排任务"),
                db,
                User(
                    id="user_admin",
                    tenant_id="tenant_demo",
                    username="ops",
                    role="admin",
                    password_hash="test",
                ),
            )
        assert rejected.value.status_code == 409
        assert "AGENT_RUNTIME_UNAVAILABLE" in rejected.value.detail


def test_codex_prompt_includes_team_context_injection() -> None:
    with _test_session() as db:
        from app.runtimes.adapters.codex import CodexAgentRuntime
        from app.session.session_schema import ChatTurnRequest

        db.add(Tenant(id="tenant_demo", name="Demo"))
        agent = AgentProfile(
            id="agent_worker",
            tenant_id="tenant_demo",
            name="Worker",
            runtime="codex",
        )
        db.add(agent)
        session = ChatSession(
            id="session_ctx",
            tenant_id="tenant_demo",
            user_id="user_admin",
            agent_id=agent.id,
        )
        db.add(session)
        db.commit()

        runtime = CodexAgentRuntime(db)
        request = ChatTurnRequest(
            tenant_id="tenant_demo",
            session_id=session.id,
            agent_id=agent.id,
            user_id="user_admin",
            message="开始执行",
            context_injection="[团队上下文]\n花名册:TL、Worker\n未闭环任务:调研报告",
        )

        prompt = runtime._build_prompt(request, session, agent, "msg_x", is_resume=False)

        assert "[团队上下文]" in prompt
        assert "花名册" in prompt
        assert prompt.index("[团队上下文]") < prompt.index("[用户消息]")
