"""零依赖 MCP stdio Server，提供只读 Plugin 组件库存工具。"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SERVER_INFO = {"name": "harness-full-demo-mcp", "version": "1.0.0"}


def _write(message: dict[str, Any]) -> None:
    """向 stdout 写一行 MCP JSON-RPC；诊断不得污染 stdout。"""
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: object) -> None:
    """返回成功响应。"""
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    """返回不包含本机绝对路径的错误响应。"""
    _write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _plugin_inventory(arguments: object) -> dict[str, Any]:
    """统计已安装 Plugin 包内的文件类型，拒绝路径逃逸。"""
    values = arguments if isinstance(arguments, dict) else {}
    relative = values.get("path", ".")
    if not isinstance(relative, str) or not relative:
        raise ValueError("path must be a non-empty string")
    package_root = Path(os.getcwd()).resolve()
    target = (package_root / relative).resolve()
    if target != package_root and package_root not in target.parents:
        raise ValueError("path must stay inside the Plugin package")
    if not target.exists():
        raise ValueError("path does not exist")

    files = sorted(path for path in target.rglob("*") if path.is_file())
    suffixes = Counter(path.suffix or "<no-extension>" for path in files[:5000])
    return {
        "root": target.relative_to(package_root).as_posix() or ".",
        "file_count": len(files),
        "top_extensions": suffixes.most_common(10),
        "has_readme": any(path.name.lower().startswith("readme") for path in files),
        "sample_files": [
            path.relative_to(package_root).as_posix()
            for path in files[:8]
        ],
        "truncated": len(files) > 5000,
    }


def _handle(message: dict[str, Any]) -> None:
    """处理 MCP initialize、工具发现和工具调用。"""
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return
    if method == "initialize":
        params = message.get("params")
        requested = (
            params.get("protocolVersion")
            if isinstance(params, dict)
            else None
        )
        _result(
            request_id,
            {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(
            request_id,
            {
                "tools": [
                    {
                        "name": "plugin_inventory",
                        "description": (
                            "只读统计已安装 Plugin 包的文件数量、主要扩展名和代表性文件"
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Plugin 包内相对路径，默认 .",
                                    "default": ".",
                                }
                            },
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )
        return
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != "plugin_inventory":
            _error(request_id, -32602, "unknown tool")
            return
        try:
            summary = _plugin_inventory(params.get("arguments"))
        except ValueError as exc:
            _result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
            return
        _result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(summary, ensure_ascii=False, indent=2),
                    }
                ]
            },
        )
        return
    if method in {"resources/list", "prompts/list"}:
        _result(request_id, {"resources": []} if method == "resources/list" else {"prompts": []})
        return
    _error(request_id, -32601, "method not found")


def main() -> None:
    """持续读取 newline-delimited MCP JSON-RPC。"""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                _handle(value)
        except Exception as exc:
            print(f"demo MCP diagnostic: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
