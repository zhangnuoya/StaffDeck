"""渠道产物投递守护(fork 独有,不上游)。

把 assistant 回复登记的 harness 产物(workspace_file)作为文件消息补发到
渠道,目前仅飞书 adapter 具备 upload_file/send_file 能力。

设计约束:不改 service_outbox 的文本投递管线——本守护自己扫描 Message
元数据建 ChannelArtifactDelivery 投递行,(message_id, artifact_path)
唯一约束保证幂等;文本 delivery 未 delivered 前不登记产物行,保证
"正文先到、文件后到"的顺序。文本回复失败不影响本表,反之亦然。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.channels.adapters import get_channel_adapter
from app.channels.service_outbox import _NON_DELIVERY_CHANNELS
from app.config import get_settings
from app.db import engine
from app.db.models import (
    ChannelArtifactDelivery,
    ChannelBinding,
    ChannelDelivery,
    ChatSession,
    Message,
    utc_now,
)
from app.harness.artifacts import HarnessArtifactAccessError, open_harness_artifact

logger = logging.getLogger(__name__)

# 单文件上传上限:飞书 im/v1/files stream 类型 30MB。
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
# 每条回复最多补发的产物条数(登记函数 publish_harness_artifacts 上限 20,
# 渠道侧进一步收紧避免刷屏)。
MAX_ARTIFACTS_PER_MESSAGE = 5
# 扫描窗口:daemon 停机重启后仍能补上停机期间完成的回复。
SCAN_WINDOW = timedelta(hours=1)
SCAN_LIMIT = 200
# 单轮最多投递条数,避免长事务。
DELIVER_BATCH = 50


def _message_artifacts(message: Message) -> list[dict[str, Any]]:
    artifacts = (message.metadata_json or {}).get("harness_artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict)]


def sweep_channel_artifact_messages(db: Session) -> int:
    """扫描近期渠道回复,为未登记的产物建 pending 投递行。"""

    if not get_settings().channel_artifact_delivery_enabled:
        return 0
    window_start = utc_now() - SCAN_WINDOW
    messages = (
        db.exec(
            select(Message)
            .join(ChatSession, Message.session_id == ChatSession.id)
            .where(
                Message.role == "assistant",
                Message.created_at >= window_start,
                ChatSession.channel != "",  # type: ignore[comparison-overlap]
                ChatSession.channel.is_not(None),  # type: ignore[union-attr]
            )
            .order_by(Message.created_at.desc())
            .limit(SCAN_LIMIT)
        )
        .all()
    )
    staged = 0
    for message in messages:
        session = db.get(ChatSession, message.session_id)
        if session is None or session.channel in _NON_DELIVERY_CHANNELS:
            continue
        if not _message_artifacts(message):
            continue
        # 文本回复送达后才补发文件,保证顺序;文本失败的回复不补文件。
        text_delivery = db.exec(
            select(ChannelDelivery).where(
                ChannelDelivery.message_id == message.id,
                ChannelDelivery.kind == "reply",
            )
        ).first()
        if text_delivery is None or text_delivery.status != "delivered":
            continue
        for artifact in _message_artifacts(message)[:MAX_ARTIFACTS_PER_MESSAGE]:
            if artifact.get("type") != "workspace_file":
                continue
            path = artifact.get("path")
            if not isinstance(path, str) or not path.strip():
                continue
            existing = db.exec(
                select(ChannelArtifactDelivery).where(
                    ChannelArtifactDelivery.message_id == message.id,
                    ChannelArtifactDelivery.artifact_path == path,
                )
            ).first()
            if existing is not None:
                continue
            db.add(
                ChannelArtifactDelivery(
                    tenant_id=message.tenant_id,
                    binding_id=text_delivery.binding_id,
                    session_id=message.session_id,
                    message_id=message.id,
                    artifact_path=path,
                    display_name=str(artifact.get("display_name") or Path(path).name)[
                        :180
                    ],
                    status="pending",
                    next_attempt_at=utc_now(),
                )
            )
            staged += 1
    if staged:
        db.commit()
    return staged


def _artifact_workspace(
    db: Session, session: ChatSession, artifact: dict[str, Any]
) -> Path | None:
    """定位产物所在工作区:CLI 运行时(codex/claude)优先,原生引擎兜底。

    与 Web 下载端点同款白名单策略:runtime_state_json.workspace 虽是服务端
    写入,仍要求位于配置的工作区根之下才接受,防任意路径读取。
    """

    state = session.runtime_state_json or {}
    raw = str(state.get("workspace") or "").strip()
    if raw:
        try:
            workspace = Path(raw).resolve(strict=True)
        except OSError:
            workspace = None
        if workspace is not None:
            allowed_roots: list[Path] = []
            configured_root = (get_settings().codex_workspace_root or "").strip()
            if configured_root:
                allowed_roots.append(Path(configured_root))
            from app import paths

            allowed_roots.append(paths.user_data_dir() / "workspaces")
            for root in allowed_roots:
                try:
                    workspace.relative_to(root.resolve(strict=False))
                    return workspace
                except (OSError, ValueError):
                    continue
    task_frame_id = str(artifact.get("task_frame_id") or "").strip()
    if task_frame_id:
        from app.core.harness_session_cleanup import harness_task_workspace_path

        try:
            return harness_task_workspace_path(
                tenant_id=session.tenant_id,
                session_id=session.id,
                task_frame_id=task_frame_id,
                db=db,
            )
        except OSError:
            return None
    return None


def _fail_artifact(
    db: Session, delivery: ChannelArtifactDelivery, error: str, *, retry_later: bool
) -> None:
    delivery.attempts += 1
    delivery.last_error = error[:500]
    delivery.updated_at = utc_now()
    if retry_later and delivery.attempts < get_settings().channel_delivery_max_attempts:
        delay = min(2**delivery.attempts, 300)
        delivery.status = "pending"
        delivery.next_attempt_at = utc_now() + timedelta(seconds=delay)
    else:
        delivery.status = "failed"
        delivery.next_attempt_at = None
    db.add(delivery)
    db.commit()


def _deliver_one_artifact(db: Session, delivery: ChannelArtifactDelivery) -> None:
    binding = db.get(ChannelBinding, delivery.binding_id)
    if (
        binding is None
        or binding.tenant_id != delivery.tenant_id
        or binding.status != "active"
    ):
        _fail_artifact(db, delivery, "渠道绑定不存在或已停用", retry_later=False)
        return
    message = db.get(Message, delivery.message_id)
    session = db.get(ChatSession, delivery.session_id)
    if message is None or session is None:
        _fail_artifact(db, delivery, "消息或会话不存在", retry_later=False)
        return
    artifact = next(
        (
            item
            for item in _message_artifacts(message)
            if item.get("path") == delivery.artifact_path
        ),
        None,
    )
    workspace = _artifact_workspace(db, session, artifact or {})
    if workspace is None:
        _fail_artifact(db, delivery, "无法定位产物工作区", retry_later=False)
        return
    try:
        opened = open_harness_artifact(workspace, delivery.artifact_path)
    except (HarnessArtifactAccessError, OSError) as exc:
        # 文件被清理/安全校验拒绝属于永久失败,重试无意义。
        _fail_artifact(db, delivery, f"产物文件不可用: {exc}", retry_later=False)
        return
    try:
        if opened.size > MAX_UPLOAD_BYTES:
            _fail_artifact(
                db, delivery, "文件超过渠道上传上限", retry_later=False
            )
            return
        data = b"".join(opened.iter_bytes())
        filename = delivery.display_name or opened.filename
    finally:
        opened.close()
    adapter = get_channel_adapter(binding.channel)
    upload_file = getattr(adapter, "upload_file", None)
    send_file = getattr(adapter, "send_file", None)
    if not callable(upload_file) or not callable(send_file):
        _fail_artifact(db, delivery, "渠道不支持文件投递", retry_later=False)
        return
    # 投递目标复用文本回复的 target(同一消息,同一会话上下文)。
    text_delivery = db.exec(
        select(ChannelDelivery).where(
            ChannelDelivery.message_id == delivery.message_id,
            ChannelDelivery.kind == "reply",
        )
    ).first()
    target = dict(text_delivery.target_json or {}) if text_delivery else {}
    if not target:
        target = dict(session.channel_target_json or {})
    try:
        file_key = upload_file(binding, filename=filename, data=data)
        send_file(binding, target, file_key=file_key, idempotency_key=f"{delivery.id}:file")
    except Exception as exc:  # noqa: BLE001 -- 渠道适配器错误类型不可枚举,与文本投递同款兜底
        retryable = bool(getattr(exc, "retryable", True))
        _fail_artifact(db, delivery, str(exc), retry_later=retryable)
        logger.warning(
            "渠道产物投递失败(第 %s 次) delivery=%s: %s",
            delivery.attempts,
            delivery.id,
            exc,
        )
        return
    delivery.status = "delivered"
    delivery.delivered_at = utc_now()
    delivery.last_error = None
    delivery.updated_at = utc_now()
    db.add(delivery)
    db.commit()


def deliver_due_artifacts(db: Session) -> int:
    if not get_settings().channel_artifact_delivery_enabled:
        return 0
    now = utc_now()
    rows = (
        db.exec(
            select(ChannelArtifactDelivery)
            .where(
                ChannelArtifactDelivery.status == "pending",
                ChannelArtifactDelivery.next_attempt_at <= now,
            )
            .limit(DELIVER_BATCH)
        )
        .all()
    )
    for row in rows:
        _deliver_one_artifact(db, row)
    return len(rows)


_artifact_thread: threading.Thread | None = None
_artifact_stop = threading.Event()


def run_artifact_delivery_daemon(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    db_engine=None,
) -> None:
    use_engine = db_engine or engine
    interval = (
        poll_seconds
        if poll_seconds is not None
        else max(get_settings().channel_delivery_poll_seconds, 2.0)
    )
    while True:
        try:
            with Session(use_engine) as db:
                sweep_channel_artifact_messages(db)
                deliver_due_artifacts(db)
        except Exception:
            logger.exception("渠道产物投递守护轮询失败")
        if once or _artifact_stop.is_set():
            return
        if _artifact_stop.wait(max(0.2, interval)):
            return


def start_artifact_delivery_daemon(*, db_engine=None) -> None:
    global _artifact_thread
    if not get_settings().channel_artifact_delivery_enabled:
        logger.info("channel_artifact_delivery_enabled=False,产物投递守护不启动")
        return
    _artifact_stop.clear()
    if _artifact_thread and _artifact_thread.is_alive():
        return
    _artifact_thread = threading.Thread(
        target=run_artifact_delivery_daemon,
        kwargs={"db_engine": db_engine},
        name="staffdeck-channel-artifact-delivery",
        daemon=True,
    )
    _artifact_thread.start()


def stop_artifact_delivery_daemon(timeout_seconds: float = 5.0) -> bool:
    global _artifact_thread
    _artifact_stop.set()
    if _artifact_thread and _artifact_thread.is_alive():
        _artifact_thread.join(timeout=max(0.0, timeout_seconds))
    stopped = not (_artifact_thread and _artifact_thread.is_alive())
    if stopped:
        _artifact_thread = None
    return stopped
