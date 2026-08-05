"""Harness Full Demo 的 Claude command Hook。

PreToolUse 会阻止包含 demo-forbidden 的 Bash 命令；所有事件写入 Plugin data 目录。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def main() -> None:
    """读取 Claude Hook stdin，写审计记录并输出兼容 JSON。"""
    mode = sys.argv[1]
    audit_path = Path(sys.argv[2])
    payload = json.load(sys.stdin)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp_ms": int(time.time() * 1000),
        "mode": mode,
        "event": payload.get("hook_event_name"),
        "tool_name": payload.get("tool_name"),
    }
    with audit_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    command = str(payload.get("tool_input", {}).get("command", ""))
    if mode == "pre" and "demo-forbidden" in command:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Harness Full Demo Hook blocked demo-forbidden"
                        ),
                    }
                }
            )
        )
        return
    print("{}")


if __name__ == "__main__":
    main()
