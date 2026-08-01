from __future__ import annotations

import os
import sys

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db.models import Tenant
from app.tools.mcp_client import _resolve_stdio_launch, _stdio_command, execute_mcp_tool


def _mock_mcp_server_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "mock_servers" / "mcp_stdio_server.py"


def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


# ---------------------------------------------------------------------------
# _resolve_stdio_launch: Windows 批处理 shim 中转
# ---------------------------------------------------------------------------


def test_resolve_stdio_launch_wraps_cmd_shim(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.mcp_client.os.name", "nt")
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda exe: r"C:\tools\npx.cmd" if exe == "npx" else None,
    )
    assert _resolve_stdio_launch(["npx", "-y", "some-server"]) == [
        "cmd",
        "/c",
        r"C:\tools\npx.cmd",
        "-y",
        "some-server",
    ]


def test_resolve_stdio_launch_wraps_explicit_bat_path(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.mcp_client.os.name", "nt")
    monkeypatch.setattr("app.tools.mcp_client.shutil.which", lambda exe: exe)
    assert _resolve_stdio_launch([r"C:\tools\server.bat", "--flag"]) == [
        "cmd",
        "/c",
        r"C:\tools\server.bat",
        "--flag",
    ]


def test_resolve_stdio_launch_keeps_real_executables(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.mcp_client.os.name", "nt")
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda exe: r"C:\Python311\python.exe",
    )
    assert _resolve_stdio_launch(["python", "server.py"]) == ["python", "server.py"]


def test_resolve_stdio_launch_is_a_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.mcp_client.os.name", "posix")
    assert _resolve_stdio_launch(["npx", "-y", "srv"]) == ["npx", "-y", "srv"]


def test_stdio_command_routes_through_shim_resolution(monkeypatch) -> None:
    monkeypatch.setattr("app.tools.mcp_client.os.name", "nt")
    monkeypatch.setattr(
        "app.tools.mcp_client.shutil.which",
        lambda exe: r"C:\tools\uvx.cmd" if exe == "uvx" else None,
    )
    assert _stdio_command({"command": "uvx", "args": ["mcp-server"]})[0] == "cmd"


# ---------------------------------------------------------------------------
# 端到端：.cmd shim 拉起真实 stdio server（用户的 WinError 2 场景）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="cmd shim 仅 Windows 需要")
def test_execute_mcp_tool_through_cmd_shim(tmp_path) -> None:
    shim = tmp_path / "mock_mcp.cmd"
    shim.write_text(
        f'@echo off\r\n"{sys.executable}" "{_mock_mcp_server_path()}" %*\r\n',
        encoding="ascii",
    )
    with _make_db() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.commit()
        result = execute_mcp_tool(
            {
                "transport": "stdio",
                "command": str(shim),
                "args": [],
            },
            {"text": "经由 cmd shim"},
            timeout_seconds=15.0,
            tool_name="echo",
        )
    assert result == {"text": "经由 cmd shim", "length": 11}
