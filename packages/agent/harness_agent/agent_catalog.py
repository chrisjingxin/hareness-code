"""Agent 与 ExecutionPolicy 的受限静态目录。

本模块只建立由 Plugin loader 显式传入的启动期只读快照，不构建 Runtime、不注册子 Agent，
也不读取用户或项目目录。Python 内置主 Agent 不使用本目录；未来动态 delegation 只能从
已校验的 catalog 取 Plugin 定义，不能将仓库文件、Prompt 或权限配置直接传入执行层。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from harness_agent.config import ConfigError, ModelCatalog
from harness_agent.prompting import canonical_json


MAX_CATALOG_FILE_BYTES = 64 * 1024
"""单个 Agent、Policy、Schema 或指令资产的最大字节数。"""

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TOOL_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ISOLATION_MODES = frozenset({"local", "remote", "worktree", "container"})
_APPROVAL_MODES = ("never", "on-risk", "always")


class AgentCatalogError(ValueError):
    """Agent 或 Policy 定义无法安全进入 catalog 时抛出。"""


@dataclass(frozen=True, slots=True)
class PluginAgentSource:
    """已由 Plugin 层完成信任校验后交给 catalog 的只读资产根目录。"""

    plugin_id: str
    root: Path

    def __post_init__(self) -> None:
        """限制 Plugin 身份，目录本身仍会在读取时拒绝 symlink。"""
        if not _IDENTIFIER_RE.fullmatch(self.plugin_id):
            raise AgentCatalogError("plugin_id must be kebab-case")


@dataclass(frozen=True, slots=True)
class StringRule:
    """工具、命令或 Agent ID 的 allow/deny 规则；``None`` 表示未施加 allow 上限。"""

    allow: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()

    def record(self) -> dict[str, object]:
        """返回可参与指纹的稳定结构。"""
        return {"allow": list(self.allow) if self.allow is not None else None, "deny": list(self.deny)}


@dataclass(frozen=True, slots=True)
class ShellPolicy:
    """Shell 是否可用及其命令规则。"""

    enabled: bool
    commands: StringRule = StringRule()

    def record(self) -> dict[str, object]:
        """返回稳定的脱敏结构。"""
        return {"enabled": self.enabled, "commands": self.commands.record()}


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """网络是否可用及允许的 host 集合。"""

    enabled: bool
    allowed_hosts: tuple[str, ...] | None = None

    def record(self) -> dict[str, object]:
        """返回稳定的脱敏结构。"""
        return {
            "enabled": self.enabled,
            "allowed_hosts": list(self.allowed_hosts) if self.allowed_hosts is not None else None,
        }


@dataclass(frozen=True, slots=True)
class DelegationPolicy:
    """动态派发的静态上限。"""

    enabled: bool
    allowed_agents: tuple[str, ...] | None = None
    max_depth: int | None = None
    max_parallelism: int | None = None

    def record(self) -> dict[str, object]:
        """返回稳定的脱敏结构。"""
        return {
            "enabled": self.enabled,
            "allowed_agents": list(self.allowed_agents) if self.allowed_agents is not None else None,
            "max_depth": self.max_depth,
            "max_parallelism": self.max_parallelism,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPolicyDefinition:
    """唯一承载工具、文件、Shell、网络与 delegation 边界的静态策略。"""

    policy_id: str
    source: str
    tools: StringRule | None = None
    filesystem_read: tuple[str, ...] | None = None
    filesystem_write: tuple[str, ...] | None = None
    shell: ShellPolicy | None = None
    network: NetworkPolicy | None = None
    isolation: str | None = None
    approval_mode: str | None = None
    delegation: DelegationPolicy | None = None

    @property
    def fingerprint(self) -> str:
        """返回策略有效内容指纹，不包含来源的本机路径。"""
        return _fingerprint(self.record())

    def record(self) -> dict[str, object]:
        """返回可审计且不含文件路径或秘密的策略摘要。"""
        return {
            "id": self.policy_id,
            "tools": self.tools.record() if self.tools else None,
            "filesystem": {
                "read": list(self.filesystem_read) if self.filesystem_read is not None else None,
                "write": list(self.filesystem_write) if self.filesystem_write is not None else None,
            },
            "shell": self.shell.record() if self.shell else None,
            "network": self.network.record() if self.network else None,
            "isolation": self.isolation,
            "approval": self.approval_mode,
            "delegation": self.delegation.record() if self.delegation else None,
        }

    def summary(self) -> dict[str, object]:
        """返回未来 RPC/TUI 可安全展示的只读摘要。"""
        return {"id": self.policy_id, "source": self.source, "fingerprint": self.fingerprint}


@dataclass(frozen=True, slots=True)
class CatalogAsset:
    """已验证、由 catalog 持有的 Prompt 或 JSON Schema 文件。"""

    name: str
    path: Path
    digest: str


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """一个可被主 Agent 动态派发的 Plugin Agent 静态定义。"""

    agent_id: str
    description: str | None
    purpose: str
    source: str
    instructions: CatalogAsset
    instruction_fragments: tuple[CatalogAsset, ...]
    input_contract: CatalogAsset | None
    output_contract: CatalogAsset | None
    success_criteria: tuple[str, ...]
    model_profile_id: str
    execution_policy_id: str

    @property
    def fingerprint(self) -> str:
        """返回 Agent 内容及其引用资产的稳定指纹。"""
        return _fingerprint(
            {
                "id": self.agent_id,
                "description": self.description,
                "purpose": self.purpose,
                "instructions": self.instructions.digest,
                "fragments": [(asset.name, asset.digest) for asset in self.instruction_fragments],
                "input_contract": self.input_contract.digest if self.input_contract else None,
                "output_contract": self.output_contract.digest if self.output_contract else None,
                "success_criteria": list(self.success_criteria),
                "model_profile": self.model_profile_id,
                "execution_policy": self.execution_policy_id,
            }
        )

    def summary(self) -> dict[str, object]:
        """返回不含 Prompt、Schema 正文或本机路径的目录摘要。"""
        return {
            "id": self.agent_id,
            "description": self.description,
            "purpose": self.purpose,
            "model_profile_id": self.model_profile_id,
            "execution_policy_id": self.execution_policy_id,
            "source": self.source,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class EffectiveExecutionPolicy:
    """多个安全 envelope 取交集后的内部值对象，不是可配置目录项。"""

    policy_ids: tuple[str, ...]
    tools: StringRule | None
    filesystem_read: tuple[str, ...] | None
    filesystem_write: tuple[str, ...] | None
    shell: ShellPolicy | None
    network: NetworkPolicy | None
    isolation: str | None
    approval_mode: str | None
    delegation: DelegationPolicy | None

    @property
    def fingerprint(self) -> str:
        """返回有效安全边界的稳定指纹。"""
        return _fingerprint(
            {
                "policy_ids": list(self.policy_ids),
                "tools": self.tools.record() if self.tools else None,
                "filesystem_read": list(self.filesystem_read) if self.filesystem_read is not None else None,
                "filesystem_write": list(self.filesystem_write) if self.filesystem_write is not None else None,
                "shell": self.shell.record() if self.shell else None,
                "network": self.network.record() if self.network else None,
                "isolation": self.isolation,
                "approval": self.approval_mode,
                "delegation": self.delegation.record() if self.delegation else None,
            }
        )


class AgentCatalog:
    """从已信任 Plugin 资产建立不可变 Agent/Policy 快照。"""

    def __init__(
        self,
        *,
        model_catalog: ModelCatalog,
        sources: tuple[PluginAgentSource, ...] = (),
    ) -> None:
        """仅加载 Plugin 层显式传入的根；主 Agent 与项目目录永不在此读取。"""
        self._model_catalog = model_catalog
        diagnostics: list[str] = []
        policies: dict[str, ExecutionPolicyDefinition] = {}
        agents: dict[str, AgentDefinition] = {}
        for source in sorted(sources, key=lambda value: value.plugin_id):
            label = f"plugin:{source.plugin_id}"
            root = source.root.expanduser()
            if root.is_symlink():
                diagnostics.append(f"{label}: plugin root must not be a symlink")
                continue
            accepted_policies: dict[str, ExecutionPolicyDefinition] = {}
            for policy_id, policy in self._load_policies(root / "policies", label, diagnostics).items():
                if policy_id in policies:
                    diagnostics.append(f'{label} policy "{policy_id}": duplicate policy ID ignored')
                    continue
                policies[policy_id] = policy
                accepted_policies[policy_id] = policy
            for agent_id, agent in self._load_agents(
                root / "agents", label, root, accepted_policies, diagnostics
            ).items():
                if agent_id in agents:
                    diagnostics.append(f'{label} agent "{agent_id}": duplicate Agent ID ignored')
                    continue
                agents[agent_id] = agent
        self._agents = MappingProxyType(dict(sorted(agents.items())))
        self._policies = MappingProxyType(dict(sorted(policies.items())))
        self.diagnostics = tuple(diagnostics)
        self.snapshot_id = _fingerprint(
            {
                "agents": [
                    {"id": record.agent_id, "source": record.source, "fingerprint": record.fingerprint}
                    for record in self.agents
                ],
                "policies": [
                    {"id": record.policy_id, "source": record.source, "fingerprint": record.fingerprint}
                    for record in self.policies
                ],
            }
        )

    @property
    def agents(self) -> tuple[AgentDefinition, ...]:
        """返回启动期固定、按 ID 排序的 Agent 定义。"""
        return tuple(self._agents.values())

    @property
    def policies(self) -> tuple[ExecutionPolicyDefinition, ...]:
        """返回启动期固定、按 ID 排序的 Policy 定义。"""
        return tuple(self._policies.values())

    def snapshot(self) -> dict[str, object]:
        """返回可记录在 Runtime Profile 的脱敏 catalog 快照摘要。"""
        return {"id": self.snapshot_id, "agents": len(self.agents), "policies": len(self.policies)}

    def list_agents(self) -> list[dict[str, object]]:
        """列出未来后端/TUI 所需的安全 Agent 摘要。"""
        return [agent.summary() for agent in self.agents]

    def list_policies(self) -> list[dict[str, object]]:
        """列出未来后端/TUI 所需的安全 Policy 摘要。"""
        return [policy.summary() for policy in self.policies]

    def require_agent(self, agent_id: str) -> AgentDefinition:
        """按 ID 读取已验证 Agent，未知定义 fail closed。"""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise AgentCatalogError(f"AGENT_NOT_FOUND: {agent_id}")
        return agent

    def require_policy(self, policy_id: str) -> ExecutionPolicyDefinition:
        """按 ID 读取已验证 Policy，未知定义 fail closed。"""
        policy = self._policies.get(policy_id)
        if policy is None:
            raise AgentCatalogError(f"EXECUTION_POLICY_NOT_FOUND: {policy_id}")
        return policy

    def effective_policy(
        self,
        agent_id: str,
        *,
        envelope: ExecutionPolicyDefinition | EffectiveExecutionPolicy | None = None,
    ) -> EffectiveExecutionPolicy:
        """计算目标 Agent Policy 与调用方 envelope 的纯交集。"""
        target = self.require_policy(self.require_agent(agent_id).execution_policy_id)
        return intersect_execution_policies(envelope, target)

    def _load_policies(
        self, root: Path, source: str, diagnostics: list[str]
    ) -> dict[str, ExecutionPolicyDefinition]:
        """读取单层 JSON Policy；损坏项只增加脱敏诊断。"""
        records: dict[str, ExecutionPolicyDefinition] = {}
        for path in _json_files(root):
            try:
                policy = _parse_policy(path, source)
                if policy.policy_id in records:
                    raise AgentCatalogError("duplicate policy ID")
                records[policy.policy_id] = policy
            except (AgentCatalogError, OSError, json.JSONDecodeError) as exc:
                diagnostics.append(_diagnostic(source, "policy", path.name, exc))
        return records

    def _load_agents(
        self,
        root: Path,
        source: str,
        asset_root: Path,
        policies: Mapping[str, ExecutionPolicyDefinition],
        diagnostics: list[str],
    ) -> dict[str, AgentDefinition]:
        """读取单层 JSON Agent，并只允许其引用同一可信来源根目录内的资产。"""
        records: dict[str, AgentDefinition] = {}
        for path in _json_files(root):
            try:
                agent = _parse_agent(
                    path,
                    source=source,
                    root=root,
                    asset_root=asset_root,
                    policies=policies,
                    model_catalog=self._model_catalog,
                )
                if agent.agent_id in records:
                    raise AgentCatalogError("duplicate Agent ID")
                records[agent.agent_id] = agent
            except (AgentCatalogError, ConfigError, OSError, json.JSONDecodeError) as exc:
                diagnostics.append(_diagnostic(source, "agent", path.name, exc))
        return records


def intersect_execution_policies(
    envelope: ExecutionPolicyDefinition | EffectiveExecutionPolicy | None,
    target: ExecutionPolicyDefinition,
) -> EffectiveExecutionPolicy:
    """取安全策略交集；不相容隔离方式显式失败，绝不选择更宽松的一方。"""
    if envelope is None:
        base = _effective_from_definition(target)
        return base
    base = _as_effective(envelope)
    isolation = _intersect_isolation(base.isolation, target.isolation)
    approval = _intersect_approval(base.approval_mode, target.approval_mode)
    return EffectiveExecutionPolicy(
        policy_ids=(*base.policy_ids, target.policy_id),
        tools=_intersect_rule(base.tools, target.tools),
        filesystem_read=_intersect_paths(base.filesystem_read, target.filesystem_read),
        filesystem_write=_intersect_paths(base.filesystem_write, target.filesystem_write),
        shell=_intersect_shell(base.shell, target.shell),
        network=_intersect_network(base.network, target.network),
        isolation=isolation,
        approval_mode=approval,
        delegation=_intersect_delegation(base.delegation, target.delegation),
    )


def _effective_from_definition(policy: ExecutionPolicyDefinition) -> EffectiveExecutionPolicy:
    """将一份静态 Policy 投影为可继续取交集的内部值。"""
    return EffectiveExecutionPolicy(
        policy_ids=(policy.policy_id,),
        tools=policy.tools,
        filesystem_read=policy.filesystem_read,
        filesystem_write=policy.filesystem_write,
        shell=policy.shell,
        network=policy.network,
        isolation=policy.isolation,
        approval_mode=policy.approval_mode,
        delegation=policy.delegation,
    )


def _as_effective(value: ExecutionPolicyDefinition | EffectiveExecutionPolicy) -> EffectiveExecutionPolicy:
    """统一静态定义和已求交的调用方 envelope。"""
    return value if isinstance(value, EffectiveExecutionPolicy) else _effective_from_definition(value)


def _intersect_rule(left: StringRule | None, right: StringRule | None) -> StringRule | None:
    """allow 取最小集合，deny 合并；缺失规则仅表示该层没有新增授权。"""
    if left is None:
        return right
    if right is None:
        return left
    return StringRule(allow=_intersect_values(left.allow, right.allow), deny=tuple(sorted(set(left.deny) | set(right.deny))))


def _intersect_paths(
    left: tuple[str, ...] | None, right: tuple[str, ...] | None
) -> tuple[str, ...] | None:
    """保守求文件 glob 交集；无法表示的重叠宁可拒绝，也不能扩权。"""
    return _intersect_values(left, right, universal="**/*")


def _intersect_values(
    left: tuple[str, ...] | None,
    right: tuple[str, ...] | None,
    *,
    universal: str | None = None,
) -> tuple[str, ...] | None:
    """计算两个有限 allow 集合的安全交集。"""
    if left is None:
        return right
    if right is None:
        return left
    if universal is not None:
        if left == (universal,):
            return right
        if right == (universal,):
            return left
    return tuple(sorted(set(left).intersection(right)))


def _intersect_shell(left: ShellPolicy | None, right: ShellPolicy | None) -> ShellPolicy | None:
    """任何一层禁用 Shell 都必须禁用；命令列表继续取交集。"""
    if left is None:
        return right
    if right is None:
        return left
    return ShellPolicy(enabled=left.enabled and right.enabled, commands=_intersect_rule(left.commands, right.commands) or StringRule())


def _intersect_network(left: NetworkPolicy | None, right: NetworkPolicy | None) -> NetworkPolicy | None:
    """任何一层禁用网络都必须禁用；host 列表继续取交集。"""
    if left is None:
        return right
    if right is None:
        return left
    return NetworkPolicy(
        enabled=left.enabled and right.enabled,
        allowed_hosts=_intersect_values(left.allowed_hosts, right.allowed_hosts),
    )


def _intersect_isolation(left: str | None, right: str | None) -> str | None:
    """隔离模式没有可证明的偏序时拒绝混用，防止意外退回本机执行。"""
    if left is None:
        return right
    if right is None or left == right:
        return left
    raise AgentCatalogError("EXECUTION_POLICY_ISOLATION_CONFLICT")


def _intersect_approval(left: str | None, right: str | None) -> str | None:
    """按 never < on-risk < always 选择更严格的审批要求。"""
    if left is None:
        return right
    if right is None:
        return left
    return max((left, right), key=_APPROVAL_MODES.index)


def _intersect_delegation(
    left: DelegationPolicy | None, right: DelegationPolicy | None
) -> DelegationPolicy | None:
    """派发允许集、深度与并发数均只能收紧。"""
    if left is None:
        return right
    if right is None:
        return left
    return DelegationPolicy(
        enabled=left.enabled and right.enabled,
        allowed_agents=_intersect_values(left.allowed_agents, right.allowed_agents),
        max_depth=_minimum(left.max_depth, right.max_depth),
        max_parallelism=_minimum(left.max_parallelism, right.max_parallelism),
    )


def _minimum(left: int | None, right: int | None) -> int | None:
    """缺失上限不收紧，两个上限同时存在时取较小值。"""
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _parse_policy(path: Path, source: str) -> ExecutionPolicyDefinition:
    """解析一个 Policy JSON，拒绝未知字段和无法安全解释的值。"""
    data = _read_json_object(path)
    _reject_unknown(
        data,
        {"id", "tools", "filesystem", "shell", "network", "isolation", "approval", "delegation"},
        "policy",
    )
    policy_id = _identifier(data.get("id"), "policy.id")
    return ExecutionPolicyDefinition(
        policy_id=policy_id,
        source=source,
        tools=_parse_rule(data.get("tools"), "policy.tools", identifier_values=True),
        filesystem_read=_parse_paths(data.get("filesystem"), "read"),
        filesystem_write=_parse_paths(data.get("filesystem"), "write"),
        shell=_parse_shell(data.get("shell")),
        network=_parse_network(data.get("network")),
        isolation=_parse_isolation(data.get("isolation")),
        approval_mode=_parse_approval(data.get("approval")),
        delegation=_parse_delegation(data.get("delegation")),
    )


def _parse_agent(
    path: Path,
    *,
    source: str,
    root: Path,
    asset_root: Path,
    policies: Mapping[str, ExecutionPolicyDefinition],
    model_catalog: ModelCatalog,
) -> AgentDefinition:
    """解析一个 Agent JSON，并在加载点完成所有引用与资产边界校验。"""
    data = _read_json_object(path)
    _reject_unknown(
        data,
        {
            "id", "description", "purpose", "instructionsRef", "instructionFragments",
            "inputContractRef", "outputContractRef", "successCriteria", "modelProfileId", "executionPolicyId",
        },
        "agent",
    )
    agent_id = _identifier(data.get("id"), "agent.id")
    description = _optional_text(data.get("description"), "agent.description")
    purpose = _required_text(data.get("purpose"), "agent.purpose")
    model_profile_id = _required_text(data.get("modelProfileId"), "agent.modelProfileId")
    model_catalog.require_profile(model_profile_id)
    policy_id = _identifier(data.get("executionPolicyId"), "agent.executionPolicyId")
    if policy_id not in policies:
        raise AgentCatalogError("agent.executionPolicyId references an unknown policy")
    instructions = _asset(root, data.get("instructionsRef"), ".md", "agent.instructionsRef")
    fragments = tuple(
        _asset(asset_root / "instructions", value, ".md", "agent.instructionFragments")
        for value in _string_list(data.get("instructionFragments"), "agent.instructionFragments", required=False)
    )
    input_contract = _optional_asset(
        asset_root / "schemas", data.get("inputContractRef"), ".json", "agent.inputContractRef"
    )
    output_contract = _optional_asset(
        asset_root / "schemas", data.get("outputContractRef"), ".json", "agent.outputContractRef"
    )
    for contract in (input_contract, output_contract):
        if contract is not None:
            _validate_json_schema(contract.path)
    return AgentDefinition(
        agent_id=agent_id,
        description=description,
        purpose=purpose,
        source=source,
        instructions=instructions,
        instruction_fragments=fragments,
        input_contract=input_contract,
        output_contract=output_contract,
        success_criteria=tuple(_string_list(data.get("successCriteria"), "agent.successCriteria", required=False)),
        model_profile_id=model_profile_id,
        execution_policy_id=policy_id,
    )


def _parse_rule(value: object, field: str, *, identifier_values: bool) -> StringRule | None:
    """解析通用 allow/deny 表，避免 Policy 通过未知字段静默扩权。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError(f"{field} must be an object")
    _reject_unknown(value, {"allow", "deny"}, field)
    allow = _string_list(value.get("allow"), f"{field}.allow", required=False, identifier_values=identifier_values)
    deny = _string_list(value.get("deny"), f"{field}.deny", required=False, identifier_values=identifier_values)
    return StringRule(allow=tuple(allow) if "allow" in value else None, deny=tuple(deny))


