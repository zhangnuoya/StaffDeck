from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import model_for_agent, visible_knowledge_base_ids
from app.capabilities.contracts import CapabilityContext
from app.capabilities.local_general_skill import LocalGeneralSkillCatalog
from app.db.models import GeneralSkill, new_id
from app.general_skills.runner import GeneralSkillRunner
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.mcp_gateway.tokens import CapabilityGrant
from app.observability.event_log import EventLog
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult

logger = logging.getLogger(__name__)

GATEWAY_ORIGIN = "mcp_gateway"


class GatewayToolError(Exception):
    """Raised for protocol-level failures (unknown tool, bad arguments)."""


def gateway_tool_descriptors() -> list[dict[str, Any]]:
    return [
        {
            "name": "query_knowledge",
            "description": (
                "Search the enterprise knowledge bases bound to this digital employee. "
                "Returns the top matching chunks with citation labels."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up"},
                    "max_chunks": {
                        "type": "integer",
                        "description": "Maximum chunks to return (1-8, default 6)",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "call_tool",
            "description": (
                "Invoke an enterprise tool (HTTP/MCP) that is bound to this digital employee, "
                "e.g. ticket systems, CRM lookups. Use list tools on the employee profile to "
                "discover names."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name"},
                    "arguments": {"type": "object", "description": "Tool input arguments"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "run_general_skill",
            "description": (
                "Run a published general skill (code-generation runner) bound to this digital "
                "employee by slug, e.g. data analysis or document generation skills."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "General skill slug"},
                    "query": {"type": "string", "description": "Task for the skill to complete"},
                },
                "required": ["slug", "query"],
            },
        },
    ]


def execute_gateway_tool(
    db: Session,
    grant: CapabilityGrant,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run one gateway tool with audit events; returns an MCP tool-result dict."""
    tool_call_id = new_id("call")
    events = EventLog(db)
    events.bind_turn(grant.turn_id)
    events.record(
        grant.tenant_id,
        grant.session_id,
        "tool_call_started",
        {
            "tool_call_id": tool_call_id,
            "origin": GATEWAY_ORIGIN,
            "tool_call": {"name": name, "arguments": arguments},
        },
    )
    if name == "query_knowledge":
        result = _query_knowledge(db, grant, arguments)
    elif name == "call_tool":
        result = _call_tool(db, grant, arguments)
    elif name == "run_general_skill":
        result = _run_general_skill(db, grant, arguments)
    else:
        raise GatewayToolError(f"unknown tool: {name}")
    events.record(
        grant.tenant_id,
        grant.session_id,
        "tool_call_finished",
        {
            "tool_call_id": tool_call_id,
            "origin": GATEWAY_ORIGIN,
            "tool_call": {"name": name, "arguments": arguments},
            "tool_result": result.model_dump(mode="json"),
        },
    )
    events.record(
        grant.tenant_id,
        grant.session_id,
        "tool_result",
        _activity_payload(grant, name, result, arguments, tool_call_id),
    )
    db.commit()
    return _mcp_tool_result(result)


def _activity_payload(
    grant: CapabilityGrant,
    name: str,
    result: ToolResult,
    arguments: dict[str, Any],
    tool_call_id: str,
) -> dict[str, Any]:
    """Mirror AgentLoop._tool_activity_payload so frontend tool cards render as-is."""
    return {
        "toolId": name,
        "toolName": name,
        "rawToolName": name,
        "content": result.model_dump(mode="json"),
        "isError": not result.success,
        "success": result.success,
        "toolCall": {"name": name, "arguments": arguments},
        "arguments": arguments,
        "toolCallId": tool_call_id,
        "origin": GATEWAY_ORIGIN,
        "agent_id": grant.agent_id,
    }


def _mcp_tool_result(result: ToolResult) -> dict[str, Any]:
    if result.success:
        text = (
            result.data
            if isinstance(result.data, str)
            else json.dumps(result.data, ensure_ascii=False, default=str)
        )
    else:
        text = result.error.message if result.error else "tool call failed"
    return {
        "content": [{"type": "text", "text": text or "(empty result)"}],
        "isError": not result.success,
    }


def _error_result(name: str, code: str, message: str) -> ToolResult:
    return ToolResult(
        tool_name=name, success=False, data=None, error=ToolError(code=code, message=message)
    )


def _query_knowledge(db: Session, grant: CapabilityGrant, arguments: dict[str, Any]) -> ToolResult:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return _error_result("query_knowledge", "INVALID_ARGUMENTS", "query 不能为空")
    knowledge_base_ids = visible_knowledge_base_ids(db, grant.tenant_id, grant.agent_id)
    if not knowledge_base_ids:
        return _error_result("query_knowledge", "NOT_ALLOWED", "当前员工未绑定任何知识库")
    try:
        max_chunks = int(arguments.get("max_chunks") or 6)
    except (TypeError, ValueError):
        max_chunks = 6
    max_chunks = max(1, min(max_chunks, 8))
    response = KnowledgeService(db).search(
        KnowledgeSearchRequest(
            tenant_id=grant.tenant_id,
            agent_id=grant.agent_id,
            query=query,
            knowledge_base_ids=knowledge_base_ids,
            max_chunks=max_chunks,
            need_evidence_pack=False,
        )
    )
    lines: list[str] = []
    for index, chunk in enumerate(response.chunks[:max_chunks], start=1):
        content = (chunk.content or "").strip()
        if content:
            lines.append(f"[{index}] {content}")
    data = {
        "query": query,
        "chunk_count": len(lines),
        "text": "\n\n".join(lines) if lines else "未检索到相关内容。",
    }
    return ToolResult(tool_name="query_knowledge", success=True, data=data["text"], error=None)


def _call_tool(db: Session, grant: CapabilityGrant, arguments: dict[str, Any]) -> ToolResult:
    name = str(arguments.get("name") or "").strip()
    if not name:
        return _error_result("call_tool", "INVALID_ARGUMENTS", "name 不能为空")
    raw_arguments = arguments.get("arguments")
    tool_arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    # ToolExecutor itself enforces enabled + agent binding (visible_tool_rows).
    return ToolExecutor(db).execute(
        grant.tenant_id,
        ToolCall(name=name, arguments=tool_arguments),
        agent_id=grant.agent_id,
    )


def _run_general_skill(
    db: Session, grant: CapabilityGrant, arguments: dict[str, Any]
) -> ToolResult:
    slug = str(arguments.get("slug") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not slug or not query:
        return _error_result("run_general_skill", "INVALID_ARGUMENTS", "slug 与 query 不能为空")
    skill = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == grant.tenant_id,
            GeneralSkill.slug == slug,
        )
    ).first()
    if not skill or skill.status != "published":
        return _error_result("run_general_skill", "NOT_FOUND", "通用技能不存在或未发布")
    catalog = LocalGeneralSkillCatalog(db)
    context = CapabilityContext(
        request_id=new_id("req"),
        tenant_id=grant.tenant_id,
        agent_id=grant.agent_id,
        user_id="mcp_gateway",
        session_id=grant.session_id,
        turn_id=grant.turn_id,
        channel="mcp",
    )
    visible_slugs = {summary.slug for summary in catalog.list_published(context)}
    if skill.slug not in visible_slugs:
        return _error_result("run_general_skill", "NOT_ALLOWED", "当前员工未绑定该通用技能")
    model_config = model_for_agent(db, grant.tenant_id, grant.agent_id, role="general_skill")
    if model_config is None:
        return _error_result("run_general_skill", "MISSING_MODEL_CONFIG", "未配置可用模型")
    response = GeneralSkillRunner().run(
        skill,
        query,
        model_config,
        user_id="mcp_gateway",
    )
    success = bool((response.structured_result or {}).get("success", True))
    return ToolResult(
        tool_name="run_general_skill",
        success=success,
        data={
            "slug": skill.slug,
            "reply": response.reply,
            "structured_result": response.structured_result,
        },
        error=None if success else ToolError(code="RUN_FAILED", message=response.reply[:200]),
    )
