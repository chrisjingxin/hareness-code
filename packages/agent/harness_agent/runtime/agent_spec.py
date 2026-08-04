"""角色级 ResolvedAgentSpec：把 AgentEngine 身份和构图输入收敛到同一快照。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness_agent.runtime.agent_catalog import EffectiveExecutionPolicy
from harness_agent.config.config import ExecutionSettings, ModelSettings
from harness_agent.runtime.execution_binding import ResolvedExecutionBinding
from harness_agent.extensions.mcp import McpConfigSnapshot
from harness_agent.threads.prompting import canonical_json, sha256_text, tool_schema_fingerprint
from harness_agent.extensions.skills import SkillRegistry


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
BUILTIN_MAIN_DEFINITION_FINGERPRINT = sha256_text("builtin-agent:main:v1")
"""内置 main 的实现身份；它不是可由 Plugin 覆盖的 AgentDefinition。"""


@dataclass(frozen=True, slots=True)
class ResolvedAgentSpec:
    """一次角色解析得到的只读构图输入和静态能力视图。"""

    project_fingerprint: str
    role: str
    agent_id: str
    definition_fingerprint: str
    model_profile_id: str
    model_settings: ModelSettings
    effective_policy: EffectiveExecutionPolicy
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
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "workspace", Path(self.workspace))

    @property
    def runtime_profile(self) -> Any:
        """从本 spec 计算唯一 AgentEngineProfile，禁止调用方另行拼装。"""
        from harness_agent.runtime.agent_engine_profile import (
            AgentEngineProfile,
            ModelRoleBinding,
            component_fingerprint,
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
            skill_catalog_fingerprint=component_fingerprint(
                {
                    "view": self.skill_view_fingerprint,
                    "snapshot_id": self.skill_registry.snapshot_id,
                }
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
) -> ResolvedAgentSpec:
    """解析当前内置 main；不读取 Plugin catalog，也不携带 Thread/Run 状态。"""
    from harness_agent.runtime.agent import (
        default_prompt_template_fingerprint,
        default_system_prompt,
        default_tool_catalog_fingerprint,
    )

    policy = EffectiveExecutionPolicy(
        policy_ids=("builtin-main",),
        tools=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation=execution.mode,
        approval_mode=str(execution.approval_mode),
        delegation=None,
    )
    tools = tuple(mcp_tools)
    return ResolvedAgentSpec(
        project_fingerprint=project_fingerprint,
        role="primary",
        agent_id="main",
        definition_fingerprint=BUILTIN_MAIN_DEFINITION_FINGERPRINT,
        model_profile_id=binding.primary_profile.profile_id,
        model_settings=binding.primary_profile.settings,
        effective_policy=policy,
        tools=tools,
        skill_registry=skill_registry,
        mcp_snapshot=mcp_snapshot,
        prompt=default_system_prompt(),
        execution=execution,
        workspace=workspace,
        interactive=interactive,
        tool_view_fingerprint=sha256_text(
            canonical_json(
                {
                    "builtin": default_tool_catalog_fingerprint(),
                    "mcp": tool_schema_fingerprint(tools),
                }
            )
        ),
        skill_view_fingerprint=sha256_text(f"skills:{skill_registry.snapshot_id}"),
        middleware_fingerprint=sha256_text(
            str(
                (
                    "prompt-epoch-v1",
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
