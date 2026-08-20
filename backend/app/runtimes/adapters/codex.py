from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from fastapi import HTTPException
from sqlmodel import Session, select

from app import paths
from app.agents.branching import model_for_agent
from app.config import get_settings
from app.core.agent_identity_prompt import AgentIdentityPrompt
from app.core.cancellation import clear_chat_turn_cancelled, is_chat_turn_cancelled
from app.core.conversation_projection import ConversationProjection
from app.db.models import AgentProfile, ChatSession, Message, utc_now
from app.harness import HarnessArtifactAccessError, normalize_harness_artifact_path
from app.harness.artifacts import (
    HarnessWorkspaceSnapshot,
    is_noise_artifact_path,
    publish_harness_artifacts,
    snapshot_harness_workspace,
)
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    knowledge_citations_from_results,
)
from app.mcp_gateway import issue_capability_token
from app.observability.event_log import EventLog
from app.runtimes import bookkeeping
from app.runtimes.adapters._cli_common import kill_process_tree, parse_jsonl, reply_chunks
from app.runtimes.contracts import AgentRuntimeKind
from app.session.helpers import public_session
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0
_HISTORY_MESSAGE_LIMIT = 10
_CODEX_PROGRESS_PHASE = "codex_progress"

# 沙箱等级白名单 + 宿主默认值。单容器/服务器（Linux）默认 workspace-write：
# landlock 把写限制在会话目录内——物理挡住 ~/.codex 用户级 MCP/skill 配置写入
# 与跨会话目录写（多员工共用一个 codex home，见 docker/entrypoint-codex.sh）。
# Windows 宿主默认 bypass：codex 的 Windows 沙箱会拦 pwsh，无终端场景不可用。
# runtime_config.sandbox 显式配置优先。
# 注意：Linux 下不要用 --approve-for-me 做写隔离——实测 codex 0.147.0 的
# 「自动批准」对越界写命令是批准后在沙箱外执行（写 workspace 根/其他 session
# 目录均成功），必须显式 -s workspace-write 才由 landlock 真实拦截（Read-only）。
_ALLOWED_SANDBOX_MODES = {"bypass", "workspace-write", "read-only", "danger-full-access"}
_DEFAULT_SANDBOX = "bypass" if os.name == "nt" else "workspace-write"

_CODEX_CLI_NOT_FOUND = "未找到 Codex CLI。请先安装 codex-cli 或在设置中配置 codex_cli_path。"


@dataclass
class _PreparedTurn:
    request: ChatTurnRequest
    chat_session: ChatSession
    user_message_id: str
    agent: AgentProfile | None
    runtime_config: dict[str, Any]
    runtime_state: dict[str, Any]
    workspace: Path
    prompt: str
    is_resume: bool
    reply: str = ""
    usage: dict[str, Any] | None = None
    failed: bool = False
    response: ChatTurnResponse | None = None
    # 本轮 staffdeck.query_knowledge 调用返回的结构化证据
    # （MCP structuredContent，与原生引擎 response_items 同构），
    # _finalize 时据此生成回复引用的 knowledge_citations。
    knowledge_results: list[dict[str, Any]] = field(default_factory=list)
    # 产物下载适配：turn 开始时的工作区快照 + codex file_change 事件
    # 报告的文件路径，_finalize 时并集登记为 harness_artifacts。
    workspace_before: HarnessWorkspaceSnapshot | None = None
    changed_paths: list[str] = field(default_factory=list)