def _parse_paths(value: object, name: str) -> tuple[str, ...] | None:
    """读取 filesystem 表内一种虚拟工作区 glob；空数组表示明确拒绝。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.filesystem must be an object")
    _reject_unknown(value, {"read", "write"}, "policy.filesystem")
    if name not in value:
        return None
    patterns = _string_list(value[name], f"policy.filesystem.{name}", required=True)
    for pattern in patterns:
        if pattern.startswith(("/", "\\")) or ".." in Path(pattern).parts or "\\" in pattern:
            raise AgentCatalogError(f"policy.filesystem.{name} contains an unsafe path pattern")
    return tuple(patterns)


def _parse_shell(value: object) -> ShellPolicy | None:
    """解析 Shell 开关与命令规则。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.shell must be an object")
    _reject_unknown(value, {"enabled", "allowedCommands", "deniedCommands"}, "policy.shell")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentCatalogError("policy.shell.enabled must be boolean")
    command_values = {
        key: value[source]
        for key, source in (("allow", "allowedCommands"), ("deny", "deniedCommands"))
        if source in value
    }
    commands = _parse_rule(command_values, "policy.shell.commands", identifier_values=False)
    return ShellPolicy(enabled=enabled, commands=commands or StringRule())


def _parse_network(value: object) -> NetworkPolicy | None:
    """解析 Network 开关和可选 host allow-list。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.network must be an object")
    _reject_unknown(value, {"enabled", "allowedHosts"}, "policy.network")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentCatalogError("policy.network.enabled must be boolean")
    hosts = _string_list(value.get("allowedHosts"), "policy.network.allowedHosts", required=False)
    return NetworkPolicy(enabled=enabled, allowed_hosts=tuple(hosts) if "allowedHosts" in value else None)


def _parse_isolation(value: object) -> str | None:
    """解析固定隔离模式，不接受可执行 backend 或工厂引用。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.isolation must be an object")
    _reject_unknown(value, {"mode"}, "policy.isolation")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in _ISOLATION_MODES:
        raise AgentCatalogError("policy.isolation.mode is unsupported")
    return mode


