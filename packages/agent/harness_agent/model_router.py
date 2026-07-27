"""模型角色解析与单次 Run 的脱敏快照。

本模块不创建 Agent 或网络客户端，只把可信配置目录转换为不可变的角色
Profile 选择。Thread 可变选择不保存在本模块；这样 ZC-060 可以复用它。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from harness_agent.config import (
    DEFAULT_MODEL_CAPABILITIES,
    MODEL_ROLES,
    ConfigError,
    ModelCatalog,
    ModelProfile,
)


@dataclass(frozen=True, slots=True)
class ThreadModelBindings:
    """一次 Run 的 canonical 角色到 Profile 映射；名称保留以兼容 legacy 记录。"""

    profiles: Mapping[str, ModelProfile]

    def for_role(self, role: str) -> ModelProfile:
        """读取已解析角色的 Profile，缺失映射必须 fail closed。"""
        profile = self.profiles.get(role)
        if profile is None:
            raise ConfigError(f"THREAD_MODEL_ROLE_MISSING: {role}")
        return profile

    def runtime_primary(self) -> ModelProfile:
        """当前 Single Agent 的 primary 模型固定来自 executor 角色。"""
        return self.for_role("executor")

    def record(self) -> dict[str, object]:
        """生成可存 SQLite/显示的安全快照，排除 endpoint、Header 和任何凭据。"""
        return {
            "roles": {
                role: profile.picker_summary()
                for role, profile in sorted(self.profiles.items())
            }
        }


class ModelRouter:
    """按配置为单次 Run 解析模型 Profile，不持有 Thread 可变状态。"""

    def __init__(self, catalog: ModelCatalog) -> None:
        """保存已验证且不可变的 ModelCatalog。"""
        self._catalog = catalog

    def resolve_run(self, primary_profile_id: str | None = None) -> ThreadModelBindings:
        """生成本次 Run 的完整角色快照；显式选择仅覆盖物理 primary/executor。"""
        if primary_profile_id is not None:
            selected = self._catalog.require_profile(primary_profile_id)
            if selected.settings.api_key_source() == "missing":
                raise ConfigError("MODEL_PROFILE_UNAVAILABLE: API_KEY_MISSING")
        else:
            selected = self._catalog.profile_for_role("executor")
            if selected.settings.api_key_source() == "missing":
                raise ConfigError("MODEL_PROFILE_UNAVAILABLE: API_KEY_MISSING")
        missing_capabilities = DEFAULT_MODEL_CAPABILITIES - selected.settings.capabilities
        if missing_capabilities:
            missing = ",".join(sorted(missing_capabilities))
            raise ConfigError(f"MODEL_PROFILE_CAPABILITY_MISSING: {missing}")
        profiles = {role: self._catalog.profile_for_role(role) for role in MODEL_ROLES}
        profiles["executor"] = selected
        return ThreadModelBindings(profiles=profiles)

    def bind_thread(self, executor_profile_id: str | None = None) -> ThreadModelBindings:
        """兼容旧调用方；新代码必须使用 ``resolve_run``，不再写入 Thread 绑定。"""
        return self.resolve_run(executor_profile_id)

    def from_record(self, record: Mapping[str, object]) -> ThreadModelBindings:
        """按 legacy 持久化 Profile ID 重建执行映射；缺失 Profile 必须 fail closed。"""
        raw_roles = record.get("roles")
        if not isinstance(raw_roles, Mapping):
            raise ConfigError("THREAD_MODEL_BINDING_INVALID")
        profiles: dict[str, ModelProfile] = {}
        for role in MODEL_ROLES:
            raw_profile = raw_roles.get(role)
            if not isinstance(raw_profile, Mapping):
                raise ConfigError("THREAD_MODEL_BINDING_INVALID")
            profile_id = raw_profile.get("id")
            if not isinstance(profile_id, str):
                raise ConfigError("THREAD_MODEL_BINDING_INVALID")
            profiles[role] = self._catalog.require_profile(profile_id)
        return ThreadModelBindings(profiles=profiles)
