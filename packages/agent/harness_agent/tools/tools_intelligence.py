"""代码智能工具：提供 LSP 语言服务查询和 MCP 工具搜索能力。

``tool_search`` 的打分方案参考 Qwen Code ``tool-search.ts``（scoreTool）与
Claude Code ``ToolSearchTool.ts``（searchToolsWithKeywords）：双查询模式
（select: 精确选择 / 关键词打分）、``+word`` 必选词预筛、名称部件拆分
（MCP 名 ``server__tool`` 与 CamelCase/下划线）、MCP 工具加权。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from harness_agent.plugins.runtime import PluginLspManager

_VALID_LSP_ACTIONS = ("definition", "references", "diagnostics", "hover")
_MAX_SEARCH_RESULTS = 20

# 停用词：参考 Qwen Code TOOL_SEARCH_STOP_WORDS，过滤噪声词避免淹没打分。
_TOOL_SEARCH_STOP_WORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "on", "in", "with", "and", "or",
    "is", "are", "do", "does", "how", "what", "why", "use", "using",
    "search", "find", "tool",
})

# 打分权重（参考 Qwen scoreTool / Claude searchToolsWithKeywords，权重同源）。
# MCP 工具权重更高：MCP 工具始终是外部注入的候选，发现是模型到达它们的
# 主要途径，值得在排序中上浮。
_SCORE_NAME_EXACT_MCP = 12
_SCORE_NAME_EXACT_BUILTIN = 10
_SCORE_NAME_SUBSTR_MCP = 6
_SCORE_NAME_SUBSTR_BUILTIN = 5
_SCORE_NAME_FULL_FALLBACK = 3
_SCORE_SEARCH_HINT = 4
_SCORE_DESCRIPTION = 2


async def lsp(
    action: str,
    file_path: str,
    line: int | None = None,
    column: int | None = None,
    workspace_root: str = "",
    manager: "PluginLspManager | None" = None,
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

        if manager is not None:
            return await manager.query(
                action,
                file_path,
                line,
                column,
                workspace_root,
            )

        # 没有 Plugin runtime 时保留旧的显式环境提示。
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
    available_tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """搜索可用的 MCP 外部工具。

    支持三种查询方式（参考 Qwen Code ToolSearch 双模式 + Claude 裸名快路径）：

    - ``select:name1,name2``：按工具名精确选择（逗号分隔，大小写不敏感），不走打分。
    - 裸工具名：与某个工具名完全一致时直接返回（子代理/压缩后模型常直接输名字）。
    - 自由关键词：对名称部件/描述/search_hint 打分排序；``+word`` 前缀标记必选词，
      必选词未全部命中（名称部件/描述/search_hint）的工具被排除。

    Args:
        query: 搜索关键词。
        available_tools: 已注册工具列表，每项含 name、description，可选
            search_hint（提升匹配的策展词，如服务器名）、is_mcp（MCP 工具加权）
            与 input_schema（工具参数 JSON Schema）。

    Returns:
        {"results": [{"name", "description", "search_hint", "input_schema"}]}
    """
    if not available_tools:
        return {"results": [], "note": "无已注册的 MCP 工具"}

    query = (query or "").strip()
    if not query:
        return {"results": [], "note": "查询不能为空，可尝试工具名或关键词"}

    # 模式一：select: 精确选择（逗号分隔，大小写不敏感，去重）。
    select_match = re.match(r"^select:(.+)$", query, re.IGNORECASE)
    if select_match:
        by_lower = {str(t.get("name", "")).lower(): t for t in available_tools}
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        missing: list[str] = []
        for raw in select_match.group(1).split(","):
            name = raw.strip()
            if not name:
                continue
            entry = by_lower.get(name.lower())
            if entry is not None and name.lower() not in seen:
                found.append(entry)
                seen.add(name.lower())
            elif entry is None:
                missing.append(name)
        results = [_result_entry(entry) for entry in found]
        if missing:
            results_note = f"未找到工具: {', '.join(missing)}"
        else:
            results_note = None
        return {"results": results} if results_note is None else {"results": results, "note": results_note}

    # 模式二：裸工具名精确匹配快路径（Claude :194-205）。
    by_lower = {str(t.get("name", "")).lower(): t for t in available_tools}
    exact = by_lower.get(query.lower())
    if exact is not None:
        return {"results": [_result_entry(exact)]}

    # 模式三：自由关键词打分。
    terms = _tokenize(query)
    if not terms:
        return {"results": [], "note": "未找到匹配工具，可尝试更宽泛的关键词"}
    required = [t[1:] for t in terms if t.startswith("+") and len(t) > 1]
    optional = [t for t in terms if not t.startswith("+")]
    # + 词既做必选预筛，也参与打分（Claude allScoringTerms 语义）。
    scoring_terms = [*required, *optional] if required else terms
    patterns = _compile_term_patterns(scoring_terms)

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in available_tools:
        if required and not _matches_required(entry, required, patterns):
            continue
        score = _score_tool(entry, scoring_terms, patterns)
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    results = [_result_entry(entry) for _, entry in scored[:_MAX_SEARCH_RESULTS]]
    if not results:
        return {"results": [], "note": "未找到匹配工具，可尝试更宽泛的关键词"}
    return {"results": results}


def _result_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """把候选条目投影为搜索结果；旧字段 name/description 保持不变以向后兼容。"""
    result: dict[str, Any] = {
        "name": str(entry.get("name", "")),
        "description": str(entry.get("description", "")),
    }
    if entry.get("search_hint"):
        result["search_hint"] = str(entry["search_hint"])
    if entry.get("input_schema") is not None:
        result["input_schema"] = entry["input_schema"]
    return result


def _tokenize(query: str) -> list[str]:
    """小写、按空白拆分、过滤停用词与过短词（参考 Qwen tokenize）。"""
    words: list[str] = []
    for raw in query.lower().split():
        word = raw.strip()
        if not word or len(word) < 2 or word in _TOOL_SEARCH_STOP_WORDS:
            continue
        words.append(word)
    return words


def _split_name_parts(name: str) -> list[str]:
    """把工具名拆成可匹配部件：CamelCase→空格、_→空格、小写拆分。

    例如 ``github__create_issue`` → [github, create, issue]，
    ``exitPlanMode`` → [exit, plan, mode]。
    """
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").lower()
    return spaced.split()


def _compile_term_patterns(terms: list[str]) -> dict[str, re.Pattern[str]]:
    """为 ASCII 词编译词边界正则；非 ASCII 词（如中文）退化为子串匹配。

    ``\\b`` 在 Python Unicode 模式下把中文字符视为 \\w，词边界正则对中文词
    内部子串不成立，会漏掉"数据库"匹配"数据库查询"这类合法命中。
    """
    patterns: dict[str, re.Pattern[str]] = {}
    for term in terms:
        if term.isascii():
            patterns[term] = re.compile(rf"\b{re.escape(term)}\b")
        else:
            patterns[term] = re.compile(re.escape(term))
    return patterns


def _matches_required(
    entry: dict[str, Any],
    required: list[str],
    patterns: dict[str, re.Pattern[str]],
) -> bool:
    """必选词全部命中（名称部件或描述或 search_hint 之一）才放行。"""
    parts = _split_name_parts(str(entry.get("name", "")))
    description = str(entry.get("description", "")).lower()
    hint = str(entry.get("search_hint", "")).lower()
    return all(
        term in parts or patterns[term].search(description)
        or (hint and patterns[term].search(hint))
        for term in required
    )


def _score_tool(
    entry: dict[str, Any],
    terms: list[str],
    patterns: dict[str, re.Pattern[str]],
) -> int:
    """对单个工具按词累加打分（参考 Qwen scoreTool / Claude searchToolsWithKeywords）。"""
    is_mcp = bool(entry.get("is_mcp", False))
    name = str(entry.get("name", ""))
    parts = _split_name_parts(name)
    full_lower = name.lower()
    description = str(entry.get("description", "")).lower()
    hint = str(entry.get("search_hint", "")).lower()
    exact_weight = _SCORE_NAME_EXACT_MCP if is_mcp else _SCORE_NAME_EXACT_BUILTIN
    substr_weight = _SCORE_NAME_SUBSTR_MCP if is_mcp else _SCORE_NAME_SUBSTR_BUILTIN

    total = 0
    for term in terms:
        pattern = patterns[term]
        if term in parts:
            total += exact_weight
        elif any(term in part for part in parts):
            total += substr_weight
        elif term in full_lower and total == 0:
            # 全名兜底：该工具此前无任何得分时，全名子串命中仍让其入候选。
            total += _SCORE_NAME_FULL_FALLBACK
        if hint and pattern.search(hint):
            total += _SCORE_SEARCH_HINT
        if pattern.search(description):
            total += _SCORE_DESCRIPTION
    return total
