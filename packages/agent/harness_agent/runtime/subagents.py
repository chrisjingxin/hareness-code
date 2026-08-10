"""子代理定义模型、Markdown 解析器和序列化器。

本模块负责从 .harness/agents/ 目录下的 Markdown 文件解析 AgentDefinition，
以及将 AgentDefinition 序列化回 Markdown 格式。支持 YAML frontmatter 元数据
和 Markdown 正文作为系统提示词。
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from harness_agent.tools.file_tool_catalog import FILE_TOOL_NAMES

_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
"""kebab-case 名称校验正则。"""

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<header>.*?)\r?\n---(?:\r?\n|\Z)(?P<body>.*)\Z", re.DOTALL)
"""YAML frontmatter 分割正则。"""

SUBAGENT_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "task", "enter_plan_mode", "exit_plan_mode", "ask_user",
})
"""子代理自动排除的工具集：防止嵌套委派、模式切换和交互中断。"""


class AgentSource(Enum):
    """Agent 定义的来源层级。"""

    BUILTIN = "builtin"
    PROJECT = "project"
    USER = "user"


class AgentColor(Enum):
    """Agent 在界面中的标识颜色。"""

    RED = "red"
    BLUE = "blue"
    GREEN = "green"
    YELLOW = "yellow"
    PURPLE = "purple"
    ORANGE = "orange"
    PINK = "pink"
    CYAN = "cyan"


@dataclass(slots=True)
class AgentDefinition:
    """子代理的完整定义，包含元数据和系统提示词。"""

    name: str
    description: str
    system_prompt: str
    source: AgentSource
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    model: str | None = None
    color: AgentColor | None = None
    max_turns: int | None = None
    background: bool = False
    file_path: Path | None = None


def _infer_source(path: Path) -> AgentSource:
    """根据文件路径推断 Agent 来源。

    包含 .harness/agents/ 段的路径视为项目或用户来源；
    位于用户主目录下为 USER，否则为 PROJECT。
    """
    parts = path.parts
    if ".harness" in parts and "agents" in parts:
        home = Path.home()
        try:
            path.relative_to(home)
            return AgentSource.USER
        except ValueError:
            return AgentSource.PROJECT
    return AgentSource.PROJECT


def parse_agent_markdown(path: Path) -> AgentDefinition:
    """从 Markdown 文件解析 AgentDefinition。

    文件格式为 YAML frontmatter（--- 分隔）加 Markdown 正文。
    frontmatter 提供元数据，正文作为 system_prompt。

    Args:
        path: Agent 定义文件路径。

    Returns:
        解析后的 AgentDefinition 实例。

    Raises:
        ValueError: 缺少必填字段或字段值不合法时抛出。
        FileNotFoundError: 文件不存在时抛出。
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"Agent 文件缺少有效的 YAML frontmatter: {path}")

    header_text = match.group("header")
    body = match.group("body").strip()

    try:
        meta: dict = yaml.safe_load(header_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Agent 文件 frontmatter YAML 解析失败: {path}: {exc}") from exc

    if not isinstance(meta, dict):
        raise ValueError(f"Agent 文件 frontmatter 必须是映射: {path}")

    # 必填字段校验
    name = meta.get("name")
    if not name:
        raise ValueError(f"Agent 定义缺少 name 字段: {path}")
    name = str(name)

    description = meta.get("description")
    if not description:
        raise ValueError(f"Agent 定义缺少 description 字段: {path}")
    description = str(description)

    # name 格式校验
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Agent name 必须是 kebab-case（^[a-z][a-z0-9]*(-[a-z0-9]+)*$），"
            f"实际值: {name!r}"
        )

    # color 校验
    color: AgentColor | None = None
    raw_color = meta.get("color")
    if raw_color is not None:
        raw_color = str(raw_color)
        try:
            color = AgentColor(raw_color)
        except ValueError:
            valid = ", ".join(c.value for c in AgentColor)
            raise ValueError(
                f"Agent color 必须是以下之一: {valid}，实际值: {raw_color!r}"
            ) from None

    # 可选字段
    tools: list[str] | None = None
    raw_tools = meta.get("tools")
    if raw_tools is not None:
        if not isinstance(raw_tools, list):
            raise ValueError(f"Agent tools 必须是列表: {path}")
        tools = [str(t) for t in raw_tools]

    disallowed_tools: list[str] | None = None
    raw_disallowed = meta.get("disallowedTools")
    if raw_disallowed is not None:
        if not isinstance(raw_disallowed, list):
            raise ValueError(f"Agent disallowedTools 必须是列表: {path}")
        disallowed_tools = [str(t) for t in raw_disallowed]

    model: str | None = None
    raw_model = meta.get("model")
    if raw_model is not None:
        model = str(raw_model)

    max_turns: int | None = None
    raw_max_turns = meta.get("maxTurns")
    if raw_max_turns is not None:
        max_turns = int(raw_max_turns)

    background: bool = bool(meta.get("background", False))

    source = _infer_source(path)

    return AgentDefinition(
        name=name,
        description=description,
        system_prompt=body,
        source=source,
        tools=tools,
        disallowed_tools=disallowed_tools,
        model=model,
        color=color,
        max_turns=max_turns,
        background=background,
        file_path=path,
    )