def _collect_turn_artifacts(
    workspace: Path,
    workspace_before: HarnessWorkspaceSnapshot | None,
    changed_paths: list[str],
    turn_no: int,
) -> list[dict[str, Any]]:
    """把本回合 codex 产物登记为与原生引擎同构的 harness_artifacts 元数据。

    信号并集:file_change 事件(codex 自己改的文件,精确)+ 工作区前后
    快照 diff(兜底捕获 shell 重定向/脚本写出的文件);排噪后仅保留
    仍存在的常规文件,sha256/size 与原生同一登记函数保证同构。
    """

    candidates: list[str] = []

    def add(raw: str) -> None:
        try:
            path = normalize_harness_artifact_path(raw)
        except HarnessArtifactAccessError:
            return
        if path and path not in candidates and not is_noise_artifact_path(path):
            candidates.append(path)

    # codex file_change 事件报告的是工作区绝对路径,相对化后才能通过
    # normalize 校验(绝对路径会被产物路径安全策略拒绝)。
    workspace_prefix = f"{workspace}/"
    for raw in changed_paths:
        add(raw.removeprefix(workspace_prefix))
    if workspace_before is not None:
        try:
            after = snapshot_harness_workspace(workspace)
        except OSError:
            after = None
        if after is not None:
            for raw in after:
                if workspace_before.get(raw) != after[raw]:
                    add(raw)
    declarations: list[dict[str, str]] = []
    for path in candidates:
        try:
            if (workspace / path).is_file():
                declarations.append({"path": path})
        except OSError:
            continue
    if not declarations:
        return []
    try:
        published = publish_harness_artifacts(
            workspace,
            f"codex-turn-{turn_no}",
            declarations,
            operation="codex_turn",
        )
    except (HarnessArtifactAccessError, OSError) as exc:
        logger.warning(
            "codex 产物登记失败(权限/安全校验未通过) workspace=%s: %s", workspace, exc
        )
        return []
    for entry in published:
        entry.setdefault("display_name", PurePath(str(entry.get("path"))).name)
        entry["source"] = "codex"
    return published


