"""零依赖 Demo LSP Server，为 .demo 文件返回可观察结果。"""

from __future__ import annotations

import json
import sys
from typing import Any


def _read() -> dict[str, Any]:
    """读取一帧 Content-Length LSP 消息。"""
    content_length: int | None = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        if line in {b"\r\n", b"\n"}:
            break
        if line.lower().startswith(b"content-length:"):
            content_length = int(line.split(b":", 1)[1].strip())
    if content_length is None:
        raise ValueError("missing Content-Length")
    value = json.loads(sys.stdin.buffer.read(content_length))
    if not isinstance(value, dict):
        raise ValueError("LSP message must be an object")
    return value


def _write(message: dict[str, Any]) -> None:
    """写一帧 LSP 消息。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    )
    sys.stdout.buffer.flush()


def _location(uri: str) -> dict[str, Any]:
    """返回文件首行的稳定位置。"""
    return {
        "uri": uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 19},
        },
    }


def main() -> None:
    """处理 Harness 当前会发送的最小 LSP 方法集。"""
    while True:
        message = _read()
        method = message.get("method")
        if "id" not in message:
            if method == "exit":
                return
            continue
        request_id = message["id"]
        params = message.get("params")
        values = params if isinstance(params, dict) else {}
        document = values.get("textDocument")
        uri = document.get("uri", "") if isinstance(document, dict) else ""
        if method == "initialize":
            result: object = {
                "capabilities": {
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "hoverProvider": True,
                    "diagnosticProvider": {
                        "interFileDependencies": False,
                        "workspaceDiagnostics": False,
                    },
                },
                "serverInfo": {
                    "name": "harness-full-demo-lsp",
                    "version": "1.0.0",
                },
            }
        elif method == "textDocument/definition":
            result = [_location(str(uri))]
        elif method == "textDocument/references":
            result = [_location(str(uri))]
        elif method == "textDocument/hover":
            result = {
                "contents": {
                    "kind": "markdown",
                    "value": "**Harness Full Demo LSP is active.**",
                }
            }
        elif method == "textDocument/diagnostic":
            result = {"kind": "full", "items": []}
        elif method == "shutdown":
            result = None
        else:
            result = []
        _write({"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    main()
