"""``/.harness`` 只读虚拟文件后端。

虚拟路径是模型可见的逻辑命名空间，Skill 根目录和 SQLite 文件从不暴露给
模型。该模块只实现读取；写入、搜索和列举统一返回拒绝结果。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from harness_agent.extensions.skills import SkillError, SkillRegistry
from harness_agent.diagnostic_log.runtime import ensure_log
from harness_agent.runtime.run_context import require_run_context

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol
    from harness_agent.threads.thread_persistence import ThreadPersistence

VIRTUAL_ROOT = "/.harness"
MAX_VIRTUAL_READ_LINES = 2_000
_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{8,80}$")


class HarnessVirtualBackend:
    """为一个固定 thread 提供 Skill 正文与历史归档的受限只读视图。"""

    def __init__(
        self,
        *,
        registry: SkillRegistry | None,
        thread_id: str,
        thread_persistence: "ThreadPersistence | None" = None,
        expected_snapshot_id: str | None = None,
        require_snapshot_id: bool = False,
        diagnostic_log: Any | None = None,
    ) -> None:
        """绑定一个 Run 的 Skill snapshot 和当前 project/thread 的归档读取器。"""
        self._registry = registry
        self._thread_id = thread_id
        self._thread_persistence = thread_persistence
        self._expected_snapshot_id = expected_snapshot_id
        self._require_snapshot_id = require_snapshot_id
        self._diagnostic_log = ensure_log(diagnostic_log)
        self._history_cache: dict[str, str] = {}

    def read(self, file_path: str, offset: int = 0, limit: int = 2_000) -> ReadResult:
        """同步读取 Skill；SQLite 历史只允许异步工具链读取以免阻塞事件循环。"""
        try:
            path = self._validated_path(file_path)
            skill_identity: tuple[str, str] | None = None
            if path.parts[0] == "skills":
                content, skill_id, kind = self._read_skill(path)
                skill_identity = (skill_id, kind)
            else:
                content = self._history_cache.get(path.stem)
            if content is None:
                return ReadResult(error="history artifact is unavailable in synchronous mode")
            result = self._page(content, offset, limit)
            if skill_identity is not None:
                self._log_skill_read(*skill_identity)
            return result
        except (SkillError, ValueError) as exc:
            return ReadResult(error=str(exc))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2_000) -> ReadResult:
        """异步读取虚拟文件，并只从已绑定 thread 的 SQLite 行恢复历史正文。"""
        try:
            path = self._validated_path(file_path)
            skill_identity: tuple[str, str] | None = None
            if path.parts[0] == "skills":
                content, skill_id, kind = self._read_skill(path)
                skill_identity = (skill_id, kind)
            else:
                artifact_id = path.stem
                content = self._history_cache.get(artifact_id)
                if content is None and self._thread_persistence is not None:
                    artifact = await self._thread_persistence.load_context_artifact(self._thread_id, artifact_id)
                    content = artifact.content if artifact is not None else None
                    if content is not None:
                        self._history_cache[artifact_id] = content
                if content is None:
                    return ReadResult(error="history artifact was not found for the current thread")
            result = self._page(content, offset, limit)
            if skill_identity is not None:
                self._log_skill_read(*skill_identity)
            return result
        except (SkillError, ValueError) as exc:
            return ReadResult(error=str(exc))

    def ls(self, _path: str) -> LsResult:
        """拒绝目录列举，防止模型枚举 Skill 或归档标识。"""
        return LsResult(error="/.harness is read_file-only and cannot be listed")

    async def als(self, path: str) -> LsResult:
        """异步列举入口保持与同步拒绝一致。"""
        return self.ls(path)

    def glob(self, _pattern: str, _path: str | None = None) -> GlobResult:
        """虚拟命名空间不支持 glob 搜索，返回空结果。

        返回空 matches 而非 error，避免 CompositeBackend 在聚合多后端
        搜索结果时将 error 传播为整体失败。
        """
        return GlobResult(matches=[])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """异步搜索入口保持与同步行为一致。"""
        return self.glob(pattern, path)

    def grep(self, _pattern: str, _path: str | None = None, _glob: str | None = None) -> GrepResult:
        """虚拟命名空间不支持全文检索，返回空结果。

        返回空 matches 而非 error，避免 CompositeBackend 在聚合多后端
        搜索结果时将 error 传播为整体失败。
        """
        return GrepResult(matches=[])

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """异步全文检索入口保持与同步行为一致。"""
        return self.grep(pattern, path, glob)

    def write(self, _file_path: str, _content: str) -> WriteResult:
        """拒绝任何虚拟文件写入。"""
        return WriteResult(error="/.harness is read-only")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """异步写入入口保持与同步拒绝一致。"""
        return self.write(file_path, content)

    def edit(self, _file_path: str, _old_string: str, _new_string: str, replace_all: bool = False) -> EditResult:
        """拒绝任何虚拟文件编辑。"""
        return EditResult(error="/.harness is read-only")

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """异步编辑入口保持与同步拒绝一致。"""
        return self.edit(file_path, old_string, new_string, replace_all)

    def execute(self, _command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """拒绝从虚拟后端执行命令；CompositeBackend 实际也会路由 execute 到默认后端。"""
        return ExecuteResponse(output="/.harness cannot be executed", exit_code=1, truncated=False)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步命令入口保持与同步拒绝一致。"""
        return self.execute(command, timeout=timeout)

    def _validated_path(self, value: str) -> PurePosixPath:
        """解析公开或 Composite 剥离后的路径，并拒绝路径穿越和未知格式。"""
        if not isinstance(value, str) or not value:
            raise ValueError("virtual path must be a non-empty string")
        normalized = value.replace("\\", "/")
        if normalized.startswith(f"{VIRTUAL_ROOT}/"):
            normalized = normalized[len(VIRTUAL_ROOT) + 1 :]
        elif normalized.startswith("/"):
            normalized = normalized[1:]
        path = PurePosixPath(normalized)
        if not normalized or ".." in path.parts or "." in path.parts:
            raise ValueError("virtual path must not contain traversal segments")
        if not path.parts or path.parts[0] not in {"skills", "history"}:
            raise ValueError("unknown /.harness virtual path")
        if path.parts[0] == "history":
            if len(path.parts) != 2 or not path.name.endswith(".md") or not _ARTIFACT_ID.fullmatch(path.stem):
                raise ValueError("history path must be /.harness/history/<artifact-id>.md")
        elif len(path.parts) < 3:
            raise ValueError("skill path must include a canonical skill ID and file name")
        return path

    def _read_skill(self, path: PurePosixPath) -> tuple[str, str, str]:
        """从当前 Run 的 Skill snapshot 安全读取正文或资源，绝不泄露根目录。"""
        if self._registry is None:
            raise SkillError("RunContext Skill snapshot is unavailable")
        if self._require_snapshot_id and self._expected_snapshot_id is None:
            raise SkillError("RunContext Skill snapshot identity is unavailable")
        if (
            self._expected_snapshot_id is not None
            and self._registry.snapshot_id != self._expected_snapshot_id
        ):
            raise SkillError("RunContext Skill snapshot identity mismatch")
        if path.name == "SKILL.md":
            skill_id = "/".join(path.parts[1:-1])
            virtual_id = self._registry.resolve_virtual_id(skill_id)
            return self._registry.load(virtual_id).body, virtual_id, "body"
        # canonical ID 可以包含多个路径段。按最长前缀解析，避免 Plugin ID 被固定两段截断。
        for boundary in range(len(path.parts) - 1, 1, -1):
            skill_id = "/".join(path.parts[1:boundary])
            try:
                virtual_id = self._registry.resolve_virtual_id(skill_id)
            except SkillError:
                continue
            relative = "/".join(path.parts[boundary:])
            if not relative:
                raise ValueError("skill resource path is required")
            return self._registry.read_resource(virtual_id, relative), virtual_id, "resource"
        raise ValueError("unknown canonical Skill ID")

    def _log_skill_read(self, skill_id: str, kind: str) -> None:
        """成功完成分页后只记录 Skill 身份与读取类型，不记录正文或路径。"""
        self._diagnostic_log.info(
            "skill.read",
            {"skill_id": skill_id, "kind": kind},
        )

    @staticmethod
    def _page(content: str, offset: int, limit: int) -> ReadResult:
        """按原始行分页返回文件数据，限制读取上限而不向模型暴露存储实现。"""
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1 or limit > MAX_VIRTUAL_READ_LINES:
            raise ValueError(f"limit must be between 1 and {MAX_VIRTUAL_READ_LINES}")
        lines = content.splitlines(keepends=True)
        return ReadResult(file_data={"content": "".join(lines[offset : offset + limit]), "encoding": "utf-8"})