class CodexAgentRuntime:
    """Agent runtime executing turns through the local Codex CLI (`codex exec`).

    Turn lifecycle mirrors AgentLoop so every consumer (SSE relay, channels,
    scheduled tasks, traces) works unchanged: sessions/messages live in the
    StaffDeck database, Codex's own thread id is kept in
    `sessions.runtime_state_json` for resume, and JSONL events are normalized
    into the existing stream event vocabulary.
    """

    runtime_kind = AgentRuntimeKind.CODEX

    def __init__(self, db: Session) -> None:
        self._db = db
        self._settings = get_settings()
        self._events = EventLog(db)

    # ------------------------------------------------------------------
    # public surface
    # ------------------------------------------------------------------

    def handle_turn(
        self, request: ChatTurnRequest, *, event_sink: Callable[[str, dict[str, Any]], None] | None = None
    ) -> ChatTurnResponse:
        # event_sink 是原生引擎的流式回调;CLI 运行时经自有转录通道落 AgentEvent,忽略该参数。
        prepared = self._prepare_turn(request)
        for _ in self._drive(prepared, streaming=False):
            pass
        assert prepared.response is not None
        return prepared.response

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, Any]]:
        prepared = self._prepare_turn(request)
        yield from self._drive(prepared, streaming=True)

    # ------------------------------------------------------------------
    # preparation
    # ------------------------------------------------------------------

    def _prepare_turn(self, request: ChatTurnRequest) -> _PreparedTurn:
        chat_session = bookkeeping.get_or_create_session(self._db, request)
        bookkeeping.mark_session_running(self._db, chat_session)
        user_message = bookkeeping.append_message(
            self._db,
            request.tenant_id,
            chat_session.id,
            "user",
            request.message,
            metadata=ConversationProjection.user_message_metadata(request),
        )
        self._events.bind_turn(user_message.id, request.client_turn_id)
        self._events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {
                "message_id": user_message.id,
                "client_turn_id": request.client_turn_id,
                "message": request.message,
                "channel": request.channel,
                "user_id": request.user_id,
            },
        )
        agent = self._db.get(AgentProfile, chat_session.agent_id or request.agent_id or "")
        runtime_config = dict(agent.runtime_config_json or {}) if agent else {}
        runtime_state = dict(chat_session.runtime_state_json or {})
        workspace = self._ensure_workspace(chat_session.id)
        try:
            workspace_before = snapshot_harness_workspace(workspace)
        except OSError:
            workspace_before = None
        is_resume = bool(runtime_state.get("thread_id"))
        prompt = self._build_prompt(request, chat_session, agent, user_message.id, is_resume)
        self._db.commit()
        self._db.refresh(chat_session)
        self._db.refresh(user_message)
        return _PreparedTurn(
            request=request,
            chat_session=chat_session,
            user_message_id=user_message.id,
            agent=agent,
            runtime_config=runtime_config,
            runtime_state=runtime_state,
            workspace=workspace,
            prompt=prompt,
            is_resume=is_resume,
            workspace_before=workspace_before,
        )

    def _ensure_workspace(self, session_id: str) -> Path:
        root = (self._settings.codex_workspace_root or "").strip()
        base = Path(root) if root else paths.user_data_dir() / "workspaces"
        workspace = base / session_id
        workspace.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=workspace,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                logger.debug("git init skipped for workspace %s", workspace)
        return workspace

    def _build_prompt(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        agent: AgentProfile | None,
        user_message_id: str,
        is_resume: bool,
    ) -> str:
        message = request.message
        attachment_text = self._attachment_text(request)
        if attachment_text:
            message = f"{message}\n\n{attachment_text}"
        if is_resume:
            return message
        sections: list[str] = []
        if agent:
            sections.append(AgentIdentityPrompt.render(agent))
        sections.append(
            "你能通过名为 staffdeck 的 MCP 工具集访问该员工绑定的企业能力。"
            "业务工具已按原生名称在工具清单中列出，请直接按名调用（不要用 call_tool 包装）；"
            "知识库检索用 query_knowledge，通用技能用 run_general_skill。不要编造企业内部信息。"
        )
        sections.append(
            "query_knowledge 返回的片段是临时证据：回答必须基于这些内容，"
            "引用来源时使用片段对应的 [n] 编号标注（例如「根据 [1]」）；"
            "证据不足时不得编造企业政策、流程或文档事实，可再次调用 query_knowledge 补充检索。"
        )
        history = self._history_text(chat_session, user_message_id)
        if history:
            sections.append(f"[对话历史]\n{history}")
        sections.append(f"[用户消息]\n{message}")
        return "\n\n".join(section for section in sections if section.strip())

    def _history_text(self, chat_session: ChatSession, user_message_id: str) -> str:
        rows = list(
            self._db.exec(
                select(Message)
                .where(Message.session_id == chat_session.id)
                .order_by(Message.created_at.desc())
                .limit(_HISTORY_MESSAGE_LIMIT + 1)
            ).all()
        )
        lines: list[str] = []
        for row in reversed(rows):
            if row.id == user_message_id:
                continue
            speaker = "用户" if row.role == "user" else "员工"
            content = (row.content or "").strip()
            if content:
                lines.append(f"{speaker}：{content[:500]}")
        return "\n".join(lines[-_HISTORY_MESSAGE_LIMIT:])

    @staticmethod
    def _attachment_text(request: ChatTurnRequest) -> str:
        parts: list[str] = []
        for attachment in request.attachments:
            if attachment.kind == "text" and attachment.text:
                parts.append(f"[附件 {attachment.filename}]\n{attachment.text[:4000]}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # codex invocation
    # ------------------------------------------------------------------

    def _resolve_model(self, prepared: _PreparedTurn) -> str:
        """优先级：runtime_config 覆盖 > AgentModelBinding/默认 > 全局配置 > 不传。"""
        override = str(prepared.runtime_config.get("model") or "").strip()
        if override:
            return override
        agent_id = prepared.chat_session.agent_id or prepared.request.agent_id
        if agent_id:
            try:
                resolved = model_for_agent(self._db, prepared.request.tenant_id, agent_id)
                if resolved and resolved.model:
                    return resolved.model
            except HTTPException:
                pass  # 模型未验证等：回退全局配置
        return str(self._settings.codex_default_model or "").strip()

    def _resolve_sandbox(self, prepared: _PreparedTurn) -> str:
        """沙箱等级：runtime_config.sandbox 显式配置优先，否则按宿主平台默认。

        默认 workspace-write（Linux/容器）意味着写被 landlock 限制在会话目录内，
        用户级 ~/.codex 配置（MCP/skill）与其他员工的会话目录都无法被写入，
        员工只能在项目级（自己的 .codex 工作空间配置）安装能力。
        """
        configured = str(prepared.runtime_config.get("sandbox") or "").strip()
        if configured in _ALLOWED_SANDBOX_MODES:
            return configured
        if configured:
            logger.warning("忽略未知 sandbox 模式 %r，回退默认 %s", configured, _DEFAULT_SANDBOX)
        return _DEFAULT_SANDBOX

    def _build_args(self, prepared: _PreparedTurn) -> list[str]:
        args = [*_codex_base_command(self._settings), "exec"]
        if prepared.is_resume:
            args += ["resume", str(prepared.runtime_state["thread_id"])]
        args += ["-", "--json", "--skip-git-repo-check"]
        if not prepared.is_resume:
            args += ["-C", str(prepared.workspace)]
        sandbox = self._resolve_sandbox(prepared)
        if sandbox == "bypass":
            # 显式 bypass（或 Windows 默认）：完全关闭审批与沙箱。
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        elif prepared.is_resume:
            # resume 不接受审批/沙箱 flag（随 thread 持久化），仅 -c 覆盖沙箱模式。
            args += ["-c", f'sandbox_mode="{sandbox}"']
        else:
            # 显式 -s 沙箱模式（不带审批 flag）：headless 下命令直接执行、不被取消，
            # 越界写由 landlock 真实拦截为 Read-only file system。
            # 不用 --approve-for-me：其「自动批准」会让越界写命令脱离沙箱执行
            # （批准即放行），实测写 workspace 根/其他 session 目录均成功，
            # 写隔离名存实亡（codex 0.147.0，2026-08-15 容器实测）。
            args += ["-s", sandbox]
        model = self._resolve_model(prepared)
        if model:
            args += ["-m", model]
        token = issue_capability_token(
            tenant_id=prepared.request.tenant_id,
            agent_id=prepared.chat_session.agent_id or prepared.request.agent_id or "",
            session_id=prepared.chat_session.id,
            turn_id=prepared.user_message_id,
        )
        gateway_url = f"{self._settings.normalized_tool_base_url}/api/mcp/{token}"
        args += ["-c", f'mcp_servers.staffdeck.url="{gateway_url}"']
        # staffdeck MCP 工具按 per-server 审批默认值自动放行：无终端下无人审批
        # 会取消 MCP 工具调用（resume 实测），approve 保证业务工具可用，
        # 能力过滤仍由 gateway 的 capability token 按员工兜底。非 staffdeck 的
        # MCP server（如用户级安装的）不受此配置影响，在沙箱模式下其工具调用
        # 仍会被取消——顺带废掉绕过文件锁装入的用户级 MCP 的可用性。
        args += ["-c", 'mcp_servers.staffdeck.default_tools_approval_mode="approve"']
        return args

    # ------------------------------------------------------------------
    # drive loop
    # ------------------------------------------------------------------

    def _drive(self, prepared: _PreparedTurn, streaming: bool) -> Iterator[dict[str, Any]]:
        session = prepared.chat_session
        if streaming:
            yield self._event(
                prepared,
                "session_created",
                {
                    "newSessionId": session.id,
                    "sessionId": session.id,
                },
            )
            yield self._event(
                prepared,
                "user_message_received",
                {
                    "message_id": prepared.user_message_id,
                    "client_turn_id": prepared.request.client_turn_id,
                    "message": prepared.request.message,
                    "channel": prepared.request.channel,
                    "user_id": prepared.request.user_id,
                    **self._turn_binding(prepared),
                },
            )
        try:
            yield from self._run_codex(prepared, streaming)
        except Exception as exc:
            logger.exception("codex turn failed (session=%s)", session.id)
            yield from self._fail_turn(prepared, "CODEX_ADAPTER_ERROR", str(exc)[:300], streaming)
        finally:
            clear_chat_turn_cancelled(session.id, prepared.user_message_id)
            if prepared.request.client_turn_id:
                clear_chat_turn_cancelled(session.id, prepared.request.client_turn_id)

    def _run_codex(self, prepared: _PreparedTurn, streaming: bool) -> Iterator[dict[str, Any]]:
        args = self._build_args(prepared)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=prepared.workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                # Linux 下新会话独立进程组：取消/超时经 killpg 清理整个包装链
                # （如单容器双用户分离的 sudo -u appuser codex）。
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            yield from self._fail_turn(
                prepared, "CODEX_CLI_NOT_FOUND", _CODEX_CLI_NOT_FOUND, streaming
            )
            return
        assert process.stdin is not None
        try:
            process.stdin.write(prepared.prompt)
            process.stdin.close()
        except OSError:
            pass

        output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def _reader(stream: Any, tag: str) -> None:
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put((tag, line))
            finally:
                output_queue.put((tag, None))

        threading.Thread(target=_reader, args=(process.stdout, "out"), daemon=True).start()
        threading.Thread(target=_reader, args=(process.stderr, "err"), daemon=True).start()

        timeout = float(self._settings.codex_timeout_seconds or 900.0)
        deadline = time.monotonic() + timeout
        last_heartbeat = time.monotonic()
        stdout_open = True
        stderr_tail: list[str] = []
        pending_agent_text: str | None = None
        saw_agent_message = False
        failure: tuple[str, str] | None = None
        cancelled = False

        while True:
            if streaming and self._is_cancelled(prepared):
                cancelled = True
                break
            if time.monotonic() > deadline:
                failure = ("CODEX_TIMEOUT", f"Codex 执行超过 {timeout:g} 秒未结束")
                break
            try:
                tag, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                if streaming and time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                    last_heartbeat = time.monotonic()
                    yield self._status(prepared, "responding", "Codex 正在执行…", streaming)
                if process.poll() is not None and output_queue.empty():
                    break
                continue
            if line is None:
                if tag == "out":
                    stdout_open = False
                if not stdout_open and process.poll() is not None:
                    break
                continue
            if tag == "err":
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-20]
                continue
            event = parse_jsonl(line)
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                thread_id = str(event.get("thread_id") or "").strip()
                if thread_id:
                    prepared.runtime_state["thread_id"] = thread_id
                    self._persist_runtime_state(prepared)
            elif event_type == "item.completed":
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                item_type = item.get("type")
                if item_type == "agent_message":
                    saw_agent_message = True
                    text = str(item.get("text") or "")
                    if pending_agent_text is not None and streaming:
                        yield self._status(
                            prepared, _CODEX_PROGRESS_PHASE, pending_agent_text[:160], streaming
                        )
                    pending_agent_text = text
                    if streaming:
                        for chunk in reply_chunks(text):
                            yield self._event(
                                prepared, "stream_delta", {"content": chunk}, persist=True
                            )
                elif item_type == "reasoning":
                    if streaming:
                        reasoning = str(item.get("text") or "").strip()
                        if reasoning:
                            yield self._status(prepared, "stepping", reasoning[:120], streaming)
                elif item_type in {
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "web_search",
                }:
                    if item_type == "mcp_tool_call":
                        self._collect_knowledge_item(prepared, item)
                        # staffdeck MCP 调用由网关侧审计记录 tool_result（execute_gateway_tool），
                        # 不再转发 codex 转录的成功调用，避免同一次调用在界面显示两张工具卡片；
                        # 失败调用（JSON-RPC 级错误）网关不落审计事件，仍需转录兜底展示。
                        if str(item.get("server") or "") == "staffdeck" and not item.get("error"):
                            continue
                    if item_type == "file_change":
                        self._collect_changed_paths(prepared, item)
                    yield self._event(
                        prepared,
                        "tool_result",
                        _tool_activity_payload(item_type, item),
                        persist=True,
                        streaming=streaming,
                    )
            elif event_type == "turn.completed":
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
                prepared.usage = usage
            elif event_type in {"error", "turn.failed"}:
                message = str(event.get("message") or event.get("error") or "Codex 执行失败")
                failure = ("CODEX_EXEC_FAILED", message[:300])
                break

        if cancelled:
            kill_process_tree(process)
            yield self._event(
                prepared,
                "stream_cancelled",
                {"phase": "cancelled", "text": "已停止生成", **self._turn_binding(prepared)},
                persist=True,
                streaming=True,
            )
            prepared.reply = "已停止生成"
            self._finalize(prepared, cancelled=True)
            return

        if failure is None and process.poll() not in (0, None) and not saw_agent_message:
            tail = "\n".join(stderr_tail[-5:])[:300]
            failure = ("CODEX_EXEC_FAILED", tail or f"codex 进程退出码 {process.returncode}")
        if failure is None and not saw_agent_message:
            tail = "\n".join(stderr_tail[-5:])[:300]
            failure = ("CODEX_EMPTY_REPLY", tail or "Codex 未返回任何回复")

        if failure is not None:
            kill_process_tree(process)
            yield from self._fail_turn(prepared, failure[0], failure[1], streaming)
            return

        process.wait(timeout=5)
        prepared.reply = (pending_agent_text or "").strip() or "（Codex 未返回文本回复）"
        if streaming and prepared.reply != (pending_agent_text or ""):
            yield self._event(prepared, "stream_replace", {"content": prepared.reply}, persist=True)
        self._finalize(prepared)
        if streaming:
            yield self._event(prepared, "stream_end", {}, persist=True)
            yield self._event(
                prepared,
                "complete",
                {
                    **prepared.response.model_dump(mode="json"),
                    **self._turn_binding(prepared),
                },
            )

    # ------------------------------------------------------------------
    # turn outcomes
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_changed_paths(prepared: _PreparedTurn, item: dict[str, Any]) -> None:
        """记录 codex file_change 事件报告的本回合改动文件（供产物登记）。"""
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        for change in changes:
            if not isinstance(change, dict):
                continue
            raw_path = str(change.get("path") or "").strip()
            if not raw_path:
                continue
            kind = str(change.get("kind") or change.get("type") or "").lower()
            if kind in {"delete", "removed"}:
                continue
            if raw_path not in prepared.changed_paths:
                prepared.changed_paths.append(raw_path)

    @staticmethod
    def _collect_knowledge_item(prepared: _PreparedTurn, item: dict[str, Any]) -> None:
        """提取 staffdeck.query_knowledge 调用的结构化证据包供回复引用。"""
        if str(item.get("server") or "") != "staffdeck":
            return
        if str(item.get("tool") or "") != "query_knowledge":
            return
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        structured = result.get("structured_content") or result.get("structuredContent")
        if isinstance(structured, dict) and (
            structured.get("evidence_pack") or structured.get("chunks")
        ):
            prepared.knowledge_results.append(structured)

    def _finalize(self, prepared: _PreparedTurn, cancelled: bool = False) -> None:
        session = prepared.chat_session
        state = dict(prepared.runtime_state)
        state["runtime"] = AgentRuntimeKind.CODEX.value
        state["workspace"] = str(prepared.workspace)
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        session.runtime_state_json = state
        self._db.add(session)
        extra_metadata: dict[str, Any] = {"runtime": AgentRuntimeKind.CODEX.value}
        if prepared.usage:
            extra_metadata["codex_usage"] = prepared.usage
        if state.get("thread_id"):
            extra_metadata["codex_thread_id"] = state["thread_id"]
        if cancelled:
            extra_metadata["status"] = "cancelled"
        # 知识引用：与原生引擎同构——从结构化证据包提取引用、按回复文本中
        # [n] 首次出现顺序重编号，写入消息 metadata 供前端渲染引用卡片。
        if prepared.knowledge_results and not cancelled:
            citations = knowledge_citations_from_results(prepared.knowledge_results)
            if citations:
                compacted_reply, compacted = compact_knowledge_citation_labels(
                    prepared.reply, citations
                )
                prepared.reply = compacted_reply
                extra_metadata["knowledge_citations"] = compacted
        # 产物下载适配:与原生引擎同构的 harness_artifacts 元数据,
        # 前端消息卡片与下载端点无需感知运行时差异。
        if not cancelled:
            artifacts = _collect_turn_artifacts(
                prepared.workspace,
                prepared.workspace_before,
                prepared.changed_paths,
                int(state.get("turn_count") or 0),
            )
            if artifacts:
                extra_metadata["harness_artifacts"] = artifacts
        bookkeeping.finalize_simple_turn(
            self._db,
            self._events,
            session,
            prepared.request.tenant_id,
            prepared.reply,
            source_message=prepared.request.message,
            user_message_id=prepared.user_message_id,
            extra_metadata=extra_metadata,
        )
        self._db.commit()
        self._db.refresh(session)
        prepared.response = ChatTurnResponse(
            reply=prepared.reply,
            session_id=session.id,
            session_state=public_session(session),
        )

    def _fail_turn(
        self,
        prepared: _PreparedTurn,
        code: str,
        message: str,
        streaming: bool,
    ) -> Iterator[dict[str, Any]]:
        self._events.record(
            prepared.request.tenant_id,
            prepared.chat_session.id,
            "error_occurred",
            {"code": code, "message": message},
        )
        self._db.commit()
        prepared.reply = f"Codex 执行失败:{message}"
        self._finalize(prepared)
        if streaming:
            yield self._event(
                prepared,
                "error_occurred",
                {"code": code, "message": message, **self._turn_binding(prepared)},
            )

    # ------------------------------------------------------------------
    # event helpers
    # ------------------------------------------------------------------

    def _turn_binding(self, prepared: _PreparedTurn) -> dict[str, Any]:
        return {"turn_id": prepared.user_message_id, "user_message_id": prepared.user_message_id}

    def _event(
        self,
        prepared: _PreparedTurn,
        kind: str,
        payload: dict[str, Any],
        *,
        persist: bool = False,
        streaming: bool = True,
    ) -> dict[str, Any]:
        if persist and streaming:
            self._events.record(prepared.request.tenant_id, prepared.chat_session.id, kind, payload)
            self._db.commit()
        data = {
            "kind": kind,
            "sessionId": prepared.chat_session.id,
            "timestamp": utc_now().isoformat(),
            "provider": "skill",
            **payload,
        }
        return {"event": kind, "data": data}

    def _status(
        self,
        prepared: _PreparedTurn,
        phase: str,
        text: str,
        streaming: bool,
    ) -> dict[str, Any]:
        payload = {"phase": phase, "text": text, **self._turn_binding(prepared)}
        if streaming:
            self._events.record(
                prepared.request.tenant_id, prepared.chat_session.id, "stream_status", payload
            )
            self._db.commit()
        return self._event(prepared, "status", payload, streaming=streaming)

    def _is_cancelled(self, prepared: _PreparedTurn) -> bool:
        session_id = prepared.chat_session.id
        if is_chat_turn_cancelled(session_id, prepared.user_message_id):
            return True
        client_turn_id = prepared.request.client_turn_id
        return bool(client_turn_id) and is_chat_turn_cancelled(session_id, str(client_turn_id))

    def _persist_runtime_state(self, prepared: _PreparedTurn) -> None:
        session = prepared.chat_session
        state = dict(prepared.runtime_state)
        state["runtime"] = AgentRuntimeKind.CODEX.value
        state["workspace"] = str(prepared.workspace)
        session.runtime_state_json = state
        session.updated_at = utc_now()
        self._db.add(session)
        self._db.commit()


