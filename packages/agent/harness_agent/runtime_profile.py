"""Agent Runtime Profile 的稳定身份、脱敏摘要和配置转换。

Runtime Profile 描述可共享 Agent 图必须保持不变的配置。它刻意不保存
thread 消息、PromptEpoch 正文、审批交互或任何凭据；这些内容属于持久
Thread State 或一次 Run Context，不能影响共享 Runtime 的身份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from harness_agent.prompting import canonical_json, sha256_text


RUNTIME_PROFILE_VERSION = 1
"""Runtime Profile 持久化记录的当前版本。"""

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class RuntimeProfileError(ValueError):
    """Runtime Profile 字段、记录或身份校验失败时抛出。"""


def component_fingerprint(value: object) -> str:
    """对一个稳定组件生成 SHA-256，不把原始配置写入 Profile 记录。"""
    return sha256_text(canonical_json(value))


@dataclass(frozen=True, slots=True)
class ModelRoleBinding:
    """一个 Agent 角色绑定的脱敏模型配置指纹。"""

    role: str
    model_config_fingerprint: str

    def __post_init__(self) -> None:
        """拒绝未规范化角色与不完整指纹，保持 Profile Key 可验证。"""
        if not _IDENTIFIER_RE.fullmatch(self.role):
            raise RuntimeProfileError("RUNTIME_PROFILE_ROLE_INVALID")
        _require_fingerprint("model_config_fingerprint", self.model_config_fingerprint)

    def record(self) -> dict[str, str]:
        """返回可持久化的角色绑定，不含模型 endpoint 或认证信息。"""
        return {"role": self.role, "model_config_fingerprint": self.model_config_fingerprint}


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """决定一个 Agent Runtime 能否被多个 thread 共享的不可变配置。"""

    project_fingerprint: str
    topology_id: str
    topology_version: int
    model_roles: tuple[ModelRoleBinding, ...]
    tool_catalog_fingerprint: str
    skill_catalog_fingerprint: str
    mcp_config_fingerprint: str
    sandbox_config_fingerprint: str
    policy_fingerprint: str
    middleware_fingerprint: str
    prompt_template_fingerprint: str

    def __post_init__(self) -> None:
        """规范化角色顺序并校验所有会改变共享图的稳定字段。"""
        _require_fingerprint("project_fingerprint", self.project_fingerprint)
        if not _IDENTIFIER_RE.fullmatch(self.topology_id):
            raise RuntimeProfileError("RUNTIME_PROFILE_TOPOLOGY_INVALID")
        if self.topology_version < 1:
            raise RuntimeProfileError("RUNTIME_PROFILE_TOPOLOGY_VERSION_INVALID")
        ordered_roles = tuple(sorted(self.model_roles, key=lambda binding: binding.role))
        if not ordered_roles or len({binding.role for binding in ordered_roles}) != len(ordered_roles):
            raise RuntimeProfileError("RUNTIME_PROFILE_ROLES_INVALID")
        object.__setattr__(self, "model_roles", ordered_roles)
        for field_name, value in (
            ("tool_catalog_fingerprint", self.tool_catalog_fingerprint),
            ("skill_catalog_fingerprint", self.skill_catalog_fingerprint),
            ("mcp_config_fingerprint", self.mcp_config_fingerprint),
            ("sandbox_config_fingerprint", self.sandbox_config_fingerprint),
            ("policy_fingerprint", self.policy_fingerprint),
            ("middleware_fingerprint", self.middleware_fingerprint),
            ("prompt_template_fingerprint", self.prompt_template_fingerprint),
        ):
            _require_fingerprint(field_name, value)

    @property
    def profile_key(self) -> str:
        """返回由全部稳定配置组成的可复算 Runtime Profile Key。"""
        return component_fingerprint(self.identity())

    def identity(self) -> dict[str, object]:
        """返回参与 Key 的完整脱敏身份，禁止加入 thread/run 动态状态。"""
        return {
            "version": RUNTIME_PROFILE_VERSION,
            "project_fingerprint": self.project_fingerprint,
            "topology": {"id": self.topology_id, "version": self.topology_version},
            "model_roles": [binding.record() for binding in self.model_roles],
            "tool_catalog_fingerprint": self.tool_catalog_fingerprint,
            "skill_catalog_fingerprint": self.skill_catalog_fingerprint,
            "mcp_config_fingerprint": self.mcp_config_fingerprint,
            "sandbox_config_fingerprint": self.sandbox_config_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "middleware_fingerprint": self.middleware_fingerprint,
            "prompt_template_fingerprint": self.prompt_template_fingerprint,
        }

    def record(self) -> dict[str, object]:
        """返回 SQLite 可保存记录；所有原始路径、提示词和秘密均已排除。"""
        return {**self.identity(), "profile_key": self.profile_key}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "RuntimeProfile":
        """从持久化记录恢复 Profile，并重新计算 Key 防止静默篡改。"""
        try:
            version = int(record["version"])
            topology = record["topology"]
            roles = record["model_roles"]
            if version != RUNTIME_PROFILE_VERSION or not isinstance(topology, Mapping) or not isinstance(roles, Sequence):
                raise RuntimeProfileError("RUNTIME_PROFILE_RECORD_INVALID")
            bindings = tuple(
                ModelRoleBinding(
                    role=str(value["role"]),
                    model_config_fingerprint=str(value["model_config_fingerprint"]),
                )
                for value in roles
                if isinstance(value, Mapping)
            )
            if len(bindings) != len(roles):
                raise RuntimeProfileError("RUNTIME_PROFILE_RECORD_INVALID")
            profile = cls(
                project_fingerprint=str(record["project_fingerprint"]),
                topology_id=str(topology["id"]),
                topology_version=int(topology["version"]),
                model_roles=bindings,
                tool_catalog_fingerprint=str(record["tool_catalog_fingerprint"]),
                skill_catalog_fingerprint=str(record["skill_catalog_fingerprint"]),
                mcp_config_fingerprint=str(record["mcp_config_fingerprint"]),
                sandbox_config_fingerprint=str(record["sandbox_config_fingerprint"]),
                policy_fingerprint=str(record["policy_fingerprint"]),
                middleware_fingerprint=str(record["middleware_fingerprint"]),
                prompt_template_fingerprint=str(record["prompt_template_fingerprint"]),
            )
            expected_key = str(record["profile_key"])
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RuntimeProfileError):
                raise
            raise RuntimeProfileError("RUNTIME_PROFILE_RECORD_INVALID") from exc
        if profile.profile_key != expected_key:
            raise RuntimeProfileError("RUNTIME_PROFILE_KEY_MISMATCH")
        return profile


def model_settings_fingerprint(
    *,
    profile_name: str | None,
    model: Any,
) -> str:
    """为当前 OpenAI-compatible 模型配置计算不含秘密的稳定指纹。"""
    return component_fingerprint(
        {
            "profile_name": profile_name or "default",
            "provider": "openai-compatible",
            "name": str(model.name),
            "base_url": str(model.base_url),
            "api_key_env": str(model.api_key_env),
            "timeout_seconds": float(model.timeout_seconds),
            "max_retries": int(model.max_retries),
            "context_window_tokens": int(model.context_window_tokens),
            "capabilities": sorted(str(capability) for capability in model.capabilities),
            "headers": dict(model.headers),
            "headers_env": dict(model.headers_env),
        }
    )


def default_runtime_profile(
    *,
    project_fingerprint: str,
    model_profile: str | None,
    model: Any,
    tool_catalog_fingerprint: str,
    skill_catalog_fingerprint: str,
    execution: Any,
    middleware_fingerprint: str,
    prompt_template_fingerprint: str,
) -> RuntimeProfile:
    """按当前单 Agent / 单模型配置创建未来 RuntimePool 可直接使用的 Profile。"""
    return RuntimeProfile(
        project_fingerprint=project_fingerprint,
        topology_id="single-agent",
        topology_version=1,
        model_roles=(
            ModelRoleBinding(
                role="primary",
                model_config_fingerprint=model_settings_fingerprint(
                    profile_name=model_profile,
                    model=model,
                ),
            ),
        ),
        tool_catalog_fingerprint=tool_catalog_fingerprint,
        skill_catalog_fingerprint=skill_catalog_fingerprint,
        mcp_config_fingerprint=component_fingerprint({"transport": "disabled"}),
        sandbox_config_fingerprint=component_fingerprint(
            {
                "mode": str(execution.mode),
                "provider": execution.remote.provider if execution.remote else None,
                "working_directory": execution.remote.working_directory if execution.remote else None,
                "params": dict(execution.remote.params) if execution.remote else {},
            }
        ),
        policy_fingerprint=component_fingerprint({"approval_mode": str(execution.approval_mode)}),
        middleware_fingerprint=middleware_fingerprint,
        prompt_template_fingerprint=prompt_template_fingerprint,
    )


def _require_fingerprint(field_name: str, value: str) -> None:
    """限制持久化身份字段为小写 SHA-256，避免把原始配置写入数据库。"""
    if not _HASH_RE.fullmatch(value):
        raise RuntimeProfileError(f"RUNTIME_PROFILE_{field_name.upper()}_INVALID")
