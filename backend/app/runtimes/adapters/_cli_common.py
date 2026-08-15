"""Shared helpers for CLI-based agent runtimes (codex, claude_code)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from collections.abc import Iterator
from typing import Any

STREAM_REPLY_CHUNK_SIZE = 96


def parse_jsonl(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def reply_chunks(text: str) -> Iterator[str]:
    for index in range(0, len(text), STREAM_REPLY_CHUNK_SIZE):
        yield text[index : index + STREAM_REPLY_CHUNK_SIZE]


def kill_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            # 子进程以 start_new_session 启动：杀整个进程组，
            # 覆盖包装层（如 sudo -u appuser codex）下的真实 codex 进程。
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
