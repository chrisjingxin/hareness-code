"""Compose Workspace Markdown 文档的安全读写与 front matter 契约。

Compose 的任务、规格、计划、清单和报告以工作空间中的 Markdown 为唯一正文
事实。本模块只固定目录、文件名、front matter 与 compare-and-replace 语义；
SQLite 侧仅由 Work Item store 保存路径、摘要和人工确认，不复制正文。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from harness_agent.compose.models import ComposeDocumentKind
from harness_agent.compose.document_paths import (
    DEFAULT_COMPOSE_DOCS_DIR,
    ComposeDocumentPathError,
    compose_document_relative_path,
    normalize_compose_docs_dir,
)
from harness_agent.threads.text_backend import (
    ContentIdentity,
    LocalTextMutationBackend,
    TextDocument,
    TextMutationBackend,
    TextMutationError,
)

MAX_COMPOSE_DOCUMENT_BYTES = 256 * 1024
"""单份 Compose Markdown 的 UTF-8 最大字节数，避免正文进入无界持久路径。"""

_FRONT_MATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<header>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(?P<body>.*)\Z",
    re.DOTALL,
)
_WORK_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_STATUS = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class ComposeDocumentStoreError(RuntimeError):
    """Compose Markdown store 的稳定错误码；不向上层泄露后端路径细节。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存可分支的错误码及可选的简短诊断。"""
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


@dataclass(frozen=True, slots=True)
class ComposeDocumentSnapshot:
    """一份已校验 Markdown 的当前 Workspace 事实和内容 identity。"""

    work_item_id: str
    slug: str
    kind: ComposeDocumentKind
    relative_path: str
    revision: int
    status: str
    updated_at_ms: int
    content: str
    identity: ContentIdentity

    @property
    def digest(self) -> str:
        """返回按真实落盘字节计算的 SHA-256 内容摘要。"""
        return self.identity.digest


@dataclass(frozen=True, slots=True)
class DocumentCommit:
    """一次新建或 CAS 更新 Markdown 文档的不可变命令。"""

    work_item_id: str
    slug: str
    kind: ComposeDocumentKind
    content: str
    expected: ComposeDocumentSnapshot | None = None


class ComposeDocumentStore:
    """将固定 Work Item 文档安全映射到 Workspace，并保持正文单一事实源。"""

    def __init__(
        self,
        workspace: Path | str,
        *,
        docs_dir: str = DEFAULT_COMPOSE_DOCS_DIR,
        backend: TextMutationBackend | None = None,
    ) -> None:
        """固定 workspace、文档根和可验证文本 CAS backend。"""
        self._workspace = Path(workspace).expanduser().resolve(strict=False)
        try:
            self._docs_dir = normalize_compose_docs_dir(docs_dir)
        except ComposeDocumentPathError as exc:
            raise ComposeDocumentStoreError("COMPOSE_DOCS_DIR_INVALID") from exc
        self._backend = backend or LocalTextMutationBackend(self._workspace)

    @property
    def docs_dir(self) -> str:
        """返回规范化后的工作空间相对文档根目录。"""
        return self._docs_dir

    async def inspect(
        self,
        work_item_id: str,
        slug: str,
        kind: ComposeDocumentKind,
    ) -> ComposeDocumentSnapshot | None:
        """读取并校验一份固定文档；文件缺失返回 ``None``，畸形内容 fail closed。"""
        relative_path, virtual_path = self._paths(work_item_id, slug, kind)
        document = await self._read_optional(virtual_path)
        if document is None:
            return None
        return _snapshot_from_document(
            document,
            work_item_id=work_item_id,
            slug=slug,
            kind=kind,
            relative_path=relative_path,
        )

    async def commit(self, command: DocumentCommit) -> ComposeDocumentSnapshot:
        """创建或以 inspect Snapshot CAS 更新一份 Markdown，拒绝覆盖外部编辑。"""
        relative_path, virtual_path = self._paths(
            command.work_item_id,
            command.slug,
            command.kind,
        )
        _validate_document_content(
            command.content,
            work_item_id=command.work_item_id,
            kind=command.kind,
        )
        current = await self._read_optional(virtual_path)
        try:
            if current is None:
                if command.expected is not None:
                    raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_CONFLICT")
                written = await asyncio.to_thread(
                    self._backend.create_text_document,
                    virtual_path,
                    command.content,
                )
            else:
                expected = command.expected
                if expected is None:
                    raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_EXISTS")
                # 先把磁盘上的当前版本重新解析为合法 Snapshot。调用方传入的
                # Snapshot 属于边界输入，不能仅凭可伪造的 identity 覆盖畸形文档。
                _snapshot_from_document(
                    current,
                    work_item_id=command.work_item_id,
                    slug=command.slug,
                    kind=command.kind,
                    relative_path=relative_path,
                )
                if (
                    expected.relative_path != relative_path
                    or expected.work_item_id != command.work_item_id
                    or expected.slug != command.slug
                    or expected.kind is not command.kind
                    or expected.identity != current.identity
                ):
                    raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_CONFLICT")
                written = await asyncio.to_thread(
                    self._backend.compare_and_replace_text,
                    virtual_path,
                    expected.identity,
                    command.content,
                )
        except ComposeDocumentStoreError:
            raise
        except TextMutationError as exc:
            raise _document_backend_error(exc) from exc
        return _snapshot_from_document(
            written,
            work_item_id=command.work_item_id,
            slug=command.slug,
            kind=command.kind,
            relative_path=relative_path,
        )

    def _paths(
        self,
        work_item_id: str,
        slug: str,
        kind: ComposeDocumentKind,
    ) -> tuple[str, str]:
        """由受信 Work Item identity 生成唯一相对/虚拟文件路径。"""
        if (
            not isinstance(work_item_id, str)
            or _WORK_ITEM_ID.fullmatch(work_item_id) is None
        ):
            raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_PATH_INVALID")
        try:
            relative_path = compose_document_relative_path(self._docs_dir, slug, kind)
        except ComposeDocumentPathError as exc:
            raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_PATH_INVALID") from exc
        return relative_path, f"/{relative_path}"

    async def _read_optional(self, virtual_path: str) -> TextDocument | None:
        """从 backend 读取完整文本；只有确切缺失才可作为尚未创建处理。"""
        try:
            return await asyncio.to_thread(self._backend.read_text_document, virtual_path)
        except TextMutationError as exc:
            if exc.code == "FILE_NOT_FOUND":
                return None
            raise _document_backend_error(exc) from exc


def _snapshot_from_document(
    document: TextDocument,
    *,
    work_item_id: str,
    slug: str,
    kind: ComposeDocumentKind,
    relative_path: str,
) -> ComposeDocumentSnapshot:
    """验证 front matter 与固定路径一致后投影为可用于 CAS 的 Snapshot。"""
    header = _validate_document_content(
        document.content,
        work_item_id=work_item_id,
        kind=kind,
    )
    return ComposeDocumentSnapshot(
        work_item_id=work_item_id,
        slug=slug,
        kind=kind,
        relative_path=relative_path,
        revision=int(header["revision"]),
        status=str(header["status"]),
        updated_at_ms=int(header["updated_at"]),
        content=document.content,
        identity=document.identity,
    )


def _validate_document_content(
    content: str,
    *,
    work_item_id: str,
    kind: ComposeDocumentKind,
) -> Mapping[str, Any]:
    """解析受限 YAML front matter，确保固定 identity 不会被正文伪造。"""
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_COMPOSE_DOCUMENT_BYTES:
        raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_FRONT_MATTER_INVALID")
    matched = _FRONT_MATTER.fullmatch(content)
    if matched is None or not matched.group("body").strip():
        raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_FRONT_MATTER_INVALID")
    try:
        header = yaml.safe_load(matched.group("header"))
    except yaml.YAMLError as exc:
        raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_FRONT_MATTER_INVALID") from exc
    if not isinstance(header, Mapping):
        raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_FRONT_MATTER_INVALID")
    actual_work_item_id = header.get("work_item_id")
    actual_kind = header.get("kind")
    revision = header.get("revision")
    status = header.get("status")
    updated_at = header.get("updated_at")
    if (
        actual_work_item_id != work_item_id
        or actual_kind != kind.value
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(status, str)
        or _STATUS.fullmatch(status) is None
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
        or updated_at <= 0
    ):
        raise ComposeDocumentStoreError("COMPOSE_DOCUMENT_FRONT_MATTER_INVALID")
    return header


def _document_backend_error(exc: TextMutationError) -> ComposeDocumentStoreError:
    """将底层文件能力错误收敛为 Compose 可恢复的稳定错误码。"""
    if exc.code in {"COMMIT_CONFLICT", "FILE_ALREADY_EXISTS"}:
        return ComposeDocumentStoreError("COMPOSE_DOCUMENT_CONFLICT")
    if exc.code in {"PATH_INVALID", "PATH_OUTSIDE_WORKSPACE", "PATH_SYMLINK_UNSUPPORTED"}:
        return ComposeDocumentStoreError("COMPOSE_DOCUMENT_PATH_INVALID")
    return ComposeDocumentStoreError("COMPOSE_DOCUMENT_IO_FAILED")
