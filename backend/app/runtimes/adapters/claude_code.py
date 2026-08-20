from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
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
_CLAUDE_PROGRESS_PHASE = "claude_progress"

_CLAUDE_CLI_NOT_FOUND = (
    "未找到 Claude Code CLI。请先安装 claude 或在设置中配置 claude_code_cli_path。"
)


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
    system_prompt: str
    is_resume: bool
    reply: str = ""
    usage: dict[str, Any] | None = None
    response: ChatTurnResponse | None = None


class ClaudeCodeAgentRuntime:
    """Agent runtime executing turns through the local Claude Code CLI.

    Mirrors CodexAgentRuntime's turn lifecycle: StaffDeck sessions/messages
    stay the source of truth, Claude's session_id is persisted in
    `sessions.runtime_state_json` for --resume, and stream-json events are
    normalized onto the existing stream event vocabulary.
    """

    runtime_kind = AgentRuntimeKind.CLAUDE_CODE

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
        is_resume = bool(runtime_state.get("thread_id"))
        message = request.message
        attachment_text = _attachment_text(request)
        if attachment_text:
            message = f"{message}\n\n{attachment_text}"
        system_prompt = self._system_prompt(agent)
        prompt = message
        if not is_resume:
            history = self._history_text(chat_session, user_message.id)
            if history:
                prompt = f"[对话历史]\n{history}\n\n[用户消息]\n{message}"
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
            system_prompt=system_prompt,
            is_resume=is_resume,
        )

    def _system_prompt(self, agent: AgentProfile | None) -> str:
        sections: list[str] = []
        if agent:
            sections.append(AgentIdentityPrompt.render(agent))
        sections.append(
            "你能通过名为 staffdeck 的 MCP 工具集访问该员工绑定的企业能力。"
            "业务工具已按原生名称在工具清单中列出，请直接按名调用（不要用 call_tool 包装）；"
            "知识库检索用 query_knowledge，通用技能用 run_general_skill。不要编造企业内部信息。"
        )
        return "\n\n".join(sections)

    def _ensure_workspace(self, session_id: str) -> Path:
        root = (self._settings.codex_workspace_root or "").strip()
        base = Path(root) if root else paths.user_data_dir() / "workspaces"
        workspace = base / f"{session_id}-claude"
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

    # ------------------------------------------------------------------
    # claude invocation
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
                pass
        return str(self._settings.claude_code_default_model or "").strip()

    def _build_args(self, prepared: _PreparedTurn) -> list[str]:
        args = [
            *_claude_base_command(self._settings),
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--add-dir",
            str(prepared.workspace),
        ]
        if prepared.is_resume:
            args += ["--resume", str(prepared.runtime_state["thread_id"])]
        model = self._resolve_model(prepared)
        if model:
            args += ["--model", model]
        token = issue_capability_token(
            tenant_id=prepared.request.tenant_id,
            agent_id=prepared.chat_session.agent_id or prepared.request.agent_id or "",
            session_id=prepared.chat_session.id,
            turn_id=prepared.user_message_id,
        )
        gateway_url = f"{self._settings.normalized_tool_base_url}/api/mcp/{token}"
        mcp_config = json.dumps(
            {"mcpServers": {"staffdeck": {"type": "http", "url": gateway_url}}},
            ensure_ascii=False,
        )
        args += ["--strict-mcp-config", "--mcp-config", mcp_config]
        if prepared.system_prompt.strip():
            args += ["--append-system-prompt", prepared.system_prompt]
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
            yield from self._run_claude(prepared, streaming)
        except Exception as exc:
            logger.exception("claude code turn failed (session=%s)", session.id)
            yield from self._fail_turn(prepared, "CLAUDE_ADAPTER_ERROR", str(exc)[:300], streaming)
        finally:
            clear_chat_turn_cancelled(session.id, prepared.user_message_id)
            if prepared.request.client_turn_id:
                clear_chat_turn_cancelled(session.id, prepared.request.client_turn_id)

    def _run_claude(self, prepared: _PreparedTurn, streaming: bool) -> Iterator[dict[str, Any]]:
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
                # Linux 下新会话独立进程组：取消/超时经 killpg 清理整个包装链。
                start_new_session=(os.name != "nt"),
            )
        except FileNotFoundError:
            yield from self._fail_turn(
                prepared, "CLAUDE_CLI_NOT_FOUND", _CLAUDE_CLI_NOT_FOUND, streaming
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

        timeout = float(self._settings.claude_code_timeout_seconds or 900.0)
        deadline = time.monotonic() + timeout
        last_heartbeat = time.monotonic()
        stdout_open = True
        stderr_tail: list[str] = []
        result_payload: dict[str, Any] | None = None
        last_text_by_message: dict[str, str] = {}
        failure: tuple[str, str] | None = None
        cancelled = False

        while True:
            if streaming and self._is_cancelled(prepared):
                cancelled = True
                break
            if time.monotonic() > deadline:
                failure = ("CLAUDE_TIMEOUT", f"Claude Code 执行超过 {timeout:g} 秒未结束")
                break
            try:
                tag, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                if streaming and time.monotonic() - last_heartbeat >= _HEARTBEAT_SECONDS:
                    last_heartbeat = time.monotonic()
                    yield self._status(prepared, "responding", "Claude Code 正在执行…", streaming)
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
            if event_type == "system" and event.get("subtype") == "init":
                session_id = str(event.get("session_id") or "").strip()
                if session_id:
                    prepared.runtime_state["thread_id"] = session_id
                    self._persist_runtime_state(prepared)
            elif event_type == "assistant":
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                message_id = str(message.get("id") or "")
                content = message.get("content") if isinstance(message.get("content"), list) else []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "text":
                        text = str(block.get("text") or "")
                        previous = last_text_by_message.get(message_id, "")
                        suffix = text.removeprefix(previous) if text.startswith(previous) else text
                        last_text_by_message[message_id] = text
                        if streaming and suffix:
                            for chunk in reply_chunks(suffix):
                                yield self._event(
                                    prepared, "stream_delta", {"content": chunk}, persist=True
                                )
                    elif block_type == "thinking":
                        if streaming:
                            thinking = str(block.get("thinking") or "").strip()
                            if thinking:
                                yield self._status(prepared, "stepping", thinking[:120], streaming)
                    elif block_type == "tool_use":
                        yield self._event(
                            prepared,
                            "tool_result",
                            _tool_use_activity_payload(block),
                            persist=True,
                            streaming=streaming,
                        )
            elif event_type == "result":
                result_payload = event

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

        if result_payload is None:
            tail = "\n".join(stderr_tail[-5:])[:300]
            failure = failure or (
                "CLAUDE_NO_RESULT",
                tail or f"claude 进程退出码 {process.returncode}",
            )
        elif result_payload.get("is_error"):
            message = str(result_payload.get("result") or result_payload.get("subtype") or "")
            failure = failure or ("CLAUDE_EXEC_FAILED", message[:300] or "Claude Code 执行失败")

        if failure is not None:
            kill_process_tree(process)
            yield from self._fail_turn(prepared, failure[0], failure[1], streaming)
            return

        usage = result_payload.get("usage") if isinstance(result_payload.get("usage"), dict) else {}
        if result_payload.get("total_cost_usd") is not None:
            usage = {**usage, "total_cost_usd": result_payload["total_cost_usd"]}
        prepared.usage = usage or None
        reply = str(result_payload.get("result") or "").strip()
        if not reply and last_text_by_message:
            reply = max(last_text_by_message.values(), key=len).strip()
        prepared.reply = reply or "（Claude Code 未返回文本回复）"
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
        state["runtime"] = AgentRuntimeKind.CLAUDE_CODE.value
        state["workspace"] = str(prepared.workspace)
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        session.runtime_state_json = state
        self._db.add(session)
        extra_metadata: dict[str, Any] = {"runtime": AgentRuntimeKind.CLAUDE_CODE.value}
        if prepared.usage:
            extra_metadata["claude_usage"] = prepared.usage
        if state.get("thread_id"):
            extra_metadata["claude_session_id"] = state["thread_id"]
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
        prepared.reply = f"Claude Code 执行失败:{message}"
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
        state["runtime"] = AgentRuntimeKind.CLAUDE_CODE.value
        state["workspace"] = str(prepared.workspace)
        session.runtime_state_json = state
        session.updated_at = utc_now()
        self._db.add(session)
        self._db.commit()


# ----------------------------------------------------------------------
# module helpers
# ----------------------------------------------------------------------


def claude_cli_available(settings: Any | None = None) -> bool:
    settings = settings or get_settings()
    configured = (settings.claude_code_cli_path or "").strip()
    if configured:
        return Path(configured).exists()
    return shutil.which("claude") is not None


def _claude_base_command(settings: Any) -> list[str]:
    raw = (settings.claude_code_cli_path or "").strip() or (shutil.which("claude") or "claude")
    lowered = raw.lower()
    if lowered.endswith(".py"):
        return [sys.executable, raw]
    if os.name == "nt" and lowered.endswith((".cmd", ".bat")):
        return ["cmd", "/c", raw]
    return [raw]


def _attachment_text(request: ChatTurnRequest) -> str:
    parts: list[str] = []
    for attachment in request.attachments:
        if attachment.kind == "text" and attachment.text:
            parts.append(f"[附件 {attachment.filename}]\n{attachment.text[:4000]}")
    return "\n\n".join(parts)


def _tool_use_activity_payload(block: dict[str, Any]) -> dict[str, Any]:
    name = str(block.get("name") or "tool")
    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
    return {
        "toolId": f"claude.{name}",
        "toolName": name,
        "rawToolName": f"claude.{name}",
        "content": {"input": arguments},
        "arguments": arguments,
        "success": True,
        "isError": False,
    }
