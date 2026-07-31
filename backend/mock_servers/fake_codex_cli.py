"""Fake Codex CLI for adapter tests.

Reads the prompt from stdin, emits canned JSONL in the real `codex exec --json`
shape, and captures argv + stdin to FAKE_CODEX_CAPTURE (JSON) for assertions.

Scenario selection via FAKE_CODEX_SCENARIO:
- (default) reasoning + command + agent_message, thread id "thread_fake_1"
- "multi_message": two agent_message items (progress narration + final reply)
- "no_message": completes without any agent_message
- "fail": emits an error event and exits non-zero
- "slow": sleeps FAKE_CODEX_SLOW_SECONDS before answering (timeout/cancel tests)
"""

from __future__ import annotations

import json
import os
import sys
import time

THREAD_ID = "thread_fake_1"


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "")
    prompt = sys.stdin.read()
    capture_path = os.environ.get("FAKE_CODEX_CAPTURE", "")
    if capture_path:
        with open(capture_path, "w", encoding="utf-8") as handle:
            json.dump({"argv": sys.argv, "prompt": prompt}, handle, ensure_ascii=False)

    if scenario == "slow":
        time.sleep(float(os.environ.get("FAKE_CODEX_SLOW_SECONDS", "30")))

    _emit({"type": "thread.started", "thread_id": THREAD_ID})
    _emit({"type": "turn.started"})

    if scenario == "fail":
        _emit({"type": "error", "message": "simulated codex failure"})
        return 1

    _emit(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": "先思考用户需求"},
        }
    )
    _emit(
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "python report.py",
                "aggregated_output": "rows: 3",
                "exit_code": 0,
            },
        }
    )
    if scenario == "multi_message":
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "item_2", "type": "agent_message", "text": "我先整理一下数据。"},
            }
        )
        _emit(
            {
                "type": "item.completed",
                "item": {"id": "item_3", "type": "agent_message", "text": "最终答复：报告已生成。"},
            }
        )
    elif scenario != "no_message":
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "agent_message",
                    "text": "假 Codex 回复：任务完成。",
                },
            }
        )
    _emit(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
