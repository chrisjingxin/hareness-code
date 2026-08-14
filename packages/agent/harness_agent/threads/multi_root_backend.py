"""多根文件 backend：按 ``/@ext/<root-id>/`` 运行时路由到对应工作区根。

路由不能在构图时固定，因为额外根会在会话中途经目录信任审批新增。
本模块按 root_id 懒创建并缓存 ``LocalShellBackend``，主根仍走传入的 default backend。
"""

from __future__ import annotations

import logging
import threading
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.local_shell import LocalShellBackend

from harness_agent.policy.workspace_roots import EXT_PREFIX, WorkspaceRootRegistry

logger = logging.getLogger(__name__)


def split_ext_backend_path(path: str) -> tuple[str, str] | None:
    """若路径为 ``/@ext/<root-id>/...``，返回 ``(root_id, inner_virtual_path)``。"""
    if not isinstance(path, str) or not path.startswith(f"{EXT_PREFIX}/"):
        return None
    rest = path[len(EXT_PREFIX) :].lstrip("/")
    if not rest:
        return None
    parts = PurePosixPath(rest).parts
    root_id = parts[0]
    inner = "/" if len(parts) == 1 else "/" + "/".join(parts[1:])
    return root_id, inner


class ExtRootBackendRouter:
    """把 ``/@ext/<root-id>/`` 前缀路由到按 root 懒创建的 LocalShellBackend。

    非扩展路径委托给 ``default`` backend。接口对齐 DeepAgents BackendProtocol
    的常用方法，以便作为 CompositeBackend 的 default 或独立使用。
    """

    def __init__(
        self,
        default: Any,
        registry: WorkspaceRootRegistry,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        """绑定主 backend 与根注册表。"""
        self._default = default
        self._registry = registry
        self._env = env
        self._cache: dict[str, LocalShellBackend] = {}
        self._lock = threading.Lock()

    def _backend_for(self, path: str) -> tuple[Any, str]:
        """返回处理该路径的 backend 与改写后的 key。"""
        split = split_ext_backend_path(path)
        if split is None:
            return self._default, path
        root_id, inner = split
        root = self._registry.get_root(root_id)
        if root is None:
            raise FileNotFoundError(f"未知的扩展工作区根：{root_id}")
        with self._lock:
            backend = self._cache.get(root_id)
            if backend is None:
                backend = LocalShellBackend(
                    root_dir=root.path,
                    virtual_mode=True,
                    inherit_env=False,
                    env=self._env,
                )
                self._cache[root_id] = backend
        return backend, inner

    def ls(self, path: str) -> Any:
        """列举目录。"""
        backend, key = self._backend_for(path)
        return backend.ls(key)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        """读取文件。"""
        backend, key = self._backend_for(file_path)
        return backend.read(key, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> Any:
        """写入文件。"""
        backend, key = self._backend_for(file_path)
        return backend.write(key, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> Any:
        """编辑文件。"""
        backend, key = self._backend_for(file_path)
        return backend.edit(key, old_string, new_string, replace_all=replace_all)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> Any:
        """搜索文件内容。"""
        if path is None:
            return self._default.grep(pattern, path=path, glob=glob)
        backend, key = self._backend_for(path)
        return backend.grep(pattern, path=key, glob=glob)

    def glob(self, pattern: str, path: str = "/") -> Any:
        """按模式搜索文件。"""
        backend, key = self._backend_for(path)
        return backend.glob(pattern, path=key)

    def execute(self, command: str, *, timeout: int | None = None) -> Any:
        """Shell 执行始终落在主根。"""
        if hasattr(self._default, "execute"):
            if timeout is not None:
                return self._default.execute(command, timeout=timeout)
            return self._default.execute(command)
        raise NotImplementedError("Default backend does not support execute")

    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any:
        """异步 Shell 执行始终落在主根。"""
        if hasattr(self._default, "aexecute"):
            if timeout is not None:
                return await self._default.aexecute(command, timeout=timeout)
            return await self._default.aexecute(command)
        if hasattr(self._default, "execute"):
            if timeout is not None:
                return self._default.execute(command, timeout=timeout)
            return self._default.execute(command)
        raise NotImplementedError("Default backend does not support aexecute")

    def __getattr__(self, name: str) -> Any:
        """其余属性委托给 default backend。"""
        return getattr(self._default, name)


from deepagents.backends.protocol import SandboxBackendProtocol  # noqa: E402

SandboxBackendProtocol.register(ExtRootBackendRouter)

