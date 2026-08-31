"""内置子代理定义、能力求交与可派发摘要。

本模块是 Dispatch Catalog 的 builtin 数据源。Plugin 不得覆盖这些 ID；
`explore` 的只读边界在这里计算一次，同时约束 schema 与执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness_agent.threads.prompting import canonical_json, sha256_text

if TYPE_CHECKING:
    from harness_agent.policy.capability_policy import EffectiveCapabilityView

FORCED_EXCLUSIONS: frozenset[str] = frozenset(
    {
        "task",
        "ask_user",
        "enter_plan_mode",
        "exit_plan_mode",
        "memory_save",
    }
)
"""任何内置 child 都不可拥有的工具。"""

EXPLORE_TOOL_ALLOWLIST: frozenset[str] = frozenset(
    {"glob", "grep", "ls", "lsp", "read_file"}
)
"""explore 角色上限；还要和父能力、本机实现求交。"""

_MIDDLEWARE_INJECTED: frozenset[str] = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "glob",
        "grep",
        "execute",
        "write_todos",
    }
)
"""DeepAgents 中间件注入的工具，不一定出现在 create_agent(tools=) 参数里。"""

GENERAL_PURPOSE_DESCRIPTION = (
    "通用多步骤子代理。继承父 Agent 的工作区能力，但不包含委派、向用户提问、"
    "计划模式切换工具和跨会话记忆写入。适合研究、搜索和实现；当文件或关键词"
    "搜索前几次没有把握时，优先交给它。它看不到父对话历史，最终只回一份完整结果。"
)

EXPLORE_DESCRIPTION = (
    "只读代码定位与结构调查子代理。只能使用 ls、read_file、glob、grep、lsp。"
    "适合按文件名、符号或内容搜索，以及回答代码在哪、结构如何连接。"
    "需要改文件、执行命令、访问网络或向用户提问时不要用它。"
)

GENERAL_PURPOSE_PROMPT = """你是主 Agent 派出的通用子代理，只完成这次交办的任务。

调用方只能看到你的最终回复，看不到中间推理和工具输出。最终回复必须让父 Agent 不必再翻你的过程就能继续工作，按需包含：结论、关键证据、改过的文件、做过的校验、未完成项或阻塞。不要复述全部工具输出。

规则：
- 不要扩大范围，不要做没被要求的重构或文档。
- 先查看相关代码和现状再修改。优先编辑已有文件；非必要不新建文件；未经明确要求不要写 README 或其他 *.md。
- 你继承父 Run 已冻结的工作区、AGENTS.md、Skill 索引和审批规则。你不能向用户提问；缺少证据就在最终回复里写明不确定和缺口。
- 不要尝试委派其他子代理、切换计划模式或写入跨会话记忆。
"""

EXPLORE_PROMPT = """你是只读代码搜索子代理。用尽可能少的上下文，给父 Agent 一份可交接的调查结果。

你只能使用 ls、read_file、glob、grep、lsp。没有写文件、命令执行、网络访问、MCP、Skill 目录或向用户提问的能力。只搜当前工作区和 AGENTS.md，不要按主 Agent 的工作流 Skill 行事。

做法：
- 先用 glob、grep 或 lsp 定位，再按需 read_file。不要整文件通读，除非文件很小。
- 尽量并行调用工具。
- 第一次搜索落空时，必须再换一种模式、路径或符号查询，才能断言目标不存在。
- 按任务需要自行判断深度：点查关键文件、跟进口与核心实现，或沿依赖把相关符号查清。

最终回复必须包含：
- 简短结论
- 相关文件的工作区虚拟路径和关键符号
- 这些部分如何连接
- 仍不确定的缺口