def _parse_approval(value: object) -> str | None:
    """解析通用审批严格度，后续再映射到现有 Harness ApprovalMode。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.approval must be an object")
    _reject_unknown(value, {"mode"}, "policy.approval")
    mode = value.get("mode")
    if not isinstance(mode, str) or mode not in _APPROVAL_MODES:
        raise AgentCatalogError("policy.approval.mode is unsupported")
    return mode


def _parse_delegation(value: object) -> DelegationPolicy | None:
    """解析派发边界；不允许以 Policy 文件注入执行 Hook。"""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentCatalogError("policy.delegation must be an object")
    _reject_unknown(value, {"enabled", "allowedAgents", "maxDepth", "maxParallelism"}, "policy.delegation")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise AgentCatalogError("policy.delegation.enabled must be boolean")
    allowed = _string_list(value.get("allowedAgents"), "policy.delegation.allowedAgents", required=False, identifier_values=True)
    return DelegationPolicy(
        enabled=enabled,
        allowed_agents=tuple(allowed) if "allowedAgents" in value else None,
        max_depth=_positive_int(value.get("maxDepth"), "policy.delegation.maxDepth"),
        max_parallelism=_positive_int(value.get("maxParallelism"), "policy.delegation.maxParallelism"),
    )


def _asset(root: Path, value: object, suffix: str, field: str) -> CatalogAsset:
    """读取受根目录约束的单个资产，不允许绝对路径、父目录或 symlink。"""
    if not isinstance(value, str) or not value:
        raise AgentCatalogError(f"{field} must be a non-empty filename")
    if Path(value).name != value or not value.endswith(suffix):
        raise AgentCatalogError(f"{field} must be a {suffix} filename without a path")
    path = root / value
    content = _read_limited_text(path)
    if suffix == ".md" and not content.strip():
        raise AgentCatalogError(f"{field} must not reference an empty file")
    return CatalogAsset(name=value, path=path.resolve(), digest=_digest(content.encode()))


def _optional_asset(root: Path, value: object, suffix: str, field: str) -> CatalogAsset | None:
    """在可选引用缺失时返回 None，其余情况与必填资产同样严格校验。"""
    return None if value is None else _asset(root, value, suffix, field)


def _validate_json_schema(path: Path) -> None:
    """验证 Schema 为安全 JSON 对象，并禁用跨文件或远端 ``$ref``。"""
    value = _read_json_object(path)
    _reject_external_refs(value)


def _reject_external_refs(value: object) -> None:
    """递归检查 `$ref`，catalog 不允许读取其目录外的第二个 Schema。"""
    if isinstance(value, Mapping):
        reference = value.get("$ref")
        if reference is not None and (
            not isinstance(reference, str)
            or (not reference.startswith("#") and reference != "")
        ):
            raise AgentCatalogError("JSON Schema contains an external $ref")
        for item in value.values():
            _reject_external_refs(item)
    elif isinstance(value, list):
        for item in value:
            _reject_external_refs(item)


def _read_json_object(path: Path) -> dict[str, Any]:
    """读取限制大小的 JSON 对象。"""
    try:
        value = json.loads(_read_limited_text(path))
    except json.JSONDecodeError as exc:
        raise AgentCatalogError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise AgentCatalogError("JSON root must be an object")
    return value


def _read_limited_text(path: Path) -> str:
    """只读取普通 UTF-8 文件，避免 symlink 和超大资产穿透信任边界。"""
    if path.is_symlink() or not path.is_file():
        raise AgentCatalogError("referenced file must be a regular file")
    if path.stat().st_size > MAX_CATALOG_FILE_BYTES:
        raise AgentCatalogError(f"referenced file exceeds {MAX_CATALOG_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def _json_files(root: Path) -> tuple[Path, ...]:
    """列出目录第一层普通 JSON 文件；不存在、symlink 或项目目录均不递归。"""
    if not root.is_dir() or root.is_symlink():
        return ()
    try:
        return tuple(sorted((path for path in root.iterdir() if path.suffix == ".json" and path.is_file() and not path.is_symlink()), key=lambda path: path.name))
    except OSError:
        return ()


def _identifier(value: object, field: str) -> str:
    """校验所有 catalog ID 为稳定 kebab-case。"""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise AgentCatalogError(f"{field} must be kebab-case")
    return value


def _required_text(value: object, field: str) -> str:
    """读取有边界的必填文本。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 2_000:
        raise AgentCatalogError(f"{field} must be a non-empty string up to 2000 characters")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    """读取可选文本，不接受空白或超长描述。"""
    if value is None:
        return None
    return _required_text(value, field)


