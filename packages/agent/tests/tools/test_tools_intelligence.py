"""代码智能工具模块：验证 lsp 操作校验、stub 返回和 tool_search 打分逻辑。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from harness_agent.tools.tools_intelligence import lsp, tool_search


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


def _mcp_tool(name: str, description: str, hint: str = "github") -> dict[str, object]:
    """构造一个 MCP 风格工具候选（is_mcp=True + search_hint + input_schema）。"""
    return {
        "name": name,
        "description": description,
        "search_hint": hint,
        "is_mcp": True,
        "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}}},
    }


def test_tool_search_empty():
    """无已注册工具时应返回空结果和提示。"""
    result = tool_search("anything")

    assert result["results"] == []
    assert "无已注册的 MCP 工具" in result["note"]


def test_tool_search_empty_query():
    """空白查询应返回提示而不是崩溃。"""
    tools = [_mcp_tool("github_search", "搜索 GitHub 仓库")]
    result = tool_search("   ", available_tools=tools)

    assert result["results"] == []
    assert "查询不能为空" in result["note"]


def test_tool_search_select_exact():
    """select: 模式按名精确返回，忽略缺失名并提示。"""
    tools = [
        _mcp_tool("github_create_issue", "创建 issue"),
        _mcp_tool("github_list_repos", "列出仓库"),
        _mcp_tool("slack_send", "发消息"),
    ]
    result = tool_search("select:github_create_issue,slack_send,missing_tool", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert names == ["github_create_issue", "slack_send"]
    assert "missing_tool" in result["note"]


def test_tool_search_select_case_insensitive_dedupe():
    """select: 大小写不敏感且去重。"""
    tools = [_mcp_tool("GitHub_Search", "搜索仓库")]
    result = tool_search("select:github_search,GITHUB_SEARCH", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "GitHub_Search"


def test_tool_search_select_no_missing_no_note():
    """select: 全部命中时不带 note 字段。"""
    tools = [_mcp_tool("github_search", "搜索仓库")]
    result = tool_search("select:github_search", available_tools=tools)

    assert len(result["results"]) == 1
    assert "note" not in result


def test_tool_search_bare_name_fast_path():
    """裸工具名与名称完全一致时直接返回（子代理/压缩后常见）。"""
    tools = [
        _mcp_tool("github_create_issue", "创建 issue"),
        _mcp_tool("github_list_repos", "列出仓库"),
    ]
    result = tool_search("github_create_issue", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "github_create_issue"


def test_tool_search_keyword_matches_name_part():
    """关键词命中名称部件（server__tool 拆分）优先于描述命中。"""
    tools = [
        _mcp_tool("github_create_issue", "创建 issue"),
        _mcp_tool("slack_send", "发送 GitHub 通知"),
    ]
    result = tool_search("issue", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert names[0] == "github_create_issue"


def test_tool_search_mcp_weighs_higher():
    """同层级比较：MCP 工具名称部件命中的权重（6）高于内置工具（5）。"""
    tools = [
        {"name": "myrepo_info", "description": "仓库信息", "is_mcp": False},
        {"name": "github_list_repos", "description": "列出仓库", "is_mcp": True},
    ]
    result = tool_search("repo", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert names[0] == "github_list_repos"


def test_tool_search_required_term_filters():
    """+必选词未在名称部件/描述/search_hint 命中时排除候选。"""
    tools = [
        _mcp_tool("github_create_issue", "创建 issue"),
        _mcp_tool("slack_send", "发消息"),
    ]
    # issue 只存在于 github_create_issue 的名称部件；slack_send 的
    # search_hint=github 无法通过 +issue 预筛。
    result = tool_search("+issue 创建", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert names == ["github_create_issue"]


def test_tool_search_required_term_allows_hint():
    """+必选词命中 search_hint（服务器名）也通过预筛（参考实现语义）。"""
    tools = [
        _mcp_tool("create_issue", "创建 issue", hint="github"),
        _mcp_tool("slack_send", "发消息", hint="slack"),
    ]
    result = tool_search("+github issue", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert "create_issue" in names
    assert "slack_send" not in names


def test_tool_search_word_boundary():
    """ASCII 词匹配使用词边界：git 不命中 digit 的描述（名称部件命中优先）。"""
    tools = [
        _mcp_tool("github_search", "搜索代码"),
        {"name": "digital_clock", "description": "digit 显示时间", "is_mcp": False},
    ]
    result = tool_search("git", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    # github_search 名称部件精确命中（12）排第一；digital_clock 仅靠全名
    # 兜底（3）排后——词边界保证了描述 "digit" 不被误判为 "git"。
    assert names[0] == "github_search"
    assert "digital_clock" not in names[:1]


def test_tool_search_chinese_substring():
    """中文词退化为子串匹配，不应漏掉合法命中。"""
    tools = [_mcp_tool("db_query", "执行数据库查询操作")]
    result = tool_search("数据库", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "db_query"


def test_tool_search_stop_words_filtered():
    """停用词被过滤后仍可命中剩余词。"""
    tools = [
        _mcp_tool("web_fetch", "获取网页内容"),
        {"name": "ls", "description": "列出目录", "is_mcp": False},
    ]
    result = tool_search("use web fetch", available_tools=tools)

    names = [item["name"] for item in result["results"]]
    assert names == ["web_fetch"]


def test_tool_search_search_hint_match():
    """search_hint（服务器名）命中应使工具进入结果。"""
    tools = [_mcp_tool("create_issue", "创建 issue", hint="slack")]
    result = tool_search("slack", available_tools=tools)

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "create_issue"


def test_tool_search_results_carry_schema_and_hint():
    """结果条目携带 search_hint 与 input_schema。"""
    tools = [_mcp_tool("github_create_issue", "创建 issue")]
    result = tool_search("issue", available_tools=tools)

    entry = result["results"][0]
    assert entry["search_hint"] == "github"
    assert entry["input_schema"]["properties"]["repo"]["type"] == "string"


def test_tool_search_no_match_note():
    """无命中时应返回提示。"""
    tools = [_mcp_tool("github_create_issue", "创建 issue")]
    result = tool_search("数据库查询", available_tools=tools)

    assert result["results"] == []
    assert "未找到匹配工具" in result["note"]


def test_tool_search_limit():
    """搜索结果最多返回 20 条，且按分数降序。"""
    tools = [
        {"name": f"tool_{i}", "description": "通用工具", "is_mcp": False}
        for i in range(30)
    ]
    result = tool_search("工具", available_tools=tools)

    assert len(result["results"]) == 20
