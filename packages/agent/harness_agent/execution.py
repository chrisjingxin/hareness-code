"""执行后端选择：默认本机，按显式配置接入企业远端沙箱。"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import SandboxBackendProtocol

from harness_agent.config import ConfigError, ExecutionSettings, RemoteSandboxSettings

if TYPE_CHECKING:
    from collections.abc import Mapping


@runtime_checkable
class RemoteSandboxFactory(Protocol):
    """企业 sandbox 插件必须实现的工厂契约。

    工厂由用户级或显式配置声明，负责认证、工作区上传/挂载以及远端资源
    生命周期。返回的 backend 必须实现 deepagents 的文件与 execute 接口，
    从而确保工具调用不会退回宿主机。
    """

    def __call__(
        self,
        *,
        workspace: Path,
        provider: str,
        working_directory: str,
        params: Mapping[str, object],
    ) -> SandboxBackendProtocol: ...


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """向 Agent 与提示词提供同一份执行环境事实。"""

    backend: Any
    mode: str
    workspace_path: str
    provider: str | None

    @property
    def sandboxed(self) -> bool:
        """判断工具是否运行在企业远端沙箱。"""
        return self.mode == "remote-sandbox"


def create_execution_context(
    settings: ExecutionSettings, workspace: Path
) -> ExecutionContext:
    """按配置创建唯一工具执行后端，sandbox 失败时绝不回退本机。"""
    if not settings.sandbox_enabled:
        # 显式关闭继承环境，避免模型凭据、云认证等无意暴露给本机 shell。
        backend = LocalShellBackend(
            root_dir=workspace,
            virtual_mode=False,
            inherit_env=False,
            env=_local_tool_environment(),
        )
        return ExecutionContext(
            backend=backend,
            mode="local",
            workspace_path=str(workspace),
            provider=None,
        )

    remote = settings.remote
    if remote is None:  # pragma: no cover - 配置解析层已保证，保留防御性边界。
        raise ConfigError("Remote sandbox settings are missing")
    backend = _create_remote_backend(remote, workspace)
    return ExecutionContext(
        backend=backend,
        mode="remote-sandbox",
        workspace_path=remote.working_directory,
        provider=remote.provider,
    )


def _create_remote_backend(
    settings: RemoteSandboxSettings, workspace: Path
) -> SandboxBackendProtocol:
    """导入用户明确配置的远端 provider，并验证其不能伪装成本机后端。"""
    module_name, separator, attribute = settings.factory.partition(":")
    if not separator or not module_name or not attribute:
        raise ConfigError(
            "sandbox.factory must use the 'package.module:factory_name' format"
        )
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(
            f"Remote sandbox provider '{settings.provider}' is unavailable: {exc}"
        ) from exc
    if not callable(factory):
        raise ConfigError("sandbox.factory must resolve to a callable")
    try:
        backend = factory(
            workspace=workspace,
            provider=settings.provider,
            working_directory=settings.working_directory,
            params=dict(settings.params),
        )
    except Exception as exc:
        raise ConfigError(
            f"Remote sandbox provider '{settings.provider}' failed to start: {exc}"
        ) from exc
    if inspect.isawaitable(backend):
        raise ConfigError("sandbox.factory must synchronously return a SandboxBackendProtocol")
    if not isinstance(backend, SandboxBackendProtocol):
        raise ConfigError(
            "sandbox.factory must return a deepagents SandboxBackendProtocol"
        )
    if isinstance(backend, LocalShellBackend):
        raise ConfigError("sandbox.factory must not return LocalShellBackend")
    return backend


def _local_tool_environment() -> dict[str, str]:
    """构建本机工具的最小环境，不透传模型、云或 SSH 凭据。"""
    import os

    allowed_names = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR")
    return {
        name: value
        for name in allowed_names
        if (value := os.environ.get(name))
    }
