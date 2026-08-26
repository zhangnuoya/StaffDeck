"""CLI 运行时(codex / claude_code)的聊天附件物化测试。

回归背景:web 上传链路把 ``ChatAttachmentRead.text`` 强制置空以防止浏览器
回传伪造文本,旧版 ``_attachment_text`` 只消费该字段,导致 web 附件对 CLI
agent 完全不可见。物化模块须把暂存字节写入会话工作区并在 prompt 注入
工作区相对路径;渠道附件(带服务端提取文本)回退写文本;暂存过期时降级
提示而不是让轮次失败。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.config import get_settings
from app.db.models import AgentProfile, ChatSession, Tenant
from app.runtimes.adapters import claude_code as claude_mod
from app.runtimes.adapters import codex as codex_mod
from app.runtimes.adapters._attachments import (
    materialize_turn_attachments,
    materialized_image_relative_paths,
    render_attachment_section,
)
from app.runtimes.adapters.claude_code import ClaudeCodeAgentRuntime
from app.runtimes.adapters.codex import CodexAgentRuntime
from app.session.attachment_store import stage_chat_attachment
from app.session.attachments import (
    parse_chat_attachment,
    validate_chat_turn_attachments,
)
from app.session.session_schema import ChatAttachmentRead, ChatTurnRequest, ChatTurnResponse

FAKE_CODEX_CLI = str(Path(__file__).resolve().parents[1] / "mock_servers" / "fake_codex_cli.py")
FAKE_CLAUDE_CLI = str(Path(__file__).resolve().parents[1] / "mock_servers" / "fake_claude_cli.py")

MAX_BYTES = 12 * 1024 * 1024
TENANT = "tenant_demo"
USER = "user_cli"


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session, runtime: str) -> AgentProfile:
    db.add(Tenant(id=TENANT, name="Demo"))
    agent = AgentProfile(
        id=f"agent_{runtime}",
        tenant_id=TENANT,
        name="CLI 员工",
        persona_prompt="严谨、克制,先验证再执行。",
        runtime=runtime,
    )
    db.add(agent)
    db.commit()
    return agent


def _request(**overrides: object) -> ChatTurnRequest:
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "agent_id": "agent_codex",
        "message": "请读取附件并汇总",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)  # type: ignore[arg-type]


def _staged_attachment(
    filename: str,
    content_type: str,
    data: bytes,
) -> ChatAttachmentRead:
    """Replay the web upload + validated turn round-trip for one file."""
    parsed = parse_chat_attachment(filename, content_type, data, extract_text=False)
    staged = stage_chat_attachment(parsed, data, tenant_id=TENANT, user_id=USER)
    return validate_chat_turn_attachments(
        [staged],
        max_attachments=8,
        max_attachment_bytes=MAX_BYTES,
    )[0]


def _pdf_with_text(text: str) -> bytes:
    """Minimal single-page PDF with one text object (pypdf-parsable)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


# ---------------------------------------------------------------------------
# materializer units
# ---------------------------------------------------------------------------