不要复述读过的整段代码，只引用对结论必要的片段。不要尝试修改仓库。
"""


@dataclass(frozen=True, slots=True)
class BuiltinAgentRecord:
    """内置可派发角色；不是伪造的 Plugin AgentDefinition。"""

    agent_id: str
    description: str
    purpose: str
    prompt: str
    tools: tuple[str, ...]
    execution_policy_id: str

    @property
    def fingerprint(self) -> str:
        """提示词与工具上限参与 catalog 快照。"""
        return sha256_text(
            canonical_json(
                {
                    "id": self.agent_id,
                    "description": self.description,
                    "purpose": self.purpose,
                    "prompt": self.prompt,
                    "tools": list(self.tools),
                    "policy": self.execution_policy_id,
                }
            )
        )

    def summary(self) -> dict[str, object]:
        """返回协议 agentSummary，不含 Prompt 正文。"""
        return {
            "id": self.agent_id,
            "description": self.description,
            "purpose": self.purpose,
            "model_profile_id": "inherit",
            "execution_policy_id": self.execution_policy_id,
            "requested_skills": [],
            "requested_mcp_servers": [],
            "max_turns": None,
            "color": None,
            "approval_mode": None,
            "permission_mode": None,
            "source": "builtin",
            "fingerprint": self.fingerprint,
            "kind": "builtin",
            "tools": list(self.tools),
        }


BUILTIN_AGENTS: tuple[BuiltinAgentRecord, ...] = (
    BuiltinAgentRecord(
        agent_id="general-purpose",
        description=GENERAL_PURPOSE_DESCRIPTION,
        purpose="通用多步骤任务，继承父能力并排除委派与提问。",
        prompt=GENERAL_PURPOSE_PROMPT,
        tools=(),
        execution_policy_id="builtin-general-purpose",
    ),
    BuiltinAgentRecord(
        agent_id="explore",
        description=EXPLORE_DESCRIPTION,
        purpose="只读代码定位与结构调查。",
        prompt=EXPLORE_PROMPT,
        tools=tuple(sorted(EXPLORE_TOOL_ALLOWLIST)),
        execution_policy_id="builtin-explore",
    ),
)

BUILTIN_AGENTS_BY_ID: dict[str, BuiltinAgentRecord] = {
    record.agent_id: record for record in BUILTIN_AGENTS
}


def resolve_child_approval_mode(parent: str, role: str) -> str:
    """计算内置 child 有效审批模式：不得比父更松，仅 GP+default 升到 auto-edit。"""
    if role == "general-purpose" and parent == "default":
        return "auto-edit"
    return parent


def explore_view_is_readonly(view: object) -> bool:
    """explore 能力视图满足 7.2 只读约束时才允许 task 自动通过。"""
    names = set(getattr(view, "tool_names", ()) or ())
    if not names or not names <= EXPLORE_TOOL_ALLOWLIST:
        return False
    if getattr(view, "mcp_tool_names", ()):
        return False
    if getattr(view, "filesystem_write", None) is not None:
        return False
    if getattr(view, "shell_commands", None) is not None:
        return False
    return True


def resolve_builtin_child_view(
    *,
    agent_id: str,
    parent: EffectiveCapabilityView,
    available_tool_names: frozenset[str],
) -> EffectiveCapabilityView:
    """角色上限 ∩ 父视图 ∩ 本机实现 − Forced exclusions。"""
    from harness_agent.policy.capability_policy import EffectiveCapabilityView

    realized = set(available_tool_names) | _MIDDLEWARE_INJECTED | set(parent.tool_names)
    parent_tools = set(parent.tool_names) & realized
    if agent_id == "explore":
        names = tuple(sorted(EXPLORE_TOOL_ALLOWLIST & parent_tools))
        return EffectiveCapabilityView(
            tool_names=names,
            mcp_tool_names=(),
            skill_ids=(),
            filesystem_read=parent.filesystem_read,
            filesystem_write=None,
            shell_commands=None,
            policy_fingerprint=parent.policy_fingerprint,
        )
    if agent_id != "general-purpose":
        raise ValueError(f"UNKNOWN_BUILTIN_AGENT: {agent_id}")
    names = tuple(sorted(parent_tools - FORCED_EXCLUSIONS))
    mcp_names = tuple(
        name for name in parent.mcp_tool_names if name in names
    )
    return EffectiveCapabilityView(
        tool_names=names,
        mcp_tool_names=mcp_names,
        skill_ids=parent.skill_ids,
        filesystem_read=parent.filesystem_read,
        filesystem_write=parent.filesystem_write,
        shell_commands=parent.shell_commands,
        policy_fingerprint=parent.policy_fingerprint,
    )
