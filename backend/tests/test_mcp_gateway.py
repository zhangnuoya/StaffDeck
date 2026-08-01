from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.agents.branching import agent_private_metadata
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    GeneralSkill,
    KnowledgeBase,
    MCPServer,
    Tenant,
    Tool,
    new_id,
)
from app.mcp_gateway import issue_capability_token, verify_capability_token
from app.mcp_gateway.server import router as gateway_router
from app.mcp_gateway.tokens import DEFAULT_TOKEN_TTL_SECONDS


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_client(db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(gateway_router)
    app.dependency_overrides[get_session] = lambda: db
    return TestClient(app)


def _seed(db: Session) -> None:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        AgentProfile(id="agent_codex", tenant_id="tenant_demo", name="Codex 员工", runtime="codex")
    )
    db.add(
        MCPServer(id="mcp_builtin", tenant_id="tenant_demo", name="内置演示", transport="builtin")
    )
    db.add(
        Tool(
            id="tool_echo",
            tenant_id="tenant_demo",
            name="demo.echo",
            display_name="回显",
            tool_type="mcp",
            method="POST",
            url="",
            mcp_server_id="mcp_builtin",
            config_json={"tool": "echo"},
        )
    )
    db.add(
        AgentResourceBinding(
            id=new_id("agentres"),
            tenant_id="tenant_demo",
            agent_id="agent_codex",
            resource_type="tool",
            resource_id="tool_echo",
            status="active",
            metadata_json=agent_private_metadata("agent_codex"),
        )
    )
    db.add(
        GeneralSkill(
            id="gs_clean",
            tenant_id="tenant_demo",
            slug="data-clean",
            name="数据清洗",
            skill_markdown="# 数据清洗",
            status="published",
        )
    )
    db.add(KnowledgeBase(id="kb_policy", tenant_id="tenant_demo", name="制度库"))
    db.commit()


def _token(**overrides: str) -> str:
    scope = {
        "tenant_id": "tenant_demo",
        "agent_id": "agent_codex",
        "session_id": "session_x",
        "turn_id": "msg_turn1",
    }
    scope.update(overrides)
    return issue_capability_token(**scope)


def _rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


def test_capability_token_roundtrip() -> None:
    token = _token()
    grant = verify_capability_token(token)
    assert grant is not None
    assert grant.tenant_id == "tenant_demo"
    assert grant.agent_id == "agent_codex"
    assert grant.session_id == "session_x"
    assert grant.turn_id == "msg_turn1"
    assert grant.expires_at > int(time.time())


def test_capability_token_rejects_tampering_and_expiry() -> None:
    token = _token()
    body, _signature = token.split(".", 1)
    assert verify_capability_token(f"{body}.wrongsignature") is None
    assert verify_capability_token("garbage") is None
    assert verify_capability_token("") is None
    expired = issue_capability_token(
        tenant_id="tenant_demo",
        agent_id="agent_codex",
        session_id="session_x",
        turn_id="msg_turn1",
        ttl_seconds=-1,
    )
    assert verify_capability_token(expired) is None
    future = time.time() + DEFAULT_TOKEN_TTL_SECONDS + 10
    assert verify_capability_token(token, now=future) is None


# ---------------------------------------------------------------------------
# protocol
# ---------------------------------------------------------------------------


def test_endpoint_rejects_invalid_token() -> None:
    with _make_db() as db:
        client = _make_client(db)
        response = client.post("/api/mcp/not-a-token", json=_rpc("initialize"))
        assert response.status_code == 401


def test_initialize_and_tools_list() -> None:
    with _make_db() as db:
        _seed(db)
        client = _make_client(db)
        token = _token()
        init = client.post(f"/api/mcp/{token}", json=_rpc("initialize"))
        assert init.status_code == 200
        result = init.json()["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "staffdeck-capability-gateway"

        notification = client.post(f"/api/mcp/{token}", json=_rpc("notifications/initialized"))
        assert notification.status_code == 202

        listed = client.post(f"/api/mcp/{token}", json=_rpc("tools/list"))
        tools = listed.json()["result"]["tools"]
        names = {tool["name"] for tool in tools}
        # 三件套 + _seed 绑定的 demo.echo（消毒为 demo_echo）
        assert {"query_knowledge", "call_tool", "run_general_skill"} <= names
        assert "demo_echo" in names
        echo = next(tool for tool in tools if tool["name"] == "demo_echo")
        assert echo["inputSchema"] == {"type": "object"}

        unknown = client.post(f"/api/mcp/{token}", json=_rpc("resources/list"))
        assert unknown.json()["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# tools/call: authorization + execution + audit
# ---------------------------------------------------------------------------


def _gateway_events(db: Session, event_type: str) -> list[AgentEvent]:
    return list(
        db.exec(
            select(AgentEvent).where(
                AgentEvent.session_id == "session_x",
                AgentEvent.event_type == event_type,
            )
        ).all()
    )


def test_call_tool_success_path_is_fully_real() -> None:
    with _make_db() as db:
        _seed(db)
        client = _make_client(db)
        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc(
                "tools/call",
                {
                    "name": "call_tool",
                    "arguments": {"name": "demo.echo", "arguments": {"text": "你好"}},
                },
            ),
        )
        payload = response.json()["result"]
        assert payload["isError"] is False
        assert "你好" in payload["content"][0]["text"]

        finished = _gateway_events(db, "tool_call_finished")
        assert len(finished) == 1
        detail = finished[0].payload_json
        assert detail["origin"] == "mcp_gateway"
        assert detail["tool_result"]["success"] is True
        assert detail["turn_id"] == "msg_turn1"
        assert detail["user_message_id"] == "msg_turn1"
        activity = _gateway_events(db, "tool_result")
        assert len(activity) == 1
        assert activity[0].payload_json["toolCallId"] == detail["tool_call_id"]
        assert activity[0].payload_json["origin"] == "mcp_gateway"


def test_call_tool_rejects_unbound_tool() -> None:
    with _make_db() as db:
        _seed(db)
        db.add(
            Tool(
                id="tool_other",
                tenant_id="tenant_demo",
                name="other.tool",
                tool_type="mcp",
                method="POST",
                url="",
                mcp_server_id="mcp_builtin",
                config_json={"tool": "echo"},
            )
        )
        db.commit()
        client = _make_client(db)
        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc("tools/call", {"name": "call_tool", "arguments": {"name": "other.tool"}}),
        )
        payload = response.json()["result"]
        assert payload["isError"] is True
        assert "未启用该工具" in payload["content"][0]["text"]