# ----------------------------------------------------------------------
# module helpers
# ----------------------------------------------------------------------


def codex_cli_available(settings: Any | None = None) -> bool:
    settings = settings or get_settings()
    configured = (settings.codex_cli_path or "").strip()
    if configured:
        return Path(configured).exists()
    return shutil.which("codex") is not None


def _codex_base_command(settings: Any) -> list[str]:
    raw = (settings.codex_cli_path or "").strip() or (shutil.which("codex") or "codex")
    lowered = raw.lower()
    if lowered.endswith(".py"):
        return [sys.executable, raw]
    if os.name == "nt" and lowered.endswith((".cmd", ".bat")):
        return ["cmd", "/c", raw]
    return [raw]


def _tool_activity_payload(item_type: str, item: dict[str, Any]) -> dict[str, Any]:
    """Map a codex item onto the AgentLoop tool_result activity shape."""
    if item_type == "command_execution":
        command = str(item.get("command") or "")
        exit_code = item.get("exit_code")
        success = exit_code in (0, None)
        content = {
            "command": command,
            "exit_code": exit_code,
            "output": str(item.get("aggregated_output") or "")[:2000],
        }
        return {
            "toolId": "codex.command",
            "toolName": "执行命令",
            "rawToolName": "codex.command",
            "content": content,
            "arguments": {"command": command},
            "success": success,
            "isError": not success,
        }
    if item_type == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        return {
            "toolId": "codex.file_change",
            "toolName": "修改文件",
            "rawToolName": "codex.file_change",
            "content": {"changes": changes},
            "arguments": {"changes": changes},
            "success": True,
            "isError": False,
        }
    if item_type == "mcp_tool_call":
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "")
        label = f"{server}.{tool}".strip(".")
        return {
            "toolId": f"mcp.{label}",
            "toolName": label,
            "rawToolName": label,
            "content": item.get("result") if "result" in item else item.get("output"),
            "arguments": item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
            "success": not item.get("error"),
            "isError": bool(item.get("error")),
        }
    return {
        "toolId": f"codex.{item_type}",
        "toolName": item_type,
        "rawToolName": f"codex.{item_type}",
        "content": item,
        "arguments": {},
        "success": True,
        "isError": False,
    }
