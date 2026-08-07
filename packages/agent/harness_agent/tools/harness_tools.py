"""Harness 扩展工具注册：将新增工具函数包装为 LangChain BaseTool 并注入 Agent 图。

deepagents 框架仅自动注入 ls/read_file/write_file/edit_file/glob/grep/execute/
write_todos/task 等核心工具。本模块负责将 web_search、web_fetch、delete_file、
apply_patch、lsp、tool_search、memory_search、memory_save、enter_plan_mode、
exit_plan_mode、task_output、task_stop、monitor 等扩展工具包装为 BaseTool 实例，
使 Agent 在运行时能实际调用它们。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool

from harness_agent.tools.tools_web import web_fetch as _web_fetch_impl
from harness_agent.tools.tools_web import web_search as _web_search_impl
from harness_agent.tools.tools_file import delete_file as _delete_file_impl
from harness_agent.tools.tools_file import apply_patch as _apply_patch_impl
from harness_agent.tools.tools_intelligence import lsp as _lsp_impl
from harness_agent.tools.tools_intelligence import tool_search as _tool_search_impl
from harness_agent.tools.tools_memory import memory_save as _memory_save_impl
from harness_agent.tools.tools_memory import memory_search as _memory_search_impl
from harness_agent.tools.tools_mode import BackgroundTaskManager, PlanModeState

logger = logging.getLogger(__name__)


def create_harness_tools(
    workspace_root: str,
    *,
    lsp_manager: Any | None = None,
) -> list[StructuredTool]:
    """创建所有 Harness 扩展工具的 BaseTool 实例列表。

    Args:
        workspace_root: 工作区根目录的绝对路径，用于路径安全校验。

    Returns:
        可直接传入 create_deep_agent(tools=...) 的工具列表。
    """
    # 共享状态实例（每次构图创建新的）
    plan_state = PlanModeState()
    task_manager = BackgroundTaskManager()

    tools: list[StructuredTool] = []

    # --- web_search ---
    async def _web_search(query: str, num_results: int = 5) -> str:
        """执行网络搜索，返回结构化搜索结果。

        Args:
            query: 搜索关键词。
            num_results: 返回结果数量，默认 5，最大 10。
        """
        result = await _web_search_impl(query, num_results)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        coroutine=_web_search,
        name="web_search",
        description="执行网络搜索，返回标题、URL 和摘要的结构化结果列表。",
    ))

    # --- web_fetch ---
    async def _web_fetch(url: str, format: str = "markdown") -> str:
        """获取指定 URL 的网页内容。

        Args:
            url: 目标 URL（必须为 http 或 https）。
            format: 输出格式，可选 text、markdown、html，默认 markdown。
        """
        result = await _web_fetch_impl(url, format)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        coroutine=_web_fetch,
        name="web_fetch",
        description="获取指定 URL 的网页内容，支持 text/markdown/html 格式输出。",
    ))

    # --- delete_file ---
    def _delete_file(file_path: str) -> str:
        """删除指定文件。

        Args:
            file_path: 要删除的文件路径（相对于工作区根目录，以 / 开头）。
        """
        result = _delete_file_impl(file_path, workspace_root)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_delete_file,
        name="delete_file",
        description="删除工作区内的指定文件。不可逆操作，需要用户审批。",
    ))

    # --- apply_patch ---
    def _apply_patch(patch: str) -> str:
        """应用 unified diff 格式的补丁到工作区文件。

        Args:
            patch: unified diff 格式的补丁内容。
        """
        result = _apply_patch_impl(patch, workspace_root)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_apply_patch,
        name="apply_patch",
        description="应用 unified diff 格式补丁，支持修改、创建和删除文件。",
    ))

    # --- lsp ---
    async def _lsp(action: str, file_path: str, line: int | None = None, column: int | None = None) -> str:
        """通过语言服务协议获取代码智能信息。

        Args:
            action: 操作类型，可选 definition、references、diagnostics、hover。
            file_path: 目标文件路径（相对于工作区）。
            line: 行号（从 1 开始），可选。
            column: 列号（从 1 开始），可选。
        """
        result = await _lsp_impl(
            action,
            file_path,
            line,
            column,
            workspace_root,
            manager=lsp_manager,
        )
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        coroutine=_lsp,
        name="lsp",
        description="查询代码智能信息：定义跳转(definition)、引用查找(references)、诊断(diagnostics)、悬停信息(hover)。",
    ))

    # --- tool_search ---
    def _tool_search(query: str) -> str:
        """搜索可用的 MCP 外部工具。

        Args:
            query: 搜索关键词，匹配工具名称或描述。
        """
        # 当前版本返回空列表，后续可从 MCP 注册表获取
        result = _tool_search_impl(query, available_tools=None)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_tool_search,
        name="tool_search",
        description="搜索可用的 MCP 外部工具，按名称或描述匹配。",
    ))

    # --- memory_save ---
    def _memory_save(key: str, content: str) -> str:
        """保存一条跨会话记忆。

        Args:
            key: 记忆标识符。
            content: 记忆内容。
        """
        result = _memory_save_impl(key, content)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_memory_save,
        name="memory_save",
        description="保存一条跨会话记忆，后续对话可通过 memory_search 检索。",
    ))

    # --- memory_search ---
    def _memory_search(query: str) -> str:
        """搜索已保存的跨会话记忆。

        Args:
            query: 搜索关键词。
        """
        result = _memory_search_impl(query)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_memory_search,
        name="memory_search",
        description="搜索已保存的跨会话记忆，按关键词匹配。",
    ))

    # --- enter_plan_mode ---
    def _enter_plan_mode() -> str:
        """进入计划模式，后续仅允许只读工具。"""
        result = plan_state.enter("default")
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_enter_plan_mode,
        name="enter_plan_mode",
        description="进入计划模式。进入后仅允许只读工具（读取、搜索、提问），不能修改文件或执行命令。",
    ))

    # --- exit_plan_mode ---
    def _exit_plan_mode() -> str:
        """退出计划模式，恢复到之前的审批模式。"""
        result = plan_state.exit()
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_exit_plan_mode,
        name="exit_plan_mode",
        description="退出计划模式，恢复到进入前的审批模式。",
    ))

    # --- task_output ---
    def _task_output(task_id: str) -> str:
        """获取后台任务的当前输出。

        Args:
            task_id: 后台任务 ID。
        """
        result = task_manager.get_output(task_id)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_task_output,
        name="task_output",
        description="获取指定后台任务的当前输出和状态。",
    ))

    # --- task_stop ---
    def _task_stop(task_id: str) -> str:
        """终止指定的后台任务。

        Args:
            task_id: 后台任务 ID。
        """
        result = task_manager.stop(task_id)
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_task_stop,
        name="task_stop",
        description="终止指定的后台任务。",
    ))

    # --- monitor ---
    def _monitor(command: str, interval: int = 5) -> str:
        """在后台持续执行命令并监控输出。

        Args:
            command: 要执行的 shell 命令。
            interval: 输出轮询间隔（秒），默认 5。
        """
        task_id = task_manager.register(command)
        result = {
            "success": True,
            "task_id": task_id,
            "command": command,
            "message": f"后台任务已启动，使用 task_output(task_id=\"{task_id}\") 获取输出",
        }
        return json.dumps(result, ensure_ascii=False)

    tools.append(StructuredTool.from_function(
        func=_monitor,
        name="monitor",
        description="在后台持续执行命令（如开发服务器），可通过 task_output 获取实时输出。",
    ))

    return tools