def _string_list(
    value: object,
    field: str,
    *,
    required: bool,
    identifier_values: bool = False,
) -> list[str]:
    """读取去重、排序的短字符串列表，保证指纹与交集结果稳定。"""
    if value is None and not required:
        return []
    if not isinstance(value, list) or len(value) > 128:
        raise AgentCatalogError(f"{field} must be an array with at most 128 entries")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256 or "\n" in item or "\r" in item:
            raise AgentCatalogError(f"{field} contains an invalid string")
        if identifier_values and not _TOOL_IDENTIFIER_RE.fullmatch(item):
            raise AgentCatalogError(f"{field} contains an invalid identifier")
        result.add(item)
    return sorted(result)


def _positive_int(value: object, field: str) -> int | None:
    """读取可选且有上限的正整数，避免配置形成无界并发。"""
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 32:
        raise AgentCatalogError(f"{field} must be an integer between 1 and 32")
    return value


def _reject_unknown(value: Mapping[str, object], allowed: set[str], field: str) -> None:
    """拒绝未知字段，避免拼写错误或未来执行 Hook 被静默接受。"""
    unknown = set(value) - allowed
    if unknown:
        raise AgentCatalogError(f"{field} contains unsupported fields: {', '.join(sorted(unknown))}")


def _fingerprint(value: object) -> str:
    """生成完整 SHA-256，以便后续 Runtime/Run 使用而不保存资产正文。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _digest(content: bytes) -> str:
    """计算单个已加载资产的 SHA-256。"""
    return hashlib.sha256(content).hexdigest()


def _diagnostic(source: str, kind: str, filename: str, error: Exception) -> str:
    """生成不回显路径、正文、endpoint 或凭据的 catalog 诊断。"""
    return f'{source} {kind} "{filename}": {error}'
