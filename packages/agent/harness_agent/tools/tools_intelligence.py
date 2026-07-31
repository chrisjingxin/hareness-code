"""代码智能工具：提供 LSP 语言服务查询和 MCP 工具搜索能力。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_VALID_LSP_ACTIONS = ("definition", "references", "diagnostics", "hover")
_MAX_SEARCH_RESULTS = 20


async def lsp(
    action: str,
    file_path: str,
    line: int | None = None,
    column: int | None = None,
    workspace_root: str = "",
) -> dict[str, Any]:
    """通过语言服务协议获取代码智能信息。

    Args:
        action: 操作类型（definition/references/diagnostics/hover）。
        file_path: 目标文件路径（相对于工作区）。
        line: 行号（从 1 开始）。
        column: 列号（从 1 开始）。
        workspace_root: 工作区根目录。

    Returns:
        操作结果字典。
    """
    try:
        # 验证 action 合法性
        if action not in _VALID_LSP_ACTIONS:
            return {"error": f"无效的操作类型: {action}，支持: {', '.join(_VALID_LSP_ACTIONS)}"}

        # 验证目标文件存在
        resolved = Path(workspace_root) / file_path if workspace_root else Path(file_path)
        if not resolved.is_file():
            return {"error": f"文件不存在: {file_path}"}

        # 预留 LSP 客户端连接点：通过环境变量配置语言服务器命令
        lsp_command = os.environ.get("HARNESS_LSP_COMMAND", "")
        if not lsp_command:
            return {
                "action": action,
                "file_path": file_path,
                "results": [],
                "note": "LSP 服务未连接，请配置语言服务器",
            }

        # TODO: 实际 LSP 客户端通信（启动子进程、发送 JSON-RPC 请求）
        return {
            "action": action,
            "file_path": file_path,
            "results": [],
            "note": "LSP 服务未连接，请配置语言服务器",
        }
    except Exception as exc:
        return {"error": str(exc)}


def tool_search(
    query: str,
    available_tools: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """搜索可用的 MCP 外部工具。

    Args:
        query: 搜索关键词。
        available_tools: 已注册工具列表，每项含 name 和 description。

    Returns:
        {"results": [{"name": str, "description": str}]}
    """
    if not available_tools:
        return {"results": [], "note": "无已注册的 MCP 工具"}

    keyword = query.lower()
    matched: list[dict[str, str]] = []
    for tool in available_tools:
        name = tool.get("name", "")
        description = tool.get("description", "")
        if keyword in name.lower() or keyword in description.lower():
            matched.append({"name": name, "description": description})
            if len(matched) >= _MAX_SEARCH_RESULTS:
                break

    return {"results": matched}
