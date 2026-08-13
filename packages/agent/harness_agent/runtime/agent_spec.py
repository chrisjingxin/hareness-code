"""角色级 ResolvedAgentSpec：把 AgentEngine 身份和构图输入收敛到同一快照。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_agent.runtime.agent_catalog import (
    AgentCatalog,
    AgentDefinition,
    DelegationPolicy,
    EffectiveExecutionPolicy,
    ExecutionPolicyDefinition,
    StringRule,
    intersect_execution_policies,
)
from harness_agent.runtime.agent_engine_profile import component_fingerprint
from harness_agent.config.config import ExecutionSettings, ModelCatalog, ModelSettings
from harness_agent.runtime.execution_binding import ResolvedExecutionBinding, SafeModelProfile
from harness_agent.extensions.mcp import McpConfigSnapshot
from harness_agent.threads.prompting import canonical_json, sha256_text, tool_schema_fingerprint
from harness_agent.extensions.plugin_skills import SkillRegistry
from harness_agent.policy.capability_policy import (
    BUILTIN_TOOL_NAMES,
    EffectiveCapabilityView,
    resolve_effective_capability_view,
)
from harness_agent.extensions.mcp import build_mcp_snapshot


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
BUILTIN_MAIN_DEFINITION_FINGERPRINT = sha256_text("builtin-agent:main:v1")
"""内置 main 的实现身份；它不是可由 Plugin 覆盖的 AgentDefinition。"""

READ_ONLY_REVIEWER_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "glob",
        "grep",
        "lsp",
    }
)
"""Compose Reviewer 的能力交集：只读文件/代码工具，不含写/Shell/网络/委派。"""

COMPOSE_PLANNING_STAGE_TOOLS: frozenset[str] = frozenset()
"""规划类 stage 只转换已提供的 ContextPack，不向模型暴露任何工具。"""

COMPOSE_PLANNING_STAGE_PROMPT_SUFFIX = """
<compose_planning_stage>
你是 Compose 工作流中的有界转换 stage，不是负责实现用户目标的主 Agent。
你只能根据本次输入生成要求的一个结果；不得调用任何工具，不得读取或修改工作区，
不得自行进入编码、测试或后续 stage。严格遵守本次输入指定的输出格式，输出结果后立即停止。
</compose_planning_stage>
""".strip()
"""规划类 stage 的系统级边界，防止弱模型把结构化任务误当成实现请求。"""

RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION = "run-context-snapshot-v1"
"""当前生产 RunContextSnapshot middleware 的 Profile 身份版本。"""


def restrict_spec_to_headless_stage(spec: ResolvedAgentSpec) -> ResolvedAgentSpec:
    """派生 Compose stage spec：复用主 Agent 全部能力，但关闭交互提问。

    Understand/Plan/Build 阶段只消费有界 ContextPack，产品决策走 workflow
    的 typed question；stage graph 不注册 ask_user，避免提问绕过流程。
    返回的 spec 拥有独立 profile key，由 Host 注册后经 AgentEnginePool
    构建独立引擎。
    """
    return ResolvedAgentSpec(
        project_fingerprint=spec.project_fingerprint,
        role="stage",
        agent_id="compose-stage",
        definition_fingerprint=sha256_text("builtin-agent:compose-stage:v1"),
        model_profile_id=spec.model_profile_id,
        model_settings=spec.model_settings,
        model_view=spec.model_view,
        effective_policy=spec.effective_policy,
        capability_view=spec.capability_view,
        tools=spec.tools,
        skill_registry=spec.skill_registry,
        mcp_snapshot=spec.mcp_snapshot,
        prompt=spec.prompt,
        execution=spec.execution,
        workspace=spec.workspace,
        interactive=False,
        tool_view_fingerprint=spec.tool_view_fingerprint,
        skill_view_fingerprint=spec.skill_view_fingerprint,
        middleware_fingerprint=sha256_text(
            str(
                (
                    RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION,
                    "context-window-v1",
                    "workspace-boundary-v1",
                    "stage-headless",
                    "memory-on",
                    "skills-on",
                )
            )
        ),
        prompt_template_fingerprint=spec.prompt_template_fingerprint,
        sandbox_config_fingerprint=spec.sandbox_config_fingerprint,
        pinned=spec.pinned,
        enable_memory=spec.enable_memory,
        enable_skills=spec.enable_skills,
        enable_ask_user=False,
    )


def restrict_spec_to_read_only(spec: ResolvedAgentSpec) -> ResolvedAgentSpec:
    """派生只读 Reviewer spec：同一模型/Policy，但能力交集为只读工具。

    Reviewer 复用主 Agent 的模型、有效 Policy 与 workspace，但 graph 只暴露
    只读工具。工具集合必须取主 spec 的可见内置工具交集：内置工具在
    create_harness_agent 内部注册，spec.tools 只携带 MCP 工具，因此这里
    以 capability_view.tool_names 为准。filesystem_write/shell/mcp/skills
    全部从能力视图剔除，作者 execution 不得兼任 Reviewer。返回的 spec
    拥有独立 profile key，由 Host 注册后经 AgentEnginePool 构建独立引擎。
    """
    return _restrict_spec_to_read_only_role(
        spec,
        role="reviewer",
        agent_id="compose-reviewer",
        identity="reviewer-readonly",
        allowed_tools=READ_ONLY_REVIEWER_TOOLS,
    )


def restrict_spec_to_read_only_stage(spec: ResolvedAgentSpec) -> ResolvedAgentSpec:
    """派生规划 stage spec，确保只完成无工具的有界结果转换。"""
    return _restrict_spec_to_read_only_role(
        spec,
        role="stage",
        agent_id="compose-planning",
        identity="planning-readonly-bounded",
        allowed_tools=COMPOSE_PLANNING_STAGE_TOOLS,
        prompt_suffix=COMPOSE_PLANNING_STAGE_PROMPT_SUFFIX,
    )


def _restrict_spec_to_read_only_role(
    spec: ResolvedAgentSpec,
    *,
    role: str,
    agent_id: str,
    identity: str,
    allowed_tools: frozenset[str],
    prompt_suffix: str = "",
) -> ResolvedAgentSpec:
    """构造指定 Compose 角色的只读能力交集。"""
    from harness_agent.policy.capability_policy import EffectiveCapabilityView

    visible = set(spec.capability_view.tool_names)
    names = tuple(sorted(allowed_tools & visible))
    tools = tuple(tool for tool in spec.tools if tool.name in names)
    view = EffectiveCapabilityView(
        tool_names=names,
        mcp_tool_names=(),
        skill_ids=(),
        filesystem_read=(spec.capability_view.filesystem_read if names else None),
        filesystem_write=None,
        shell_commands=None,
        policy_fingerprint=spec.effective_policy.fingerprint,
    )
    return ResolvedAgentSpec(
        project_fingerprint=spec.project_fingerprint,
        role=role,
        agent_id=agent_id,
        definition_fingerprint=sha256_text(f"builtin-agent:{agent_id}:v1"),
        model_profile_id=spec.model_profile_id,
        model_settings=spec.model_settings,
        model_view=spec.model_view,
        effective_policy=spec.effective_policy,
        capability_view=view,
        tools=tools,
        skill_registry=spec.skill_registry,
        mcp_snapshot=spec.mcp_snapshot,
        # 规划 stage 不能继承主 Agent 的“主动解决用户任务”提示，否则弱模型会
        # 把结构化转换输入误当成实现请求；Reviewer 无 suffix 时仍复用主提示。
        prompt=prompt_suffix or spec.prompt,
        execution=spec.execution,
        workspace=spec.workspace,
        interactive=False,
        tool_view_fingerprint=view.fingerprint,
        skill_view_fingerprint=sha256_text(
            canonical_json({"view": view.fingerprint, "skills": "readonly-none"})
        ),
        middleware_fingerprint=sha256_text(
            str(
                (
                    RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION,
                    "context-window-v1",
                    "workspace-boundary-v1",
                    identity,
                    "memory-off",
                    "skills-off",
                )
            )
        ),
        prompt_template_fingerprint=spec.prompt_template_fingerprint,
        sandbox_config_fingerprint=spec.sandbox_config_fingerprint,
        pinned=spec.pinned,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )


def skill_catalog_fingerprint(
    skill_registry: SkillRegistry,
    *,
    view_fingerprint: str | None = None,
) -> str:
    """从同一 immutable Registry 计算 AgentEngine Profile 的 Skill 身份。"""
    return component_fingerprint(
        {
            "view": view_fingerprint or sha256_text(f"skills:{skill_registry.snapshot_id}"),
            "snapshot_id": skill_registry.snapshot_id,
        }
    )


@dataclass(frozen=True, slots=True)
class ResolvedAgentSpec:
    """一次角色解析得到的只读构图输入和静态能力视图。"""

    project_fingerprint: str
    role: str
    agent_id: str
    definition_fingerprint: str
    model_profile_id: str
    model_settings: ModelSettings
    model_view: SafeModelProfile
    effective_policy: EffectiveExecutionPolicy
    capability_view: EffectiveCapabilityView
    tools: tuple[Any, ...]
    skill_registry: SkillRegistry
    mcp_snapshot: McpConfigSnapshot
    prompt: str
    execution: ExecutionSettings
    workspace: Path
    interactive: bool
    tool_view_fingerprint: str
    skill_view_fingerprint: str
    middleware_fingerprint: str
    prompt_template_fingerprint: str
    sandbox_config_fingerprint: str
    pinned: bool = False
    enable_memory: bool = True
    enable_skills: bool = True
    enable_ask_user: bool = True

    def __post_init__(self) -> None:
        """冻结工具快照并验证会进入 Profile 的身份字段。"""
        for name, value in (("role", self.role), ("agent_id", self.agent_id)):
            if not _IDENTIFIER_RE.fullmatch(value):
                raise ValueError(f"RESOLVED_AGENT_{name.upper()}_INVALID")
        for name, value in (
            ("project_fingerprint", self.project_fingerprint),
            ("definition_fingerprint", self.definition_fingerprint),
            ("tool_view_fingerprint", self.tool_view_fingerprint),
            ("skill_view_fingerprint", self.skill_view_fingerprint),
            ("middleware_fingerprint", self.middleware_fingerprint),
            ("prompt_template_fingerprint", self.prompt_template_fingerprint),
            ("sandbox_config_fingerprint", self.sandbox_config_fingerprint),
        ):
            if not _HASH_RE.fullmatch(value):
                raise ValueError(f"RESOLVED_AGENT_{name.upper()}_INVALID")
        if not self.prompt:
            raise ValueError("RESOLVED_AGENT_PROMPT_INVALID")
        if self.capability_view.policy_fingerprint != self.effective_policy.fingerprint:
            raise ValueError("RESOLVED_AGENT_CAPABILITY_POLICY_MISMATCH")
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "workspace", Path(self.workspace))

    @property
    def runtime_profile(self) -> Any:
        """从本 spec 计算唯一 AgentEngineProfile，禁止调用方另行拼装。"""
        from harness_agent.runtime.agent_engine_profile import (
            AgentEngineProfile,
            ModelRoleBinding,
            model_settings_fingerprint,
        )
        from harness_agent.runtime.agent import default_tool_catalog_fingerprint

        return AgentEngineProfile(
            project_fingerprint=self.project_fingerprint,
            topology_id="agent",
            topology_version=1,
            model_roles=(
                ModelRoleBinding(
                    role=self.role,
                    model_config_fingerprint=model_settings_fingerprint(
                        profile_name=self.model_profile_id,
                        model=self.model_settings,
                    ),
                ),
            ),
            tool_catalog_fingerprint=component_fingerprint(
                {
                    "view": self.tool_view_fingerprint,
                    "mcp_tool_schema": tool_schema_fingerprint(self.tools),
                    "builtin_tool_catalog": default_tool_catalog_fingerprint(),
                }
            ),
            skill_catalog_fingerprint=skill_catalog_fingerprint(
                self.skill_registry,
                view_fingerprint=self.skill_view_fingerprint,
            ),
            mcp_config_fingerprint=self.mcp_snapshot.digest,
            sandbox_config_fingerprint=component_fingerprint(
                {
                    "descriptor": self.sandbox_config_fingerprint,
                    "execution": _execution_identity(self.execution),
                    "workspace": sha256_text(str(self.workspace)),
                }
            ),
            policy_fingerprint=self.effective_policy.fingerprint,
            middleware_fingerprint=component_fingerprint(
                {
                    "base": self.middleware_fingerprint,
                    "interactive": self.interactive,
                    "enable_memory": self.enable_memory,
                    "enable_skills": self.enable_skills,
                    "enable_ask_user": self.enable_ask_user,
                }
            ),
            prompt_template_fingerprint=component_fingerprint(
                {
                    "declared": self.prompt_template_fingerprint,
                    "content": sha256_text(self.prompt),
                }
            ),
            agent_id=self.agent_id,
            definition_fingerprint=self.definition_fingerprint,
        )


def _execution_identity(settings: ExecutionSettings) -> dict[str, object]:
    """返回不含秘密的执行后端身份，供 spec 重新计算 Profile。"""
    return {
        "mode": settings.mode,
        "provider": settings.remote.provider if settings.remote else None,
        "working_directory": settings.remote.working_directory if settings.remote else None,
        "params": dict(settings.remote.params) if settings.remote else {},
        # AUTO 模式分类器 profile 变化会改变中间件构成，必须参与引擎指纹。
        "approval_classifier": settings.approval_classifier,
    }


def resolve_builtin_main_agent_spec(
    *,
    project_fingerprint: str,
    workspace: Path,
    binding: ResolvedExecutionBinding,
    execution: ExecutionSettings,
    skill_registry: SkillRegistry,
    mcp_snapshot: McpConfigSnapshot,
    mcp_tools: tuple[Any, ...],
    interactive: bool,
    pinned: bool,
    delegation_agent_ids: tuple[str, ...] = (),
) -> ResolvedAgentSpec:
    """解析当前内置 main；不读取 Plugin catalog，也不携带 Thread/Run 状态。"""
    from harness_agent.runtime.agent import (
        default_prompt_template_fingerprint,
        default_system_prompt,
    )

    policy = EffectiveExecutionPolicy(
        policy_ids=("builtin-main",),
        tools=None,
        mcp_tools=None,
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation=execution.mode,
        approval_mode=str(execution.approval_mode),
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose", *tuple(sorted(set(delegation_agent_ids)))),
            max_depth=1,
            max_parallelism=4,
        ),
    )
    mcp_tool_names = tuple(
        str(getattr(tool, "name", ""))
        if not isinstance(tool, dict)
        else str(tool.get("name", ""))
        for tool in mcp_tools
    )
    capability_view = resolve_effective_capability_view(
        policy,
        available_tools=(*BUILTIN_TOOL_NAMES, *mcp_tool_names),
        mcp_tool_names=mcp_tool_names,
        available_skill_ids=(record.skill_id for record in skill_registry.records),
    )
    tools = tuple(
        tool
        for tool, name in zip(mcp_tools, mcp_tool_names, strict=True)
        if name in capability_view.mcp_tool_names
    )
    effective_skills = skill_registry.restricted(capability_view.skill_ids)
    return ResolvedAgentSpec(
        project_fingerprint=project_fingerprint,
        role="primary",
        agent_id="main",
        definition_fingerprint=BUILTIN_MAIN_DEFINITION_FINGERPRINT,
        model_profile_id=binding.primary_profile.profile_id,
        model_settings=binding.primary_profile.settings,
        model_view=binding.safe_primary,
        effective_policy=policy,
        capability_view=capability_view,
        tools=tools,
        skill_registry=effective_skills,
        mcp_snapshot=mcp_snapshot,
        prompt=default_system_prompt(),
        execution=execution,
        workspace=workspace,
        interactive=interactive,
        tool_view_fingerprint=capability_view.fingerprint,
        skill_view_fingerprint=sha256_text(
            canonical_json(
                {
                    "view": capability_view.fingerprint,
                    "skills": effective_skills.snapshot_id,
                }
            )
        ),
        middleware_fingerprint=sha256_text(
            str(
                (
                    RUN_CONTEXT_SNAPSHOT_MIDDLEWARE_VERSION,
                    "context-window-v1",
                    "workspace-boundary-v1",
                    "interactive-question" if interactive else "headless",
                    "memory-on",
                    "skills-on",
                )
            )
        ),
        prompt_template_fingerprint=default_prompt_template_fingerprint(),
        sandbox_config_fingerprint=sha256_text(
            canonical_json(
                {
                    "mode": execution.mode,
                    "provider": execution.remote.provider if execution.remote else None,
                    "working_directory": (
                        execution.remote.working_directory if execution.remote else None
                    ),
                    "params": dict(execution.remote.params) if execution.remote else {},
                    "approval_classifier": execution.approval_classifier,
                }
            )
        ),
        pinned=pinned,
        enable_ask_user=interactive,
    )


def resolve_plugin_agent_spec(
    *,
    definition: AgentDefinition,
    catalog: AgentCatalog,
    parent_policy: EffectiveExecutionPolicy,
    model_catalog: ModelCatalog,
    project_fingerprint: str,
    workspace: Path,
    execution: ExecutionSettings,
    skill_registry: SkillRegistry,
    mcp_snapshot: McpConfigSnapshot,
    mcp_tools: tuple[Any, ...],
    interactive: bool,
    inherited_model_profile_id: str,
) -> ResolvedAgentSpec:
    """把 Plugin Agent 请求解析为唯一、只收紧的构图快照。"""
    model_profile = model_catalog.require_profile(
        inherited_model_profile_id
        if definition.model_profile_id == "inherit"
        else definition.model_profile_id
    )
    effective = catalog.effective_policy(
        definition.agent_id,
        envelope=parent_policy,
    )
    requested_skill_ids = _resolve_requested_skill_ids(
        definition.requested_skills,
        skill_registry,
        source=definition.source,
    )
    effective = intersect_execution_policies(
        effective,
        ExecutionPolicyDefinition(
            policy_id=f"{definition.agent_id}-component-request",
            source=definition.source,
            skills=StringRule(allow=requested_skill_ids),
            mcp_tools=StringRule(
                allow=_resolve_requested_mcp_tool_names(
                    definition,
                    mcp_snapshot,
                    mcp_tools,
                )
            ),
        ),
    )
    mcp_tool_names = tuple(
        str(getattr(tool, "name", ""))
        if not isinstance(tool, dict)
        else str(tool.get("name", ""))
        for tool in mcp_tools
    )
    capability_view = resolve_effective_capability_view(
        effective,
        available_tools=(*BUILTIN_TOOL_NAMES, *mcp_tool_names),
        mcp_tool_names=mcp_tool_names,
        available_skill_ids=(record.skill_id for record in skill_registry.records),
    )
    tools = tuple(
        tool
        for tool, name in zip(mcp_tools, mcp_tool_names, strict=True)
        if name in capability_view.mcp_tool_names
    )
    selected_servers = tuple(
        server
        for server in mcp_snapshot.servers
        if any(
            name.startswith(f"{server.name}_")
            for name in capability_view.mcp_tool_names
        )
    )
    effective_mcp_snapshot = build_mcp_snapshot(
        selected_servers,
        revision=mcp_snapshot.revision,
    )
    effective_skills = skill_registry.restricted(capability_view.skill_ids)
    prompt_fingerprint = sha256_text(definition.prompt)
    return ResolvedAgentSpec(
        project_fingerprint=project_fingerprint,
        role="delegate",
        agent_id=definition.agent_id,
        definition_fingerprint=definition.fingerprint,
        model_profile_id=model_profile.profile_id,
        model_settings=model_profile.settings,
        model_view=SafeModelProfile.from_profile(model_profile),
        effective_policy=effective,
        capability_view=capability_view,
        tools=tools,
        skill_registry=effective_skills,
        mcp_snapshot=effective_mcp_snapshot,
        prompt=definition.prompt,
        execution=execution,
        workspace=workspace,
        interactive=interactive,
        tool_view_fingerprint=capability_view.fingerprint,
        skill_view_fingerprint=sha256_text(
            canonical_json(
                {
                    "view": capability_view.fingerprint,
                    "skills": effective_skills.snapshot_id,
                }
            )
        ),
        middleware_fingerprint=sha256_text(
            canonical_json(
                {
                    "kind": "plugin-agent-v1",
                    "source": definition.source,
                    "max_turns": definition.max_turns,
                }
            )
        ),
        prompt_template_fingerprint=prompt_fingerprint,
        sandbox_config_fingerprint=sha256_text(
            canonical_json(
                {
                    "mode": execution.mode,
                    "provider": execution.remote.provider if execution.remote else None,
                    "working_directory": (
                        execution.remote.working_directory if execution.remote else None
                    ),
                    "params": dict(execution.remote.params) if execution.remote else {},
                }
            )
        ),
        pinned=False,
        enable_ask_user=False,
        enable_memory=False,
    )


def _resolve_requested_skill_ids(
    requested: tuple[str, ...],
    registry: SkillRegistry,
    *,
    source: str,
) -> tuple[str, ...]:
    """将 Claude/portable 的包内短名解析为当前 snapshot 的唯一 canonical ID。"""
    plugin_name = source.removeprefix("plugin:")
    resolved: list[str] = []
    for name in requested:
        exact = [record.skill_id for record in registry.records if record.skill_id == name]
        suffix = [
            record.skill_id
            for record in registry.records
            if record.skill_id.endswith(f"/{plugin_name}/{name}")
        ]
        matches = exact or suffix
        if len(matches) == 1:
            resolved.append(matches[0])
    return tuple(sorted(set(resolved)))


def _resolve_requested_mcp_tool_names(
    definition: AgentDefinition,
    snapshot: McpConfigSnapshot,
    tools: tuple[Any, ...],
) -> tuple[str, ...]:
    """把 Agent 的包内 MCP Server 短名解析为该 Server 的实际 Tool 名。"""
    plugin_name = definition.source.removeprefix("plugin:")
    requested = {
        re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")
        for name in definition.requested_mcp_servers
    }
    server_prefixes = tuple(
        f"{server.name}_"
        for server in snapshot.servers
        if server.source.startswith("plugin:")
        and server.source.endswith(f"/{plugin_name}")
        and any(server.name.endswith(f"__{name}") for name in requested)
    )
    names = (
        str(getattr(tool, "name", ""))
        if not isinstance(tool, dict)
        else str(tool.get("name", ""))
        for tool in tools
    )
    return tuple(
        sorted(
            name
            for name in names
            if name and any(name.startswith(prefix) for prefix in server_prefixes)
        )
    )