def serialize_agent_markdown(defn: AgentDefinition) -> str:
    """将 AgentDefinition 序列化为 Markdown 格式字符串。

    输出格式为 YAML frontmatter + Markdown 正文，可被 parse_agent_markdown 往返解析。
    frontmatter 只包含非 None 且非默认值的字段。

    Args:
        defn: 要序列化的 AgentDefinition。

    Returns:
        完整的 Markdown 文件内容。
    """
    meta: dict[str, object] = {}
    meta["name"] = defn.name
    meta["description"] = defn.description

    if defn.tools is not None:
        meta["tools"] = defn.tools
    if defn.disallowed_tools is not None:
        meta["disallowedTools"] = defn.disallowed_tools
    if defn.model is not None:
        meta["model"] = defn.model
    if defn.color is not None:
        meta["color"] = defn.color.value
    if defn.max_turns is not None:
        meta["maxTurns"] = defn.max_turns
    if defn.background:
        meta["background"] = defn.background

    header = yaml.dump(meta, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{header}---\n\n{defn.system_prompt}\n"


def discover_agents(workspace: Path | None, user_home: Path | None = None) -> list[AgentDefinition]:
    """扫描项目和用户目录中的 agent 定义文件。

    按 project(.harness/agents/) > user(~/.harness/agents/) 优先级扫描。
    目录不存在时静默跳过。解析失败的文件记录警告并跳过。
    """
    if user_home is None:
        user_home = Path.home()

    results: list[AgentDefinition] = []

    scan_dirs: list[tuple[Path, AgentSource]] = []
    if workspace is not None:
        scan_dirs.append((workspace / ".harness" / "agents", AgentSource.PROJECT))
    scan_dirs.append((user_home / ".harness" / "agents", AgentSource.USER))

    for agents_dir, source in scan_dirs:
        if not agents_dir.is_dir():
            continue
        for md_file in sorted(agents_dir.glob("*.md")):
            try:
                defn = parse_agent_markdown(md_file)
            except (ValueError, OSError) as exc:
                warnings.warn(f"跳过无法解析的 agent 文件 {md_file}: {exc}", stacklevel=2)
                continue
            # 覆盖解析推断的 source，使用扫描目录确定的来源
            defn.source = source
            results.append(defn)

    return results


_EXPLORE_PROMPT = """你是一个只读代码搜索专家。你的任务是快速定位代码、搜索文件内容并汇报发现。

规则：
- 你只能读取和搜索，不能修改任何文件
- 使用 glob 和 grep 进行高效搜索
- 使用 read_file 查看具体文件内容
- 最终回复必须包含完整的搜索结果和文件路径"""

_PLAN_PROMPT = """你是一个架构规划师。你的任务是分析代码结构、理解需求并制定实施计划。

规则：
- 你只能读取和搜索代码，不能修改文件
- 使用 write_todos 记录你的规划步骤
- 最终回复必须包含结构化的实施计划"""

BUILTIN_AGENTS: list[AgentDefinition] = [
    AgentDefinition(
        name="general-purpose",
        description="通用子代理，可执行多步骤复杂任务。拥有与主代理相同的工具集（不含 task）。适用于需要多轮工具调用的研究、搜索和实现任务。",
        system_prompt="",  # 使用 deepagents 的 DEFAULT_SUBAGENT_PROMPT
        source=AgentSource.BUILTIN,
        color=AgentColor.BLUE,
    ),
    AgentDefinition(
        name="explore",
        description="只读搜索专家。快速定位文件、搜索代码内容、查看目录结构。适用于需要广泛搜索但不需要修改的任务。",
        system_prompt=_EXPLORE_PROMPT,
        source=AgentSource.BUILTIN,
        tools=["ls", "read_file", "glob", "grep", "execute", "web_search", "web_fetch", "memory_search"],
        color=AgentColor.GREEN,
    ),
    AgentDefinition(
        name="plan",
        description="架构规划师。分析代码结构、理解需求、制定实施计划。适用于需要深入分析但不需要修改的规划任务。",
        system_prompt=_PLAN_PROMPT,
        source=AgentSource.BUILTIN,
        tools=["ls", "read_file", "glob", "grep", "write_todos", "memory_search"],
        color=AgentColor.PURPLE,
    ),
]


class AgentRegistry:
    """Agent 定义的运行时注册表，管理发现、注册和查询。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentDefinition] = {}

    def load(self, workspace: Path | None, user_home: Path | None = None) -> None:
        """加载内置 + 文件系统中的所有 agent 定义。

        优先级：project > user > builtin，同名覆盖。
        """
        # 先注册 builtin
        for agent in BUILTIN_AGENTS:
            self._agents[agent.name] = agent
        # 再加载文件系统（后加载覆盖先加载）
        discovered = discover_agents(workspace, user_home)
        # user 先注册，project 后注册（project 覆盖 user）
        for agent in sorted(discovered, key=lambda a: (a.source == AgentSource.PROJECT)):
            self._agents[agent.name] = agent

    def get(self, name: str) -> AgentDefinition | None:
        """按名称获取 agent 定义。"""
        return self._agents.get(name)

    def list(self) -> list[AgentDefinition]:
        """返回所有已注册 agent 的列表。"""
        return list(self._agents.values())

    def register(self, defn: AgentDefinition) -> None:
        """注册或覆盖一个 agent 定义（热加载）。"""
        self._agents[defn.name] = defn

    def unregister(self, name: str) -> bool:
        """移除一个 agent 定义。返回是否存在并被移除。"""
        return self._agents.pop(name, None) is not None


# 项目已有的全部工具名（来自 tool_risk.py 的 TOOL_KIND_MAP + ask_user）
ALL_TOOL_NAMES: frozenset[str] = frozenset({
    *FILE_TOOL_NAMES,
    "execute", "write_todos", "task",
    "web_search", "web_fetch",
    "tool_search", "memory_save", "memory_search",
    "enter_plan_mode", "exit_plan_mode", "task_output", "task_stop",
    "monitor", "ask_user",
})
"""当前项目注册的全部工具名称集合。"""


def filter_tools_for_agent(
    all_tool_names: frozenset[str] | set[str] | None = None,
    *,
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
) -> list[str]:
    """根据白名单/黑名单规则计算子代理可用工具集。

    过滤规则（按顺序）：
    1. 确定候选集：tools 指定时为白名单，否则为全量
    2. 移除 disallowed_tools 黑名单
    3. 移除 SUBAGENT_EXCLUDED_TOOLS（task/enter_plan_mode/exit_plan_mode/ask_user）

    Args:
        all_tool_names: 可用工具全集，默认使用 ALL_TOOL_NAMES。
        tools: 白名单列表，None 表示继承全部。
        disallowed_tools: 黑名单列表，None 表示不排除。

    Returns:
        排序后的可用工具名列表。
    """
    base = all_tool_names if all_tool_names is not None else ALL_TOOL_NAMES

    # 第一步：白名单或全量
    if tools is not None:
        candidates = set(tools) & set(base)
    else:
        candidates = set(base)

    # 第二步：黑名单
    if disallowed_tools is not None:
        candidates -= set(disallowed_tools)

    # 第三步：强制排除
    candidates -= SUBAGENT_EXCLUDED_TOOLS

    return sorted(candidates)