def test_materialize_staged_web_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "ws"
    workspace.mkdir()

    png = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"
    xlsx = b"PK\x03\x04fake-xlsx"
    pdf_text = "Hello PDF Attachment"
    attachments = [
        _staged_attachment("notes.md", "text/markdown", "季度数据\nQ1: 100".encode()),
        _staged_attachment("data.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx),
        _staged_attachment("report.pdf", "application/pdf", _pdf_with_text(pdf_text)),
        _staged_attachment("shot.png", "image/png", png),
    ]

    results = materialize_turn_attachments(
        attachments,
        workspace=workspace,
        tenant_id=TENANT,
        user_id=USER,
    )

    by_name = {item.filename: item for item in results}
    assert all(item.error is None for item in results), [item.error for item in results]

    notes = by_name["notes.md"]
    assert notes.relative_path and notes.relative_path.startswith("attachments/")
    assert notes.relative_path.endswith("notes.md")
    assert (workspace / notes.relative_path).read_bytes() == "季度数据\nQ1: 100".encode()
    assert notes.inline_text is None  # web 附件不内联,靠工作区文件

    excel = by_name["data.xlsx"]
    assert (workspace / excel.relative_path).read_bytes() == xlsx

    report = by_name["report.pdf"]
    assert (workspace / report.relative_path).read_bytes().startswith(b"%PDF")
    assert report.extracted_text_path == f"{report.relative_path}.extracted.txt"
    assert pdf_text in (workspace / report.extracted_text_path).read_text(encoding="utf-8")

    shot = by_name["shot.png"]
    assert (workspace / shot.relative_path).read_bytes() == png


def test_materialize_channel_text_fallback(tmp_path: Path) -> None:
    """渠道附件(无暂存、带服务端提取文本)回退写文本并保留内联节选。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attachment = ChatAttachmentRead(
        id="file_channel",
        filename="feishu-note.txt",
        content_type="text/plain",
        size=48,
        kind="text",
        text="渠道消息附件正文" * 400,  # 超过内联上限,验证截断
    )

    results = materialize_turn_attachments(
        [attachment],
        workspace=workspace,
        tenant_id=TENANT,
        user_id=None,
    )

    assert len(results) == 1
    item = results[0]
    assert item.error is None
    assert item.relative_path is not None
    assert (workspace / item.relative_path).read_text(encoding="utf-8") == attachment.text
    assert len(item.inline_text or "") <= 4_000
    assert item.inline_text and item.inline_text.startswith("渠道消息附件正文")


def test_materialize_missing_staging_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """暂存过期(sha 有值但读不到)时降级提示,不抛异常、不写文件。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    attachment = _staged_attachment("gone.md", "text/markdown", b"payload")

    # 模拟暂存目录被清理:改用独立 data 目录后重新读取。
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data2"))
    workspace = tmp_path / "ws"
    workspace.mkdir()

    results = materialize_turn_attachments(
        [attachment],
        workspace=workspace,
        tenant_id=TENANT,
        user_id=USER,
    )

    assert len(results) == 1
    item = results[0]
    assert item.relative_path is None
    assert item.error and "暂存" in item.error
    assert not (workspace / "attachments").exists()

    section = render_attachment_section(results)
    assert "gone.md" in section
    assert item.error in section


def test_render_attachment_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    attachments = [
        _staged_attachment("report.pdf", "application/pdf", _pdf_with_text("Rendered PDF")),
        _staged_attachment("shot.png", "image/png", b"\x89PNG\r\n\x1a\nbody"),
    ]
    results = materialize_turn_attachments(
        attachments,
        workspace=workspace,
        tenant_id=TENANT,
        user_id=USER,
    )
    section = render_attachment_section(results)

    assert section.startswith("[用户上传附件]")
    for item in results:
        assert item.relative_path in section
    assert results[0].extracted_text_path in section
    # 默认(claude_code 等无视觉 CLI):图片只提示存在。
    assert "无法直接查看图像内容" in section
    assert "Rendered PDF" not in section  # 只给路径,pdf 正文不进 prompt

    # codex(vision_supported):图片标注为视觉输入,图片路径可供 -i 收集。
    vision_section = render_attachment_section(results, vision_supported=True)
    assert "视觉输入" in vision_section
    assert "无法直接查看图像内容" not in vision_section
    assert materialized_image_relative_paths(results) == [results[1].relative_path]


# ---------------------------------------------------------------------------
# adapter end-to-end with fake CLIs
# ---------------------------------------------------------------------------


@pytest.fixture
def codex_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "codex_cli_path", FAKE_CODEX_CLI)
    monkeypatch.setattr(settings, "codex_workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "tool_base_url", "http://testserver")
    monkeypatch.setattr(settings, "codex_timeout_seconds", 30.0)
    monkeypatch.setenv("FAKE_CODEX_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "")
    return settings


@pytest.fixture
def claude_settings(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "claude_code_cli_path", FAKE_CLAUDE_CLI)
    monkeypatch.setattr(settings, "codex_workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "tool_base_url", "http://testserver")
    monkeypatch.setattr(settings, "claude_code_timeout_seconds", 30.0)
    monkeypatch.setenv("FAKE_CLAUDE_CAPTURE", str(tmp_path / "capture.json"))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "")
    return settings


def test_codex_turn_prompt_includes_materialized_attachment(
    codex_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    raw = "季度销售数据\nQ1: 100".encode()
    attachment = _staged_attachment("sales.md", "text/markdown", raw)

    with _make_db() as db:
        _seed(db, "codex")
        response = CodexAgentRuntime(db).handle_turn(
            _request(user_id=USER, attachments=[attachment])
        )
    assert isinstance(response, ChatTurnResponse)

    capture = json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))
    prompt = capture["prompt"]
    assert "[用户上传附件]" in prompt
    assert "attachments/" in prompt
    assert "sales.md" in prompt

    workspace = Path(codex_mod.get_settings().codex_workspace_root) / response.session_id
    written = sorted((workspace / "attachments").glob("*sales.md"))
    assert len(written) == 1
    assert written[0].read_bytes() == raw


def test_codex_turn_attaches_image_for_vision(
    codex_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex 0.147+ 支持 exec/exec resume -i:物化成功的图片应作为视觉输入附给模型。"""
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    png = b"\x89PNG\r\n\x1a\n" + b"vision-payload"
    attachment = _staged_attachment("shot.png", "image/png", png)

    with _make_db() as db:
        _seed(db, "codex")
        response = CodexAgentRuntime(db).handle_turn(
            _request(user_id=USER, attachments=[attachment])
        )
    assert isinstance(response, ChatTurnResponse)

    capture = json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))
    argv = capture["argv"]
    idx = argv.index("-i")
    workspace = Path(codex_mod.get_settings().codex_workspace_root) / response.session_id
    written = sorted((workspace / "attachments").glob("*shot.png"))
    assert len(written) == 1
    assert written[0].read_bytes() == png
    assert argv[idx + 1] == str(written[0])

    prompt = capture["prompt"]
    assert "视觉输入" in prompt
    assert "无法直接查看图像内容" not in prompt


def test_claude_turn_prompt_includes_materialized_attachment(
    claude_settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    raw = "项目周报\n本周完成附件功能".encode()
    attachment = _staged_attachment("weekly.md", "text/markdown", raw)

    with _make_db() as db:
        _seed(db, "claude_code")
        response = ClaudeCodeAgentRuntime(db).handle_turn(
            _request(agent_id="agent_claude_code", user_id=USER, attachments=[attachment])
        )
    assert isinstance(response, ChatTurnResponse)

    capture = json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))
    prompt = capture["prompt"]
    assert "[用户上传附件]" in prompt
    assert "attachments/" in prompt

    workspace = Path(claude_mod.get_settings().codex_workspace_root) / f"{response.session_id}-claude"
    written = sorted((workspace / "attachments").glob("*weekly.md"))
    assert len(written) == 1
    assert written[0].read_bytes() == raw


def test_codex_build_args_attaches_images_on_new_and_resume(
    codex_settings, tmp_path: Path
) -> None:
    """exec 与 exec resume 都接受 -i;绝对路径与 -C/cwd 无关。"""
    from app.runtimes.adapters.codex import _PreparedTurn

    def prepared(*, is_resume: bool) -> _PreparedTurn:
        return _PreparedTurn(
            request=ChatTurnRequest(tenant_id=TENANT, agent_id="agent_codex", message="hi"),
            chat_session=ChatSession(id="session_x", tenant_id=TENANT, agent_id="agent_codex"),
            user_message_id="msg_1",
            agent=None,
            runtime_config={"model": "fake-model"},
            runtime_state={"thread_id": "thread_1"} if is_resume else {},
            workspace=tmp_path,
            prompt="hi",
            is_resume=is_resume,
            image_paths=["attachments/abc123-shot.png"],
        )

    with _make_db() as db:
        runtime = CodexAgentRuntime(db)
        for is_resume in (False, True):
            args = runtime._build_args(prepared(is_resume=is_resume))
            idx = args.index("-i")
            assert args[idx + 1] == str(tmp_path / "attachments/abc123-shot.png")


def test_data_url_roundtrip_helper() -> None:
    """图片 data_url 解码路径的独立回归(渠道图片无暂存时走该分支)。"""
    from app.runtimes.adapters._attachments import _decode_image_data_url

    data = b"\x89PNG\r\n\x1a\n" + b"body"
    attachment = ChatAttachmentRead(
        id="file_img",
        filename="i.png",
        content_type="image/png",
        size=len(data),
        kind="image",
        data_url="data:image/png;base64," + base64.b64encode(data).decode("ascii"),
    )
    assert _decode_image_data_url(attachment) == data
    attachment.data_url = "data:image/jpeg;base64,AAAA"
    assert _decode_image_data_url(attachment) is None
