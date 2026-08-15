from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from sqlmodel import Session, select

from app.agents.branching import model_for_agent, visible_knowledge_base_ids, visible_tool_rows
from app.capabilities.contracts import CapabilityContext, GeneralSkillSummary
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

# MCP 工具名必须匹配 ^[a-zA-Z0-9_-]{1,64}$；StaffDeck 的 Tool.name 是 `{server}.{leaf}`，
# 含点号且 server 段可能重复。消毒为下划线分隔并保留一份「消毒名 -> 真实名」的映射。
_MCP_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_BUILTIN_TOOL_NAMES = {"query_knowledge", "call_tool", "run_general_skill"}


class GatewayToolError(Exception):
    """Raised for protocol-level failures (unknown tool, bad arguments)."""


def _builtin_tool_descriptors(
    general_skills: Sequence[GeneralSkillSummary] | None = None,
) -> list[dict[str, Any]]:
    # 员工绑定的已发布技能清单动态拼进 run_general_skill 描述：
    # 外部运行时（codex 等）只看到这一个入口工具，没有清单会盲试 slug。
    skill_hint = ""
    if general_skills:
        listing = ", ".join(f"{skill.slug}: {skill.name}" for skill in general_skills)
        skill_hint = f" Available skills: [{listing}]."
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
                "Invoke an enterprise tool by name. Prefer calling bound tools by their "
                "native name directly (they are listed alongside this one); this wrapper "
                "is only a fallback."
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
                + skill_hint
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


def gateway_tool_descriptors(db: Session, grant: CapabilityGrant) -> list[dict[str, Any]]:
    """Return the tools this agent can see: the three builtins plus every bound Tool
    rendered with its native name and ``inputSchema`` so external runtimes (Codex,
    Claude Code) discover and call them directly instead of via ``call_tool``.

    The ``run_general_skill`` description is personalized with the general skills
    visible to this employee, so external runtimes know which slugs exist instead
    of guessing and hitting NOT_ALLOWED.
    """
    context = CapabilityContext(
        request_id=new_id("req"),
        tenant_id=grant.tenant_id,
        agent_id=grant.agent_id,
        user_id="mcp_gateway",
        session_id=grant.session_id,
        turn_id=grant.turn_id,
        channel="mcp",
    )
    visible_skills = LocalGeneralSkillCatalog(db).list_published(context)
    descriptors = _builtin_tool_descriptors(visible_skills)
    seen = {entry["name"] for entry in descriptors}
    tools = visible_tool_rows(db, grant.tenant_id, grant.agent_id, include_inactive=False)
    for tool in tools:
        sanitized = _sanitize_tool_name(tool.name)
        if not sanitized or sanitized in _BUILTIN_TOOL_NAMES or sanitized in seen:
            if sanitized in _BUILTIN_TOOL_NAMES:
                logger.warning(
                    "tool %r sanitizes to builtin name %r; hidden from MCP list",
                    tool.name,
                    sanitized,
                )
            continue
        descriptors.append(
            {
                "name": sanitized,
                "description": str(tool.description or tool.display_name or tool.name),
                "inputSchema": tool.input_schema or {"type": "object"},
            }
        )
        seen.add(sanitized)
    return descriptors


def _sanitize_tool_name(name: str) -> str:
    """``github.create_issue`` -> ``github_create_issue``; trim to 64 chars."""
    sanitized = _MCP_NAME_RE.sub("_", str(name or "")).strip("_")
    return sanitized[:64]