def test_query_knowledge_requires_bound_knowledge_base() -> None:
    with _make_db() as db:
        _seed(db)  # kb_policy exists but is NOT bound to agent_codex
        client = _make_client(db)
        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc(
                "tools/call", {"name": "query_knowledge", "arguments": {"query": "报销流程"}}
            ),
        )
        payload = response.json()["result"]
        assert payload["isError"] is True
        assert "未绑定任何知识库" in payload["content"][0]["text"]


def test_run_general_skill_rejects_unbound_or_unpublished() -> None:
    with _make_db() as db:
        _seed(db)  # data-clean published but NOT bound to agent_codex
        client = _make_client(db)
        unbound = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc(
                "tools/call",
                {"name": "run_general_skill", "arguments": {"slug": "data-clean", "query": "清洗"}},
            ),
        )
        assert unbound.json()["result"]["isError"] is True
        assert "未绑定该通用技能" in unbound.json()["result"]["content"][0]["text"]

        missing = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc(
                "tools/call",
                {"name": "run_general_skill", "arguments": {"slug": "no-such", "query": "x"}},
            ),
        )
        assert missing.json()["result"]["isError"] is True
        assert "不存在或未发布" in missing.json()["result"]["content"][0]["text"]


def test_unknown_gateway_tool_is_a_jsonrpc_error() -> None:
    with _make_db() as db:
        _seed(db)
        client = _make_client(db)
        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc("tools/call", {"name": "delete_everything", "arguments": {}}),
        )
        assert response.json()["error"]["code"] == -32602


def test_token_scopes_audit_to_its_own_session() -> None:
    with _make_db() as db:
        _seed(db)
        client = _make_client(db)
        client.post(
            f"/api/mcp/{_token(session_id='session_a')}",
            json=_rpc(
                "tools/call",
                {
                    "name": "call_tool",
                    "arguments": {"name": "demo.echo", "arguments": {"text": "a"}},
                },
            ),
        )
        other = list(db.exec(select(AgentEvent).where(AgentEvent.session_id == "session_a")).all())
        assert other
        assert {event.tenant_id for event in other} == {"tenant_demo"}


def test_native_tool_call_bypasses_call_tool_wrapper() -> None:
    """按消毒名直接调 demo_echo，不经 call_tool 包装。"""
    with _make_db() as db:
        _seed(db)
        client = _make_client(db)
        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc(
                "tools/call",
                {"name": "demo_echo", "arguments": {"text": "原生直调"}},
            ),
        )
        payload = response.json()["result"]
        assert payload["isError"] is False
        assert "原生直调" in payload["content"][0]["text"]

        finished = _gateway_events(db, "tool_call_finished")
        assert len(finished) == 1
        detail = finished[0].payload_json
        # 请求名是消毒后的 demo_echo（模型所见），工具卡片名也是它
        assert detail["tool_call"]["name"] == "demo_echo"
        assert detail["origin"] == "mcp_gateway"
        activity = _gateway_events(db, "tool_result")
        assert activity[0].payload_json["toolId"] == "demo_echo"


def test_unbound_tool_hidden_from_list_and_blocked() -> None:
    with _make_db() as db:
        _seed(db)
        # agent_codex 未绑定 other.tool
        db.add(
            Tool(
                id="tool_other",
                tenant_id="tenant_demo",
                name="other.tool",
                display_name="未绑定工具",
                tool_type="mcp",
                method="POST",
                url="",
                mcp_server_id="mcp_builtin",
                config_json={"tool": "echo"},
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            )
        )
        db.commit()
        client = _make_client(db)

        listed = client.post(f"/api/mcp/{_token()}", json=_rpc("tools/list"))
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert "other_tool" not in names

        response = client.post(
            f"/api/mcp/{_token()}",
            json=_rpc("tools/call", {"name": "other_tool", "arguments": {"text": "x"}}),
        )
        assert response.json()["error"]["code"] == -32602  # unknown tool -> INVALID_PARAMS


def test_http_tool_appears_in_list_with_schema() -> None:
    with _make_db() as db:
        _seed(db)
        db.add(
            Tool(
                id="tool_http",
                tenant_id="tenant_demo",
                name="crm.lookup",
                display_name="客户查询",
                tool_type="http",
                method="GET",
                url="https://example.test/api",
                input_schema={
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"],
                },
            )
        )
        db.add(
            AgentResourceBinding(
                id=new_id("agentres"),
                tenant_id="tenant_demo",
                agent_id="agent_codex",
                resource_type="tool",
                resource_id="tool_http",
                status="active",
                metadata_json=agent_private_metadata("agent_codex"),
            )
        )
        db.commit()
        client = _make_client(db)
        listed = client.post(f"/api/mcp/{_token()}", json=_rpc("tools/list"))
        tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
        assert "crm_lookup" in tools
        assert tools["crm_lookup"]["inputSchema"]["properties"]["customer_id"]["type"] == "string"
