"""CLI 运行时(codex/claude_code)的记忆桥接。

与原生引擎同源同构:召回走 MemoryService.context_memories(含 agent 过滤
与读侧去重),提取复用 memory/jobs.py 的后台 job——轮次定位依赖的
user_message_received 事件与 assistant 消息 turn_id metadata 在
bookkeeping 链路中均已落库,提取器读到的对话历史与原生会话一致。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import Session, select

from app.db.models import ModelConfig
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService, memory_read
from app.observability.event_log import EventLog
from app.session.session_schema import ChatTurnRequest, StepAgentResult

logger = logging.getLogger(__name__)


def recall_memory_context(
    db: Session,
    tenant_id: str,
    user_id: str | None,
    agent_id: str | None,
) -> list[dict[str, Any]]:
    """按用户 + 员工召回长期记忆,返回 memory_read 投影(与原生引擎入参一致)。"""
    if not user_id:
        return []
    return [
        memory_read(row)
        for row in MemoryService(db).context_memories(tenant_id, user_id, agent_id=agent_id)
    ]


def render_memory_section(memories: list[dict[str, Any]], *, latest: bool = False) -> str:
    """渲染「用户记忆:」提示词段;条目压平去重与 stage_protocol._memory_text 同构。"""
    lines: list[str] = []
    seen: set[str] = set()
    for item in memories:
        content = " ".join(str(item.get("content") or "").split())
        if not content or content in seen:
            continue
        seen.add(content)
        kind = str(item.get("kind") or "").strip()
        lines.append(f"- [{kind}] {content}" if kind else f"- {content}")
    if not lines:
        return ""
    header = "最新用户记忆（每轮刷新，供参考）：" if latest else "用户记忆："
    return header + "\n" + "\n".join(lines)


def enqueue_cli_memory_capture(
    db: Session,
    events: EventLog,
    request: ChatTurnRequest,
    session_id: str,
) -> dict[str, Any] | None:
    """CLI 运行时轮后记忆提取入队,条件与事件形状对齐原生引擎的入队路径。

    CLI 轮没有 harness step/tool 结果,传空壳 StepAgentResult——稳定记忆
    的提取判断只依赖对话消息。失败仅记 memory_error,不影响对话主链路。
    """
    if request.message_visibility != "visible" or not request.user_id:
        return None
    model_config_id = _capture_model_id(db, request)
    if not model_config_id:
        events.record(
            request.tenant_id,
            session_id,
            "memory_error",
            {"message": "未找到启用的模型配置，已跳过本轮记忆提取。"},
        )
        return None
    try:
        job = enqueue_memory_capture(
            request,
            session_id,
            StepAgentResult(),
            None,
            model_config_id,
        )
    except Exception as exc:  # noqa: BLE001 - 记忆提取失败不得影响对话主链路。
        events.record(request.tenant_id, session_id, "memory_error", {"message": str(exc)})
        return None
    events.record(
        request.tenant_id,
        session_id,
        "async_job_enqueued",
        {"job_id": job.id, "job_name": job.name, "feature": "memory"},
    )
    return {"job_id": job.id, "job_name": job.name}


def _capture_model_id(db: Session, request: ChatTurnRequest) -> str | None:
    """提取模型：请求指定 > 租户默认启用模型（后台 job 按 id 复取 ModelConfig 行）。"""
    if request.model_config_id:
        row = db.get(ModelConfig, request.model_config_id)
        if row and row.tenant_id == request.tenant_id and row.enabled:
            return row.id
    row = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == request.tenant_id,
            ModelConfig.is_default == True,
            ModelConfig.enabled == True,
        )
    ).first()
    return row.id if row else None
