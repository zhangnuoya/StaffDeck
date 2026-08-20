"""codex 运行时产物下载适配的回归测试。

覆盖:_collect_turn_artifacts 信号并集与排噪、file_change 路径收集、
下载端点的 codex 分支(无 Harness TaskFrame 时走会话工作区)、
会话删除时的 CLI 工作区清理与白名单防误删。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.chat import (
    _runtime_workspace_path,
    delete_chat_session,
    download_harness_artifact,
)
from app.db.models import ChatSession, Message, Tenant, User
from app.harness import snapshot_harness_workspace
from app.runtimes.adapters.codex import (
    CodexAgentRuntime,
    _collect_turn_artifacts,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _workspace_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(data_dir))
    root = data_dir / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_codex_session(
    db: Session,
    *,
    workspace: Path | None,
    session_id: str = "sess_codex",
) -> User:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    user = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        password_hash="test",
    )
    db.add(user)
    runtime_state = {"runtime": "codex", "workspace": str(workspace)} if workspace else {}
    db.add(
        ChatSession(
            id=session_id,
            tenant_id="tenant_demo",
            user_id=user.id,
            runtime_state_json=runtime_state,
        )
    )
    db.commit()
    return user


async def _read_response_body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    if response.background is not None:
        await response.background()
    return b"".join(chunks)


# ----------------------------------------------------------------------
# 收集逻辑
# ----------------------------------------------------------------------


def test_collect_turn_artifacts_unions_signals_and_filters_noise(tmp_path: Path) -> None:
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "input.txt").write_text("input", encoding="utf-8")
    before = snapshot_harness_workspace(workspace)

    (workspace / "report.md").write_text("报告内容", encoding="utf-8")
    (workspace / "data").mkdir()
    (workspace / "data" / "result.csv").write_text("a,b", encoding="utf-8")
    (workspace / "input.txt").write_text("modified", encoding="utf-8")
    (workspace / "shell-out.txt").write_text("shell", encoding="utf-8")
    (workspace / ".hidden.tmp").write_text("noise", encoding="utf-8")
    (workspace / "debug.log").write_text("noise", encoding="utf-8")

    published = _collect_turn_artifacts(
        workspace,
        before,
        # file_change 信号:codex 报工作区绝对路径;另含缺失文件与越界绝对路径
        [
            f"{workspace}/shell-out.txt",
            "../outside.txt",
            "/etc/passwd",
            "missing.txt",
        ],
        turn_no=3,
    )

    assert {item["path"] for item in published} == {
        "report.md",
        "data/result.csv",
        "input.txt",
        "shell-out.txt",
    }
    assert all(item["task_frame_id"] == "codex-turn-3" for item in published)
    assert all(item["source"] == "codex" for item in published)
    assert all(item["operation"] == "codex_turn" for item in published)
    report = next(item for item in published if item["path"] == "report.md")
    assert report["display_name"] == "report.md"
    expected_bytes = "报告内容".encode()
    assert report["size"] == len(expected_bytes)
    assert report["sha256"] == hashlib.sha256(expected_bytes).hexdigest()


def test_collect_turn_artifacts_without_snapshot_falls_back_to_changed_paths(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok", encoding="utf-8")

    published = _collect_turn_artifacts(workspace, None, ["result.txt"], turn_no=1)
    assert [item["path"] for item in published] == ["result.txt"]


def test_collect_turn_artifacts_returns_empty_when_nothing_changed(tmp_path: Path) -> None:
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir()
    (workspace / "input.txt").write_text("input", encoding="utf-8")
    before = snapshot_harness_workspace(workspace)

    assert _collect_turn_artifacts(workspace, before, [], turn_no=1) == []


def test_collect_changed_paths_ignores_deletes_and_duplicates() -> None:
    prepared = SimpleNamespace(changed_paths=[])
    item = {
        "changes": [
            {"path": "a.txt", "kind": "add"},
            {"path": "b.txt", "type": "delete"},
            {"path": "a.txt"},
            {"path": ""},
            "bad-entry",
        ]
    }

    CodexAgentRuntime._collect_changed_paths(prepared, item)

    assert prepared.changed_paths == ["a.txt"]


# ----------------------------------------------------------------------
# 下载端点 codex 分支
# ----------------------------------------------------------------------


def test_codex_artifact_download_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace_root(monkeypatch, tmp_path)
    workspace = root / "sess_codex"
    workspace.mkdir()
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_codex_session(db, workspace=workspace)
        (workspace / "result.txt").write_text("codex artifact body", encoding="utf-8")
        published = _collect_turn_artifacts(workspace, None, ["result.txt"], turn_no=1)
        db.add(
            Message(
                id="msg_codex_reply",
                tenant_id="tenant_demo",
                session_id="sess_codex",
                role="assistant",
                content="文件已生成。",
                metadata_json={"harness_artifacts": published},
            )
        )
        db.commit()

        response = download_harness_artifact(
            "sess_codex",
            "codex-turn-1",
            tenant_id="tenant_demo",
            path="result.txt",
            current_user=user,
            db=db,
        )

        assert asyncio.run(_read_response_body(response)) == b"codex artifact body"
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="result.txt"'
        )


def test_codex_artifact_download_rejects_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace_root(monkeypatch, tmp_path)
    workspace = root / "sess_codex"
    workspace.mkdir()
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_codex_session(db, workspace=workspace)
        (workspace / "result.txt").write_text("before", encoding="utf-8")
        published = _collect_turn_artifacts(workspace, None, ["result.txt"], turn_no=1)
        db.add(
            Message(
                id="msg_codex_reply",
                tenant_id="tenant_demo",
                session_id="sess_codex",
                role="assistant",
                content="文件已生成。",
                metadata_json={"harness_artifacts": published},
            )
        )
        (workspace / "result.txt").write_text("tampered", encoding="utf-8")
        db.commit()

        with pytest.raises(HTTPException) as changed:
            download_harness_artifact(
                "sess_codex",
                "codex-turn-1",
                tenant_id="tenant_demo",
                path="result.txt",
                current_user=user,
                db=db,
            )
        assert changed.value.status_code == 409


def test_codex_artifact_download_rejects_unsafe_and_unpublished_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace_root(monkeypatch, tmp_path)
    workspace = root / "sess_codex"
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok", encoding="utf-8")
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_codex_session(db, workspace=workspace)
        published = _collect_turn_artifacts(workspace, None, ["result.txt"], turn_no=1)
        db.add(
            Message(
                id="msg_codex_reply",
                tenant_id="tenant_demo",
                session_id="sess_codex",
                role="assistant",
                content="文件已生成。",
                metadata_json={"harness_artifacts": published},
            )
        )
        db.commit()

        for bad_path in ["../outside.txt", "unpublished.txt"]:
            with pytest.raises(HTTPException) as rejected:
                download_harness_artifact(
                    "sess_codex",
                    "codex-turn-1",
                    tenant_id="tenant_demo",
                    path=bad_path,
                    current_user=user,
                    db=db,
                )
            assert rejected.value.status_code == 404


def test_runtime_workspace_path_requires_whitelisted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace_root(monkeypatch, tmp_path)
    inside = root / "sess_ok"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert _runtime_workspace_path(
        SimpleNamespace(runtime_state_json={"workspace": str(inside)})
    ) == inside.resolve()
    assert (
        _runtime_workspace_path(
            SimpleNamespace(runtime_state_json={"workspace": str(outside)})
        )
        is None
    )
    assert _runtime_workspace_path(SimpleNamespace(runtime_state_json={})) is None


# ----------------------------------------------------------------------
# 会话删除清理
# ----------------------------------------------------------------------


def test_session_delete_removes_runtime_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace_root(monkeypatch, tmp_path)
    workspace = root / "sess_del"
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok", encoding="utf-8")
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_codex_session(db, workspace=workspace, session_id="sess_del")
        db.add(
            Message(
                id="msg_1",
                tenant_id="tenant_demo",
                session_id="sess_del",
                role="user",
                content="hi",
                metadata_json={},
            )
        )
        db.commit()

        result = delete_chat_session(
            "sess_del",
            tenant_id="tenant_demo",
            current_user=user,
            db=db,
        )

        assert result == {"status": "deleted"}
        assert not workspace.exists()


def test_session_delete_keeps_workspaces_outside_whitelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _workspace_root(monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere" / "sess_keep"
    outside.mkdir(parents=True)
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_codex_session(db, workspace=outside, session_id="sess_keep")
        db.commit()

        delete_chat_session(
            "sess_keep",
            tenant_id="tenant_demo",
            current_user=user,
            db=db,
        )

        assert outside.exists()
