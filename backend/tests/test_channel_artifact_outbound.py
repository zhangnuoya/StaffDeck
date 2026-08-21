"""渠道产物投递守护回归测试(fork 独有功能)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.channels import artifact_outbound
from app.channels.adapters import base as adapter_registry
from app.channels.artifact_outbound import (
    deliver_due_artifacts,
    sweep_channel_artifact_messages,
)
from app.db.models import (
    ChannelArtifactDelivery,
    ChannelBinding,
    ChannelDelivery,
    ChatSession,
    Message,
    Tenant,
    UIConfig,
    User,
    utc_now,
)


class TransientFailure(RuntimeError):
    retryable = True


class PermanentFailure(RuntimeError):
    retryable = False


class FakeFileAdapter:
    def __init__(self, *, upload_fail_times: int = 0, permanent: bool = False):
        self.upload_fail_times = upload_fail_times
        self.permanent = permanent
        self.uploads: list[tuple[str, str, bytes]] = []
        self.sends: list[tuple[str, dict, str, str]] = []

    def upload_file(self, binding, *, filename: str, data: bytes) -> str:
        if self.upload_fail_times > 0:
            self.upload_fail_times -= 1
            raise PermanentFailure("上传被拒") if self.permanent else TransientFailure("上传抖动")
        self.uploads.append((binding.id, filename, data))
        return "file_key_fake"

    def send_file(self, binding, target, *, file_key: str, idempotency_key: str) -> str:
        self.sends.append((binding.id, dict(target), file_key, idempotency_key))
        return "om_fake"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id="tenant_demo", name="Demo"))
        session.add(
            User(
                id="user_1",
                tenant_id="tenant_demo",
                username="demo",
                password_hash="x",
                role="member",
            )
        )
        session.commit()
        yield session


def _seed_feishu_binding(db: Session, *, status: str = "active") -> ChannelBinding:
    binding = ChannelBinding(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="feishu",
        status=status,
        external_account_key="feishu:account",
    )
    db.add(binding)
    db.commit()
    return binding


def _channel_session(
    db: Session, binding: ChannelBinding, *, workspace: Path | None = None
) -> ChatSession:
    session = ChatSession(
        id="session_chan",
        tenant_id="tenant_demo",
        user_id="user_1",
        agent_id=binding.agent_id,
        channel="feishu",
        external_conv_id="feishu_p2p_u1",
        channel_target_json={"message_id": "om_root"},
        channel_binding_id=binding.id,
        channel_account_key=binding.external_account_key,
    )
    if workspace is not None:
        session.runtime_state_json = {"workspace": str(workspace)}
    db.add(session)
    db.commit()
    return session


def _assistant_message(
    db: Session,
    session_id: str,
    message_id: str,
    *,
    artifacts: list[dict] | None = None,
) -> Message:
    message = Message(
        id=message_id,
        tenant_id="tenant_demo",
        session_id=session_id,
        role="assistant",
        content="回复内容",
    )
    if artifacts is not None:
        message.metadata_json = {"harness_artifacts": artifacts}
    db.add(message)
    db.commit()
    return message


def _delivered_text_delivery(
    db: Session,
    binding: ChannelBinding,
    session_id: str,
    message_id: str,
    *,
    status: str = "delivered",
) -> ChannelDelivery:
    delivery = ChannelDelivery(
        tenant_id="tenant_demo",
        binding_id=binding.id,
        session_id=session_id,
        message_id=message_id,
        kind="reply",
        text="回复内容",
        status=status,
        target_json={"message_id": "om_root"},
        idempotency_key=f"key-{message_id}",
    )
    db.add(delivery)
    db.commit()
    return delivery


def _codex_artifact(path: str, *, display_name: str | None = None) -> dict:
    entry = {
        "type": "workspace_file",
        "task_frame_id": "codex-turn-1",
        "path": path,
        "sha256": "0" * 64,
        "size": 8,
        "source": "codex",
    }
    if display_name:
        entry["display_name"] = display_name
    return entry


def _artifact_rows(db: Session) -> list[ChannelArtifactDelivery]:
    return list(
        db.exec(
            select(ChannelArtifactDelivery).order_by(ChannelArtifactDelivery.artifact_path)
        ).all()
    )


def test_sweep_stages_pending_rows_after_text_delivered(db: Session) -> None:
    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding)
    message = _assistant_message(
        db,
        session.id,
        "msg_1",
        artifacts=[_codex_artifact("report.md"), _codex_artifact("data.csv")],
    )
    _delivered_text_delivery(db, binding, session.id, message.id)

    staged = sweep_channel_artifact_messages(db)
    assert staged == 2
    rows = _artifact_rows(db)
    assert [row.status for row in rows] == ["pending", "pending"]
    assert {row.artifact_path for row in rows} == {"report.md", "data.csv"}
    assert all(row.binding_id == binding.id for row in rows)


def test_sweep_skips_when_text_delivery_not_delivered(db: Session) -> None:
    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding)
    message = _assistant_message(
        db, session.id, "msg_2", artifacts=[_codex_artifact("report.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id, status="pending")

    assert sweep_channel_artifact_messages(db) == 0
    assert _artifact_rows(db) == []


def test_sweep_is_idempotent_and_filters_types(db: Session) -> None:
    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding)
    message = _assistant_message(
        db,
        session.id,
        "msg_3",
        artifacts=[
            _codex_artifact("report.md"),
            {"type": "inline_text", "path": "ignored.txt"},
            _codex_artifact("report.md"),  # 重复路径只登记一次
        ],
    )
    _delivered_text_delivery(db, binding, session.id, message.id)

    assert sweep_channel_artifact_messages(db) == 1
    assert sweep_channel_artifact_messages(db) == 0
    rows = _artifact_rows(db)
    assert [row.artifact_path for row in rows] == ["report.md"]
    assert rows[0].display_name == "report.md"


def test_deliver_uploads_and_sends_file_for_codex_workspace(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"hello!!!")
    settings = artifact_outbound.get_settings().model_copy(
        update={"codex_workspace_root": str(tmp_path)}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    adapter = FakeFileAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db,
        session.id,
        "msg_4",
        artifacts=[_codex_artifact("report.md", display_name="报告.md")],
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert adapter.uploads == [(binding.id, "报告.md", b"hello!!!")]
    assert len(adapter.sends) == 1
    _, target, file_key, idempotency_key = adapter.sends[0]
    assert target == {"message_id": "om_root"}
    assert file_key == "file_key_fake"
    assert idempotency_key.endswith(":file")

    # 已送达后不再重复投递。
    assert deliver_due_artifacts(db) == 0


def test_deliver_fails_permanently_when_file_missing(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = artifact_outbound.get_settings().model_copy(
        update={"codex_workspace_root": str(tmp_path)}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    adapter = FakeFileAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db, session.id, "msg_5", artifacts=[_codex_artifact("gone.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "failed"
    assert row.last_error and "不可用" in row.last_error
    assert adapter.uploads == []


def test_deliver_retries_transient_then_succeeds(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"data")
    settings = artifact_outbound.get_settings().model_copy(
        update={"codex_workspace_root": str(tmp_path)}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    adapter = FakeFileAdapter(upload_fail_times=1)
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db, session.id, "msg_6", artifacts=[_codex_artifact("report.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None and row.next_attempt_at > utc_now()

    # 退避时间未到:本轮不投递;到期后成功。
    assert deliver_due_artifacts(db) == 0
    row.next_attempt_at = utc_now()
    db.add(row)
    db.commit()
    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "delivered"


def test_deliver_gives_up_after_max_attempts(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"data")
    settings = artifact_outbound.get_settings().model_copy(
        update={
            "codex_workspace_root": str(tmp_path),
            "channel_delivery_max_attempts": 2,
        }
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    adapter = FakeFileAdapter(upload_fail_times=99)
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db, session.id, "msg_7", artifacts=[_codex_artifact("report.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    row = _artifact_rows(db)[0]
    for _ in range(2):
        deliver_due_artifacts(db)
        row = _artifact_rows(db)[0]
        if row.status == "pending":
            row.next_attempt_at = utc_now()
            db.add(row)
            db.commit()
    assert row.status == "failed"
    assert row.attempts == 2


def test_deliver_fails_when_binding_inactive(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_bytes(b"data")
    settings = artifact_outbound.get_settings().model_copy(
        update={"codex_workspace_root": str(tmp_path)}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    adapter = FakeFileAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db, status="disabled")
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db, session.id, "msg_8", artifacts=[_codex_artifact("report.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "failed"
    assert adapter.uploads == []


def test_oversized_file_fails_without_upload(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "big.bin").write_bytes(b"x" * 32)
    settings = artifact_outbound.get_settings().model_copy(
        update={"codex_workspace_root": str(tmp_path)}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)
    monkeypatch.setattr(artifact_outbound, "MAX_UPLOAD_BYTES", 16)

    adapter = FakeFileAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding, workspace=workspace)
    message = _assistant_message(
        db, session.id, "msg_9", artifacts=[_codex_artifact("big.bin")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "failed"
    assert "上限" in (row.last_error or "")


def test_native_artifact_uses_task_frame_workspace(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "harness"
    storage.mkdir()
    db.add(
        UIConfig(
            tenant_id="tenant_demo",
            harness_storage_path=str(storage),
            sandbox_enabled=False,
        )
    )
    db.commit()
    # native 产物走 harness_task_workspace_path:storage/tenant/session/frame 下。
    from app.core.harness_session_cleanup import harness_path_segment

    frame_id = "frame_1"
    task_ws = (
        storage
        / harness_path_segment("tenant_demo")
        / harness_path_segment("session_chan")
        / harness_path_segment(frame_id)
    )
    task_ws.mkdir(parents=True)
    (task_ws / "result.json").write_bytes(b"{}")

    adapter = FakeFileAdapter()
    monkeypatch.setitem(adapter_registry._adapters, "feishu", adapter)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding)  # 无 runtime workspace → native 分支
    message = _assistant_message(
        db,
        session.id,
        "msg_10",
        artifacts=[
            {
                "type": "workspace_file",
                "task_frame_id": frame_id,
                "path": "result.json",
            }
        ],
    )
    _delivered_text_delivery(db, binding, session.id, message.id)
    sweep_channel_artifact_messages(db)

    assert deliver_due_artifacts(db) == 1
    row = _artifact_rows(db)[0]
    assert row.status == "delivered"
    assert adapter.uploads[0][1] == "result.json"


def test_disabled_flag_skips_everything(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = artifact_outbound.get_settings().model_copy(
        update={"channel_artifact_delivery_enabled": False}
    )
    monkeypatch.setattr(artifact_outbound, "get_settings", lambda: settings)

    binding = _seed_feishu_binding(db)
    session = _channel_session(db, binding)
    message = _assistant_message(
        db, session.id, "msg_11", artifacts=[_codex_artifact("report.md")]
    )
    _delivered_text_delivery(db, binding, session.id, message.id)

    assert sweep_channel_artifact_messages(db) == 0
    assert deliver_due_artifacts(db) == 0
    assert _artifact_rows(db) == []
