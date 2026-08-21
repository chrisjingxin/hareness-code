"""Harness 扩展工具注册：验证 tool_search 候选来自运行时真实 MCP 工具。"""

from __future__ import annotations

import json

from langchain_core.tools import StructuredTool

from harness_agent.tools.harness_tools import create_harness_tools


def _fake_mcp_tool(name: str, description: str) -> StructuredTool:
    """构造一个带参数 schema 的 mock MCP 工具（StructuredTool.from_function）。"""
    def _impl(repo: str) -> str:
        return f"handled {repo}"

    return StructuredTool.from_function(
        func=_impl,
        name=name,
        description=description,
    )


def _tool_search_instance(tools: list[StructuredTool]) -> StructuredTool:
    instance = next(t for t in tools if t.name == "tool_search")
    return instance


def test_removed_patch_tool_is_not_registered() -> None:
    """扩展工具注册和 tool_search 候选都不再包含历史补丁工具。"""
    tools = create_harness_tools(
        "/tmp",
        deferred_builtin_names=frozenset({"apply_patch", "lsp"}),
    )

    assert "apply_patch" not in {tool.name for tool in tools}
    search = _tool_search_instance(tools)
    result = json.loads(search.invoke({"query": "apply_patch"}))
    assert all(item["name"] != "apply_patch" for item in result["results"])


def test_tool_search_no_mcp_tools_returns_empty():
    """未传入 mcp_tools 时 tool_search 保持"无已注册的 MCP 工具"语义。"""
    tools = create_harness_tools("/tmp")
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "anything"}))

    assert result["results"] == []
    assert "无已注册的 MCP 工具" in result["note"]


def test_tool_search_searches_real_mcp_tools():
    """传入 MCP 工具后，tool_search 返回真实工具名/描述/参数 schema。"""
    tools = create_harness_tools(
        "/tmp",
        mcp_tools=[
            _fake_mcp_tool("github_create_issue", "在 GitHub 上创建 issue"),
            _fake_mcp_tool("slack_send", "发送 Slack 消息"),
        ],
    )
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "github"}))

    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["name"] == "github_create_issue"
    assert entry["description"] == "在 GitHub 上创建 issue"
    # search_hint 从 ``{server}_{tool}`` 名称提取服务器名。
    assert entry["search_hint"] == "github"
    # input_schema 从 mock 工具函数签名提取。
    assert entry["input_schema"]["properties"]["repo"]["type"] == "string"


def test_tool_search_select_by_name():
    """select: 模式可在注册的 MCP 工具中精确选择。"""
    tools = create_harness_tools(
        "/tmp",
        mcp_tools=[
            _fake_mcp_tool("github_create_issue", "在 GitHub 上创建 issue"),
            _fake_mcp_tool("slack_send", "发送 Slack 消息"),
        ],
    )
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "select:slack_send,missing"}))

    assert [item["name"] for item in result["results"]] == ["slack_send"]
    assert "missing" in result["note"]


def test_tool_search_ignores_nameless_tools():
    """没有 name 的工具不进入候选，不破坏搜索。"""
    nameless = _fake_mcp_tool("gh_search", "搜索")
    object.__setattr__(nameless, "name", "")
    tools = create_harness_tools("/tmp", mcp_tools=[nameless])
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "search"}))

    assert result["results"] == []


def test_tool_search_includes_deferred_builtin():
    """延迟加载模式：内置低频工具进入候选（is_mcp=False），且不暴露打分标记。"""
    tools = create_harness_tools(
        "/tmp",
        deferred_builtin_names=frozenset({"lsp", "web_search"}),
    )
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "代码智能"}))

    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["name"] == "lsp"
    # is_mcp 是候选内部打分标记，不暴露给模型；search_hint 为内置工具省略。
    assert "is_mcp" not in entry


def test_tool_search_deferred_names_off_by_default():
    """未传 deferred_builtin_names 时保持 Phase 1 语义：内置工具不进候选。"""
    tools = create_harness_tools("/tmp")
    search = _tool_search_instance(tools)

    result = json.loads(search.invoke({"query": "语言服务"}))

    assert result["results"] == []


def test_tools_exclude_removed_background_tools():
    """已下线假工具面：monitor/task_output/task_stop 不得注册为模型工具。"""
    tools = create_harness_tools("/tmp")
    names = {tool.name for tool in tools}
    assert names.isdisjoint({"monitor", "task_output", "task_stop"})
