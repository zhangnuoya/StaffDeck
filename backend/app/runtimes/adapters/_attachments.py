"""CLI 运行时(codex / claude_code)的聊天附件物化。

Web 上传链路出于防伪造考虑把 ``ChatAttachmentRead.text`` 强制置空,原始字节
只保留在服务端暂存区(``harness_uploads``,staffdeck 私有);CLI 子进程以
appuser 运行,既看不到暂存区也拿不到 data_url 之外的任何副本。这里对齐原生
引擎 ``core/harness_attachments.materialize_task_attachments`` 的语义:轮次
开始前把校验过的附件字节写入会话工作区 ``attachments/``,并在 prompt 中注入
工作区相对路径,让 agent 用自己的文件能力读取。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.session.attachment_store import read_staged_chat_attachment, sandbox_attachment_path
from app.session.attachments import MAX_EXTRACTED_TEXT_CHARS, extract_pdf_text
from app.session.session_schema import ChatAttachmentRead

logger = logging.getLogger(__name__)

# 渠道 text 附件内联进 prompt 的上限,与旧版 _attachment_text 行为一致。
INLINE_TEXT_CHARS = 4_000


@dataclass
class MaterializedAttachment:
    filename: str
    kind: str
    content_type: str
    size: int
    relative_path: str | None = None
    extracted_text_path: str | None = None
    inline_text: str | None = None
    error: str | None = None


def materialize_turn_attachments(
    attachments: list[ChatAttachmentRead],
    *,
    workspace: Path,
    tenant_id: str,
    user_id: str | None,
) -> list[MaterializedAttachment]:
    if not attachments:
        return []
    results: list[MaterializedAttachment] = []
    for index, attachment in enumerate(attachments, start=1):
        results.append(
            _materialize_one(
                attachment,
                index=index,
                workspace=workspace,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        )
    return results


def render_attachment_section(
    items: list[MaterializedAttachment],
    *,
    vision_supported: bool = False,
) -> str:
    """Render the prompt block describing materialized turn attachments.

    ``vision_supported`` marks CLI runtimes that can attach images to the
    model call itself (``codex exec -i``); otherwise the block tells the
    agent the image is present but not viewable.
    """
    if not items:
        return ""
    lines = [
        "[用户上传附件]",
        "以下附件已随本轮消息保存到当前工作区(工作区根目录即当前目录),需要内容时直接读取对应文件:",
    ]
    for item in items:
        size = _human_size(item.size)
        if item.error or not item.relative_path:
            lines.append(
                f"- {item.filename}({item.kind}/{item.content_type},{size}):"
                f"{item.error or '附件未能写入工作区。'}"
            )
            continue
        entry = f"- {item.relative_path}({item.filename},{item.kind},{size})"
        if item.extracted_text_path:
            entry += f";提取文本:{item.extracted_text_path}"
        if item.kind == "image":
            if vision_supported:
                entry += "。该图片已随本轮作为视觉输入直接提供,可直接查看并描述其内容"
            else:
                entry += "。注意:你当前无法直接查看图像内容,仅知晓文件存在"
        lines.append(entry)
        if item.inline_text:
            lines.append(f"  内容节选:\n{item.inline_text}")
    return "\n".join(lines)


def materialized_image_relative_paths(items: list[MaterializedAttachment]) -> list[str]:
    """Workspace-relative paths of images successfully materialized this turn."""
    return [
        item.relative_path
        for item in items
        if item.kind == "image" and item.relative_path and not item.error
    ]


def _materialize_one(
    attachment: ChatAttachmentRead,
    *,
    index: int,
    workspace: Path,
    tenant_id: str,
    user_id: str | None,
) -> MaterializedAttachment:
    result = MaterializedAttachment(
        filename=attachment.filename,
        kind=attachment.kind,
        content_type=attachment.content_type,
        size=attachment.size,
    )
    sandbox_path = sandbox_attachment_path(attachment, index)
    relative_path = sandbox_path.removeprefix("/workspace/")

    staged: bytes | None = None
    if user_id and attachment.sha256:
        staged = read_staged_chat_attachment(
            attachment,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if staged is None:
            logger.warning(
                "附件暂存读取失败(过期或校验不过) filename=%s sha256=%s",
                attachment.filename,
                (attachment.sha256 or "")[:12],
            )

    if staged is not None:
        if _write_workspace_file(workspace, relative_path, staged):
            result.relative_path = relative_path
            if attachment.kind == "pdf":
                text, _error = extract_pdf_text(staged)
                trimmed = text.strip()
                if len(trimmed) > MAX_EXTRACTED_TEXT_CHARS:
                    trimmed = trimmed[:MAX_EXTRACTED_TEXT_CHARS]
                if trimmed:
                    extracted_path = f"{relative_path}.extracted.txt"
                    if _write_workspace_file(workspace, extracted_path, trimmed.encode("utf-8")):
                        result.extracted_text_path = extracted_path
            if attachment.kind == "image":
                # 文件已就位;CLI 无视觉输入,渲染层负责注明局限。
                pass
            if attachment.kind == "text" and attachment.text:
                result.inline_text = attachment.text[:INLINE_TEXT_CHARS]
        else:
            result.error = "附件写入工作区失败。"
        return result

    # 没有暂存(渠道附件或暂存过期):回退用服务端已提取的文本。
    if attachment.kind == "image":
        data = _decode_image_data_url(attachment)
        if data is not None and _write_workspace_file(workspace, relative_path, data):
            result.relative_path = relative_path
        else:
            result.error = "图片附件未能写入工作区(data URL 缺失或无效)。"
        return result

    if attachment.text:
        # 渠道附件没有暂存字节,与原生引擎回退一致:把服务端已提取的文本
        # 直接写入附件路径(pdf 场景该文件即提取文本,而非原始二进制)。
        if _write_workspace_file(workspace, relative_path, attachment.text.encode("utf-8")):
            result.relative_path = relative_path
            result.inline_text = attachment.text[:INLINE_TEXT_CHARS]
        else:
            result.error = "附件写入工作区失败。"
        return result

    result.error = "服务端暂存已过期或校验未通过,无法提供附件内容。"
    return result


def _write_workspace_file(workspace: Path, relative_path: str, data: bytes) -> bool:
    target = workspace / relative_path
    try:
        if not _is_safe_relative_path(relative_path):
            raise ValueError("attachment path escapes workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # 工作区由后端(staffdeck 用户)写入、CLI 子进程以 appuser 运行,
        # 显式放开读权限保证 codex/claude 进程可见。
        os.chmod(target, 0o644)
        os.chmod(target.parent, 0o755)
    except OSError as exc:
        logger.warning("附件写入工作区失败 path=%s: %s", relative_path, exc)
        return False
    except ValueError as exc:
        logger.warning("附件路径不安全 path=%s: %s", relative_path, exc)
        return False
    return True


def _is_safe_relative_path(relative_path: str) -> bool:
    if not relative_path or relative_path.startswith(("/", "\\")):
        return False
    parts = Path(relative_path).parts
    if not parts or any(part in {"..", "", "."} for part in parts):
        return False
    return Path(relative_path).as_posix() == relative_path and not re.search(
        r"[:\\]", relative_path
    )


def _decode_image_data_url(attachment: ChatAttachmentRead) -> bytes | None:
    raw = str(attachment.data_url or "")
    prefix = f"data:{attachment.content_type};base64,"
    if not raw.startswith(prefix):
        return None
    try:
        return base64.b64decode(raw.removeprefix(prefix), validate=True)
    except (ValueError, TypeError):
        return None


def _human_size(size: int) -> str:
    try:
        value = int(size)
    except (TypeError, ValueError):
        return "未知大小"
    if value < 1024:
        return f"{value} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


__all__ = [
    "INLINE_TEXT_CHARS",
    "MaterializedAttachment",
    "materialize_turn_attachments",
    "materialized_image_relative_paths",
    "render_attachment_section",
]