def execute_gateway_tool(
    db: Session,
    grant: CapabilityGrant,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run one gateway tool with audit events; returns an MCP tool-result dict.

    ``name`` may be one of the three builtins or the sanitized name of a bound
    Tool (resolved back to its real DB name before dispatch).
    """
    builtin_runner = _BUILTIN_RUNNERS.get(name)
    if builtin_runner is not None:
        return _run_with_audit(db, grant, name, name, arguments, builtin_runner)
    native = _resolve_native_tool(db, grant, name)
    if native is None:
        raise GatewayToolError(f"unknown tool: {name}")
    real_name, display_name = native

    def runner(db: Session, grant: CapabilityGrant, arguments: dict[str, Any]) -> ToolResult:
        # ToolExecutor enforces enabled + agent binding (visible_tool_rows) again.
        return ToolExecutor(db).execute(
            grant.tenant_id,
            ToolCall(name=real_name, arguments=arguments),
            agent_id=grant.agent_id,
        )

    return _run_with_audit(db, grant, name, display_name or real_name, arguments, runner)


def _run_with_audit(
    db: Session,
    grant: CapabilityGrant,
    requested_name: str,
    activity_name: str,
    arguments: dict[str, Any],
    runner,
) -> dict[str, Any]:
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
            "tool_call": {"name": requested_name, "arguments": arguments},
        },
    )
    result = runner(db, grant, arguments)
    events.record(
        grant.tenant_id,
        grant.session_id,
        "tool_call_finished",
        {
            "tool_call_id": tool_call_id,
            "origin": GATEWAY_ORIGIN,
            "tool_call": {"name": requested_name, "arguments": arguments},
            "tool_result": result.model_dump(mode="json"),
        },
    )
    events.record(
        grant.tenant_id,
        grant.session_id,
        "tool_result",
        _activity_payload(grant, requested_name, activity_name, result, arguments, tool_call_id),
    )
    db.commit()
    return _mcp_tool_result(result)


def _resolve_native_tool(
    db: Session, grant: CapabilityGrant, requested_name: str
) -> tuple[str, str] | None:
    """Map a sanitized requested name back to the agent's bound Tool row.

    Multiple Tools may sanitize to the same name; the first bound one wins.
    Returns ``(real_name, display_name)``.
    """
    tools = visible_tool_rows(db, grant.tenant_id, grant.agent_id, include_inactive=False)
    for tool in tools:
        if _sanitize_tool_name(tool.name) == requested_name:
            return tool.name, str(tool.display_name or tool.name)
    return None


def _activity_payload(
    grant: CapabilityGrant,
    requested_name: str,
    activity_name: str,
    result: ToolResult,
    arguments: dict[str, Any],
    tool_call_id: str,
) -> dict[str, Any]:
    """Mirror AgentLoop._tool_activity_payload so frontend tool cards render as-is."""
    return {
        "toolId": requested_name,
        "toolName": activity_name,
        "rawToolName": requested_name,
        "content": result.model_dump(mode="json"),
        "isError": not result.success,
        "success": result.success,
        "toolCall": {"name": requested_name, "arguments": arguments},
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
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": text or "(empty result)"}],
        "isError": not result.success,
    }
    if result.structured is not None:
        # MCP CallToolResult.structuredContent：把结构化证据（知识检索的证据包等）
        # 透传给外部运行时（codex 会放入 item.result.structured_content）。
        payload["structuredContent"] = result.structured
    return payload


# Builtin runners keyed by their tool name; native (bound-Tool) names resolve
# at call time via _resolve_native_tool and are dispatched through ToolExecutor.
_BUILTIN_RUNNERS: dict[str, Any] = {
    "query_knowledge": lambda db, grant, args: _query_knowledge(db, grant, args),
    "call_tool": lambda db, grant, args: _call_tool(db, grant, args),
    "run_general_skill": lambda db, grant, args: _run_general_skill(db, grant, args),
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
            need_evidence_pack=True,
        )
    )
    # 文本编号与证据包对齐：只对证据包收录的 chunk 编号，避免相关性过滤造成
    # 文本 [n] 与 evidence_pack 顺序错位；证据包为空时退化为按 chunks 顺序编号。
    evidence_chunk_ids = {str(item.get("chunk_id")) for item in response.evidence_pack}
    lines: list[str] = []
    number = 0
    for chunk in response.chunks[:max_chunks]:
        content = (chunk.content or "").strip()
        if not content:
            continue
        if evidence_chunk_ids and str(chunk.id) not in evidence_chunk_ids:
            continue
        number += 1
        lines.append(f"[{number}] {content}")
    data = {
        "query": query,
        "chunk_count": len(lines),
        "text": "\n\n".join(lines) if lines else "未检索到相关内容。",
    }
    # 结构化证据包（MCP structuredContent）：与原生引擎 response_items 同构，
    # 供会话层生成 knowledge_citations（引用卡片）与前端复用渲染链路。
    structured = {
        "query": query,
        "chunks": [item.model_dump(mode="json") for item in response.chunks[:max_chunks]],
        "selected_documents": response.selected_documents,
        "selected_concepts": response.selected_concepts,
        "okf_citations": response.okf_citations,
        "evidence_pack": response.evidence_pack,
        "trace": response.route_trace or response.trace,
    }
    return ToolResult(
        tool_name="query_knowledge",
        success=True,
        data=data["text"],
        structured=structured,
        error=None,
    )


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
