"""za38 agent 内核 —— 参照 dcode create_cli_agent 裁剪版。

启用 deepagents 内置编码工具 + JS 解释器 + ask_user + memory + skills + HITL。
裁剪：沙箱、目标/评分、远程异步子 agent、web_search、fetch_url。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import MemorySaver

if TYPE_CHECKING:
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"


def _load_system_prompt() -> str:
    """从打包的 markdown 文件加载系统提示词。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def create_za38_agent(
    model: BaseChatModel | str,
    assistant_id: str = "za38",
    *,
    tools: Sequence[BaseTool | Any] | None = None,
    system_prompt: str | None = None,
    interactive: bool = True,
    auto_approve: bool = False,
    shell_allow_list: list[str] | None = None,
    enable_ask_user: bool = True,
    enable_memory: bool = True,
    enable_skills: bool = True,
    enable_interpreter: bool = True,
    checkpointer: Any = None,
    mcp_server_info: list | None = None,
    cwd: str | None = None,
    workdir: str | None = None,
) -> Any:
    """创建 za38 编码 agent。

    参照 dcode create_cli_agent，裁剪沙箱/评分/远程异步子 agent。

    Args:
        model: LLM 模型（ChatModel 实例或 "provider:model" 字符串）。
        tools: 额外工具（MCP 工具等）。核心工具由 middleware 自动注入。
        system_prompt: 自定义系统提示词。None 时用默认。
        interactive: True=交互模式（启用 ask_user），False=无头模式。
        auto_approve: True=跳过所有 HITL 审批。
        shell_allow_list: shell 命令白名单。
        enable_ask_user: 启用 ask_user 工具。
        enable_memory: 启用 AGENTS.md 记忆。
        enable_skills: 启用技能系统。
        enable_interpreter: 启用 JS 解释器（js_eval）。
        checkpointer: checkpoint saver。None 时用 MemorySaver。
        mcp_server_info: MCP 服务器信息列表。
        cwd: 工作目录。
        workdir: 工作目录别名（优先于 cwd）。

    Returns:
        编译后的 LangGraph agent（CompiledStateGraph）。
    """
    from za38_agent.providers.za38_gateway import resolve_model as _resolve

    if isinstance(model, str):
        raise ValueError(
            "String provider specs are not supported in v0.1. "
            "Load the OpenAI-compatible model from za38_agent.config instead."
        )
    resolved_model = _resolve(model)

    # LocalShellBackend 提供文件系统与 shell execute 工具。
    root = workdir or cwd or "."
    backend = LocalShellBackend(root_dir=root, virtual_mode=False)

    agent_middleware: list[Any] = []

    # 1. AskUserMiddleware（交互式提问，仅 interactive 模式）
    if interactive and enable_ask_user:
        from za38_agent.ask_user import AskUserMiddleware
        agent_middleware.append(AskUserMiddleware())

    # 2. MemoryMiddleware 需要明确后端和实际存在的记忆文件，避免首次启动因空路径失败。
    if enable_memory:
        from deepagents.middleware.memory import MemoryMiddleware

        memory_sources = [
            path
            for path in (
                Path.home() / ".za38" / "AGENTS.md",
                Path(root).resolve() / ".za38" / "AGENTS.md",
            )
            if path.is_file()
        ]
        if memory_sources:
            agent_middleware.append(
                MemoryMiddleware(backend=backend, sources=[str(path) for path in memory_sources])
            )

    # 3. 技能目录同样只传入已存在路径，避免空安装环境阻断主 Agent 启动。
    if enable_skills:
        from deepagents.middleware.skills import SkillsMiddleware

        skill_sources = [
            path
            for path in (
                Path(__file__).parent / "built_in_skills",
                Path.home() / ".za38" / "skills",
                Path(root).resolve() / ".za38" / "skills",
            )
            if path.is_dir()
        ]
        if skill_sources:
            agent_middleware.append(
                SkillsMiddleware(backend=backend, sources=[str(path) for path in skill_sources])
            )

    # 4. CodeInterpreterMiddleware（JS 解释器）
    if enable_interpreter:
        try:
            from langchain_core._api import suppress_langchain_beta_warning
            from langchain_quickjs import CodeInterpreterMiddleware

            with suppress_langchain_beta_warning():
                agent_middleware.append(
                    CodeInterpreterMiddleware(
                        tool_name="js_eval",
                        timeout=30,
                        memory_limit=128 * 1024 * 1024,
                        max_ptc_calls=50,
                        max_result_chars=50000,
                        ptc=None,  # safe 模式
                    )
                )
        except ImportError:
            logger.warning("langchain-quickjs not installed, js_eval disabled")

    # 5. ShellAllowListMiddleware（shell 白名单）
    if shell_allow_list:
        from za38_agent.shell_allow_list import ShellAllowListMiddleware
        agent_middleware.append(ShellAllowListMiddleware(shell_allow_list))

    # 6. HITL（interrupt_on）
    interrupt_on = _add_interrupt_on(auto_approve=auto_approve) if not auto_approve else None

    # 7. SummarizationToolMiddleware（compact_conversation 工具）
    from deepagents.middleware.summarization import create_summarization_tool_middleware
    agent_middleware.append(create_summarization_tool_middleware(resolved_model, backend))

    prompt = system_prompt or _load_system_prompt()
    all_tools = list(tools) if tools else []

    return create_deep_agent(
        model=resolved_model,
        tools=all_tools,
        middleware=agent_middleware,
        backend=backend,
        system_prompt=prompt,
        interrupt_on=interrupt_on,
        checkpointer=checkpointer or MemorySaver(),
    )


def _add_interrupt_on(*, auto_approve: bool = False) -> dict[str, Any]:
    """参照 dcode agent.py:_add_interrupt_on 裁剪版。

    裁剪：web_search, fetch_url, start_async_task, update_async_task, cancel_async_task
    保留：execute, write_file, edit_file, delete, task, compact_conversation
    """
    from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

    def _should_interrupt(_request: Any) -> bool:
        return not auto_approve

    # HumanInTheLoopMiddleware 只会注册声明了 allowed_decisions 的配置；
    # 省略该字段会悄然退化为自动批准，违背交互模式的安全边界。
    approval = InterruptOnConfig(allowed_decisions=["approve", "reject"], when=_should_interrupt)

    return {
        "execute": approval,
        "write_file": approval,
        "edit_file": approval,
        "delete": approval,
        "task": approval,
        "compact_conversation": approval,
    }
