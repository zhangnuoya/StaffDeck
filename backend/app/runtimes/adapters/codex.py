from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app import paths
from app.config import get_settings
from app.core.agent_identity_prompt import AgentIdentityPrompt
from app.core.cancellation import clear_chat_turn_cancelled, is_chat_turn_cancelled
from app.core.legacy_conversation_projection import LegacyConversationProjection
from app.db.models import AgentProfile, ChatSession, Message, utc_now
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

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
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
            metadata=LegacyConversationProjection.user_message_metadata(request),
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

    def _build_args(self, prepared: _PreparedTurn) -> list[str]:
        args = [*_codex_base_command(self._settings), "exec"]
        if prepared.is_resume:
            args += ["resume", str(prepared.runtime_state["thread_id"])]
        args += ["-", "--json", "--skip-git-repo-check"]
        if not prepared.is_resume:
            args += ["-C", str(prepared.workspace)]
        sandbox = str(prepared.runtime_config.get("sandbox") or "bypass")
        if sandbox == "bypass":
            # 非交互服务场景无人能点“批准”；审批层（approval_policy=never）会
            # 把 MCP 工具调用自动取消，且 Windows 沙箱会拦截 pwsh。默认完全绕过
            # 审批与沙箱（与 claude_code 适配器的 bypassPermissions 同一姿态），
            # 收紧环境可用 runtime_config.sandbox = workspace-write / read-only 回退。
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        elif prepared.is_resume:
            args += ["-c", f'sandbox_mode="{sandbox}"']
        else:
            args += ["-s", sandbox]
        model = str(
            prepared.runtime_config.get("model") or self._settings.codex_default_model or ""
        )
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