def mount_harness_virtual_files(
    default_backend: "BackendProtocol",
    *,
    registry: SkillRegistry | None,
    thread_id: str,
    thread_persistence: "ThreadPersistence | None" = None,
    expected_snapshot_id: str | None = None,
    require_snapshot_id: bool = False,
    diagnostic_log: Any | None = None,
) -> CompositeBackend:
    """把虚拟只读后端挂在真实 backend 之前，文件工具仍使用统一 ``read_file``。"""
    return CompositeBackend(
        default=default_backend,
        routes={
            f"{VIRTUAL_ROOT}/": HarnessVirtualBackend(
                registry=registry,
                thread_id=thread_id,
                thread_persistence=thread_persistence,
                expected_snapshot_id=expected_snapshot_id,
                require_snapshot_id=require_snapshot_id,
                diagnostic_log=diagnostic_log,
            )
        },
    )


def run_scoped_virtual_backend_factory(
    default_backend: "BackendProtocol",
    *,
    thread_persistence: "ThreadPersistence | None" = None,
) -> Callable[[Any], CompositeBackend]:
    """返回按当前 RunContext 解析虚拟历史的 backend factory。

    编译图可以安全共享 ``default_backend``，但 Skill 和 ``/.harness/history``
    必须以当前 RunContext 为边界。RunContext 缺失或没有 Skill identity 时
    直接拒绝 Skill 读取，避免共享图闭包捕获构图期旧 Registry。
    """

    def backend_for_run(runtime: Any) -> CompositeBackend:
        """在工具执行边界创建仅绑定当前 thread 的虚拟挂载。"""
        context = require_run_context(runtime)
        snapshot = context.context_snapshot
        return mount_harness_virtual_files(
            default_backend,
            registry=context.skill_registry,
            thread_id=context.thread_id,
            thread_persistence=thread_persistence,
            expected_snapshot_id=(
                snapshot.skill_snapshot_id if snapshot is not None else None
            ),
            require_snapshot_id=True,
            diagnostic_log=context.diagnostic_log,
        )

    return backend_for_run
