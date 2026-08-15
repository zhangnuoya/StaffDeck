"""Fake Codex CLI for adapter tests.

Reads the prompt from stdin, emits canned JSONL in the real `codex exec --json`
shape, and captures argv + stdin to FAKE_CODEX_CAPTURE (JSON) for assertions.

Scenario selection via FAKE_CODEX_SCENARIO:
- (default) reasoning + command + agent_message, thread id "thread_fake_1"
- "multi_message": two agent_message items (progress narration + final reply)
- "no_message": completes without any agent_message
- "fail": emits an error event and exits non-zero
- "slow": sleeps FAKE_CODEX_SLOW_SECONDS before answering (timeout/cancel tests)
- "mcp_tool_success": staffdeck MCP tool call that succeeds
- "mcp_tool_error": staffdeck MCP tool call that fails at the JSON-RPC level
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

    if scenario == "knowledge_citation":
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_k",
                    "type": "mcp_tool_call",
                    "server": "staffdeck",
                    "tool": "query_knowledge",
                    "arguments": {"query": "考勤制度"},
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "[1] 迟到超过30分钟按旷工半天处理。\n\n[2] 请假需提前一天在系统申请。",
                            }
                        ],
                        "structured_content": {
                            "query": "考勤制度",
                            "chunks": [],
                            "selected_documents": [],
                            "selected_concepts": [],
                            "okf_citations": [],
                            "evidence_pack": [
                                {
                                    "chunk_id": "c1",
                                    "document_id": "d1",
                                    "bucket_id": "b1",
                                    "source_path": "docs/考勤制度.md",
                                    "section_path": "考勤制度 > 迟到早退",
                                    "summary": "迟到早退规定",
                                    "content": "迟到超过30分钟按旷工半天处理。",
                                    "excerpt": "迟到超过30分钟按旷工半天处理。",
                                    "relevance_score": 0.9,
                                    "confidence_reason": "引用来源摘要、章节路径或正文与查询相关",
                                },
                                {
                                    "chunk_id": "c2",
                                    "document_id": "d1",
                                    "bucket_id": "b1",
                                    "source_path": "docs/考勤制度.md",
                                    "section_path": "考勤制度 > 请假流程",
                                    "summary": "请假流程",
                                    "content": "请假需提前一天在系统申请。",
                                    "excerpt": "请假需提前一天在系统申请。",
                                    "relevance_score": 0.85,
                                    "confidence_reason": "引用来源摘要、章节路径或正文与查询相关",
                                },
                            ],
                            "trace": [],
                        },
                    },
                },
            }
        )
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_k2",
                    "type": "agent_message",
                    "text": "根据 [2] 请假需提前一天在系统申请；[1] 提到迟到超过30分钟按旷工半天处理。",
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

    if scenario == "mcp_tool_success":
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_m",
                    "type": "mcp_tool_call",
                    "server": "staffdeck",
                    "tool": "mysql_mysql_query",
                    "arguments": {"sql": "select 3"},
                    "result": {
                        "content": [{"type": "text", "text": "[[3]]"}],
                    },
                },
            }
        )
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_m2",
                    "type": "agent_message",
                    "text": "查询结果为 3。",
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

    if scenario == "mcp_tool_error":
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_me",
                    "type": "mcp_tool_call",
                    "server": "staffdeck",
                    "tool": "unknown_tool",
                    "arguments": {},
                    "error": "unknown tool: unknown_tool",
                },
            }
        )
        _emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_me2",
                    "type": "agent_message",
                    "text": "工具调用失败，请检查工具名。",
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
