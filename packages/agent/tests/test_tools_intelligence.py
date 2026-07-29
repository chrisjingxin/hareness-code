"""代码智能工具模块：验证 lsp 操作校验、stub 返回和 tool_search 匹配逻辑。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from harness_agent.tools_intelligence import lsp, tool_search


@pytest.mark.asyncio
async def test_lsp_invalid_action():
    """无效 action 应返回错误信息。"""
    result = await lsp("rename", "main.py")

    assert "error" in result
    assert "无效的操作类型" in result["error"]


@pytest.mark.asyncio
async def test_lsp_stub_no_server(tmp_path):
    """未配置 LSP 环境变量时应返回 stub 结果。"""
    target = tmp_path / "main.py"
    target.write_text("print('hello')", encoding="utf-8")

    with patch.dict("os.environ", {}, clear=True):
        result = await lsp("definition", "main.py", line=1, column=1, workspace_root=str(tmp_path))

    assert result["action"] == "definition"
    assert result["file_path"] == "main.py"
    assert result["results"] == []
    assert "LSP 服务未连接" in result["note"]


@pytest.mark.asyncio
async def test_lsp_file_not_found(tmp_path):
    """文件不存在时应返回错误。"""
    with patch.dict("os.environ", {}, clear=True):
        result = await lsp("hover", "nonexistent.py", workspace_root=str(tmp_path))

    assert "error" in result
    assert "文件不存在" in result["error"]


def test_tool_search_empty():
    """无已注册工具时应返回空结果和提示。"""
    result = tool_search("anything")

    assert result["results"] == []
    assert "无已注册的 MCP 工具" in result["note"]


def test_tool_search_matches_name():
    """应按工具名称进行子串匹配。"""
    tools = [
        {"name": "github_search", "description": "搜索 GitHub 仓库"},
        {"name": "file_read", "description": "读取文件内容"},
    ]
    result = tool_search("github", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "github_search"


def test_tool_search_matches_description():
    """应按工具描述进行子串匹配。"""
    tools = [
        {"name": "tool_a", "description": "执行数据库查询操作"},
        {"name": "tool_b", "description": "发送通知消息"},
    ]
    result = tool_search("数据库", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "tool_a"


def test_tool_search_case_insensitive():
    """搜索应大小写不敏感。"""
    tools = [
        {"name": "GitHub_Search", "description": "Search repositories"},
        {"name": "other_tool", "description": "无关描述"},
    ]
    result = tool_search("github", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "GitHub_Search"


def test_tool_search_limit():
    """搜索结果最多返回 20 条。"""
    tools = [{"name": f"tool_{i}", "description": "通用工具"} for i in range(30)]
    result = tool_search("工具", available_tools=tools)

    assert len(result["results"]) == 20
