"""Fake Claude Code CLI for adapter tests.

Emits canned stream-json in the real `claude -p --output-format stream-json`
shape and captures argv + stdin to FAKE_CLAUDE_CAPTURE (JSON).

Scenarios via FAKE_CLAUDE_SCENARIO:
- (default): init + thinking + progressive text + tool_use + success result
- "fail": result with is_error=true
- "no_result": exits without a result event
- "slow": sleeps FAKE_CLAUDE_SLOW_SECONDS before answering
"""

from __future__ import annotations

import json
import os
import sys
import time

SESSION_ID = "claude_fake_session_1"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _assistant(content: list[dict], message_id: str = "msg_1") -> dict:
    return {
        "type": "assistant",
        "message": {"id": message_id, "role": "assistant", "content": content},
    }


def main() -> int:
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "")
    prompt = sys.stdin.read()
    capture_path = os.environ.get("FAKE_CLAUDE_CAPTURE", "")
    if capture_path:
        with open(capture_path, "w", encoding="utf-8") as handle:
            json.dump({"argv": sys.argv, "prompt": prompt}, handle, ensure_ascii=False)

    if scenario == "slow":
        time.sleep(float(os.environ.get("FAKE_CLAUDE_SLOW_SECONDS", "30")))

    _emit({"type": "system", "subtype": "init", "session_id": SESSION_ID, "model": "fake-claude"})

    if scenario == "no_result":
        return 0

    if scenario == "fail":
        _emit(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "simulated claude failure",
            }
        )
        return 1

    _emit(_assistant([{"type": "thinking", "thinking": "先理解任务"}]))
    _emit(_assistant([{"type": "text", "text": "假 Claude"}]))
    _emit(_assistant([{"type": "text", "text": "假 Claude 回复：任务完成。"}]))
    _emit(
        _assistant(
            [{"type": "tool_use", "name": "Write", "input": {"file_path": "report.py"}}],
            message_id="msg_2",
        )
    )
    _emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "假 Claude 回复：任务完成。",
            "total_cost_usd": 0.001,
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
