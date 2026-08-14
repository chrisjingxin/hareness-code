"""文本 mutation backend adapter contract。

该模块把文件 mutation 的语义从本机 ``Path`` 和 DeepAgents backend 细节中抽出。
尤其是 compare-and-replace 不可证明时，统一返回 ``BACKEND_CAS_UNSUPPORTED``，
绝不把先读后盲写伪装成安全提交。
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

from harness_agent.threads.snapshots import VIRTUAL_READ_ONLY_ROOT


class TextMutationError(RuntimeError):
    """文本 backend 的稳定能力或提交错误。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存机器可判断的 code，消息不包含原文。"""
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    """后端实际字节内容及无损文本元数据的 identity。"""

    digest: str
    byte_length: int
    encoding: str = "utf-8"
    has_bom: bool = False
    line_ending: str = "none"
    has_final_newline: bool = False


@dataclass(frozen=True, slots=True)
class TextDocument:
    """一次完整文本读取结果；提交接口只接受该 identity，不接受宿主 Path。"""

    path: str
    content: str
    identity: ContentIdentity


@runtime_checkable
class TextMutationBackend(Protocol):
    """Local/remote/unsupported backend 共同实现的 canonical contract。"""

    @property
    def backend_id(self) -> str:
        """返回稳定的 backend/route identity。"""

    def read_text_document(self, path: str) -> TextDocument:
        """读取完整文本和内容 identity。"""

    def create_text_document(self, path: str, content: str) -> TextDocument:
        """只在目标不存在时创建文本文件。"""

    def compare_and_replace_text(
        self,
        path: str,
        expected_content_identity: ContentIdentity,
        proposed_content: str,
    ) -> TextDocument:
        """仅当当前 identity 未变化时原子替换并返回实际落盘内容。"""

    def delete_if_unchanged(self, path: str, expected_content_identity: ContentIdentity) -> None:
        """仅当当前 identity 未变化时删除文件。"""


def _line_ending(content: str) -> str:
    """检测文本换行风格，混合换行由 adapter fail closed。"""
    crlf = content.count("\r\n")
    rest = content.replace("\r\n", "")
    lone_cr = rest.count("\r")
    lone_lf = rest.count("\n")
    if sum(bool(value) for value in (crlf, lone_cr, lone_lf)) > 1:
        raise TextMutationError("TEXT_NEWLINE_MIXED", "混合换行文本不能安全 mutation")
    if crlf:
        return "crlf"
    if lone_cr:
        return "cr"
    if lone_lf:
        return "lf"
    return "none"


def _document(path: str, raw: bytes) -> TextDocument:
    """从真实字节生成完整文本 document 和强 identity。"""
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    payload = raw[3:] if has_bom else raw
    # NUL 虽能被 UTF-8 解码，但在文件工具语义中代表二进制内容；不能让它获得
    # Snapshot 后走 exact-string mutation，必须要求专用工具处理。
    if b"\x00" in payload:
        raise TextMutationError("TEXT_ENCODING_UNSUPPORTED", "文件包含二进制 NUL 字节")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TextMutationError("TEXT_ENCODING_UNSUPPORTED", "文件不是可无损处理的 UTF-8 文本") from exc
    return TextDocument(
        path=path,
        content=content,
        identity=ContentIdentity(
            digest=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            encoding="utf-8",
            has_bom=has_bom,
            line_ending=_line_ending(content),
            has_final_newline=content.endswith(("\n", "\r")),
        ),
    )


def _encode_content(content: str, *, expected: ContentIdentity | None = None) -> bytes:
    """按已有 BOM/换行元数据编码拟议文本，拒绝混合换行。"""
    line_ending = _line_ending(content)
    if expected is not None:
        if line_ending == "mixed":  # 防止未来修改检测函数时语义漂移
            raise TextMutationError("TEXT_NEWLINE_MIXED")
        if expected.line_ending == "crlf" and line_ending == "lf":
            content = content.replace("\n", "\r\n")
        elif expected.line_ending == "cr" and line_ending == "lf":
            content = content.replace("\n", "\r")
    has_bom = expected.has_bom if expected is not None else content.startswith("\ufeff")
    if content.startswith("\ufeff"):
        content = content[1:]
    payload = content.encode("utf-8")
    return b"\xef\xbb\xbf" + payload if has_bom else payload


class LocalTextMutationBackend:
    """基于安全虚拟路径、临时文件和同目录 rename 的本机 adapter。"""

    def __init__(
        self,
        root: str | Path,
        *,
        backend_id: str | None = None,
        registry: Any | None = None,
    ) -> None:
        """绑定一个工作区根；外部调用只传 `/` 开头的 backend 路径。

        Args:
            root: 主工作区根。
            backend_id: 可选稳定 identity。
            registry: 可选 ``WorkspaceRootRegistry``；提供时支持 ``/@ext/<id>/`` 路径。
        """
        self._root = Path(root).resolve(strict=False)
        self._backend_id = backend_id or f"local:{self._root}"
        self._registry = registry
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @property
    def backend_id(self) -> str:
        """返回本机工作区 identity。"""
        return self._backend_id

    def read_text_document(self, path: str) -> TextDocument:
        """安全读取完整 UTF-8 文本，不跟随符号链接。"""
        target = self._resolve(path)
        try:
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise TextMutationError("FILE_NOT_FOUND", "文件不存在") from exc
        except IsADirectoryError as exc:
            raise TextMutationError("IS_DIRECTORY", "目标是目录") from exc
        except OSError as exc:
            raise TextMutationError("BACKEND_READ_FAILED", "文件读取失败") from exc
        try:
            with os.fdopen(fd, "rb") as file:
                raw = file.read()
        except OSError as exc:
            raise TextMutationError("BACKEND_READ_FAILED", "文件读取失败") from exc
        return _document(path, raw)

    def create_text_document(self, path: str, content: str) -> TextDocument:
        """以 O_EXCL 创建新文本，已存在时不覆盖。"""
        target = self._resolve(path)
        lock = self._lock_for(path)
        with lock:
            raw = _encode_content(content)
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(target, flags, 0o644)
            except FileExistsError as exc:
                raise TextMutationError("FILE_ALREADY_EXISTS", "目标文件已存在") from exc
            except OSError as exc:
                raise TextMutationError("BACKEND_CREATE_FAILED", "文件创建失败") from exc
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(raw)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError as exc:
                raise TextMutationError("BACKEND_CREATE_FAILED", "文件创建失败") from exc
            return self.read_text_document(path)

    def compare_and_replace_text(
        self,
        path: str,
        expected_content_identity: ContentIdentity,
        proposed_content: str,
    ) -> TextDocument:
        """校验当前 hash 后通过同目录临时文件原子替换。"""
        target = self._resolve(path)
        lock = self._lock_for(path)
        with lock:
            current = self.read_text_document(path)
            self._require_identity(current, expected_content_identity)
            raw = _encode_content(proposed_content, expected=current.identity)
            original_mode = stat.S_IMODE(target.stat(follow_symlinks=False).st_mode)
            fd, temporary_name = tempfile.mkstemp(prefix=".harness-text-", dir=target.parent)
            try:
                os.chmod(temporary_name, original_mode)
                with os.fdopen(fd, "wb") as file:
                    file.write(raw)
                    file.flush()
                    os.fsync(file.fileno())
                if target.is_symlink():
                    raise TextMutationError("PATH_SYMLINK_UNSUPPORTED", "目标不能是符号链接")
                os.replace(temporary_name, target)
            except TextMutationError:
                Path(temporary_name).unlink(missing_ok=True)
                raise
            except OSError as exc:
                Path(temporary_name).unlink(missing_ok=True)
                raise TextMutationError("BACKEND_REPLACE_FAILED", "文件替换失败") from exc
            return self.read_text_document(path)

    def delete_if_unchanged(self, path: str, expected_content_identity: ContentIdentity) -> None:
        """校验 identity 后安全删除文件。"""
        target = self._resolve(path)
        lock = self._lock_for(path)
        with lock:
            current = self.read_text_document(path)
            self._require_identity(current, expected_content_identity)
            try:
                target.unlink()
            except OSError as exc:
                raise TextMutationError("BACKEND_DELETE_FAILED", "文件删除失败") from exc

    def _resolve(self, path: str) -> Path:
        """将 canonical virtual path 映射到 root，并拒绝 symlink/traversal。"""
        if not isinstance(path, str) or not path.startswith("/"):
            raise TextMutationError("PATH_INVALID", "backend 路径必须是绝对虚拟路径")
        normalized = path.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if ".." in parts or "~" in parts:
            raise TextMutationError("PATH_INVALID", "backend 路径不能包含遍历段")
        if normalized == VIRTUAL_READ_ONLY_ROOT or normalized.startswith(f"{VIRTUAL_READ_ONLY_ROOT}/"):
            raise TextMutationError("VIRTUAL_READONLY", "/.harness 是只读虚拟路径")

        root = self._root
        relative_parts = PurePosixPath(normalized.lstrip("/")).parts
        if self._registry is not None and normalized.startswith("/@ext/"):
            from harness_agent.threads.multi_root_backend import split_ext_backend_path

            split = split_ext_backend_path(normalized)
            if split is None:
                raise TextMutationError("PATH_INVALID", "扩展工作区路径无效")
            root_id, inner = split
            workspace_root = self._registry.get_root(root_id)
            if workspace_root is None:
                raise TextMutationError("PATH_OUTSIDE_WORKSPACE", f"未知的扩展工作区根：{root_id}")
            root = workspace_root.path
            relative_parts = PurePosixPath(inner.lstrip("/")).parts
            normalized = inner

        target = (root / PurePosixPath(*relative_parts)).resolve(strict=False) if relative_parts else root.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise TextMutationError("PATH_OUTSIDE_WORKSPACE", "目标路径越过工作区") from exc
        current = root
        for part in relative_parts:
            current /= part
            if current.is_symlink():
                raise TextMutationError("PATH_SYMLINK_UNSUPPORTED", "文件路径不能经过符号链接")
        return target

    def _lock_for(self, path: str) -> threading.RLock:
        """为进程内同路径提交提供最小线性化。"""
        with self._locks_guard:
            return self._locks.setdefault(path, threading.RLock())

    @staticmethod
    def _require_identity(current: TextDocument, expected: ContentIdentity) -> None:
        """比较完整 identity，变化时拒绝提交而不是盲写。"""
        if current.identity != expected:
            raise TextMutationError("COMMIT_CONFLICT", "文件内容已变化，请重新读取")


class RemoteTextMutationBackend:
    """把 provider 原生 text/CAS 能力适配为统一 contract。"""

    def __init__(self, provider: Any, *, backend_id: str) -> None:
        """provider 可是原生 text adapter，也可是只实现 read 的 DeepAgents backend。"""
        if not backend_id:
            raise ValueError("BACKEND_ID_REQUIRED")
        self._provider = provider
        self._backend_id = backend_id

    @property
    def backend_id(self) -> str:
        """返回 provider/route identity。"""
        return self._backend_id

    def read_text_document(self, path: str) -> TextDocument:
        """只接受 provider 原生完整读取，分页 read 不能充当内容 identity。"""
        method = getattr(self._provider, "read_text_document", None)
        if callable(method):
            return _coerce_document(path, method(path))
        raise TextMutationError(
            "BACKEND_TEXT_UNSUPPORTED",
            "远端 backend 未提供可证明完整性的文本读取 contract",
        )

    def create_text_document(self, path: str, content: str) -> TextDocument:
        """只有 provider 明确声明 create contract 时才允许创建。"""
        return self._call_mutation("create_text_document", path, content)

    def compare_and_replace_text(
        self,
        path: str,
        expected_content_identity: ContentIdentity,
        proposed_content: str,
    ) -> TextDocument:
        """只调用 provider 原生 CAS，不把 exact edit 伪装为 CAS。"""
        return self._call_mutation(
            "compare_and_replace_text",
            path,
            expected_content_identity,
            proposed_content,
        )

    def delete_if_unchanged(self, path: str, expected_content_identity: ContentIdentity) -> None:
        """只调用 provider 原生 compare-delete。"""
        method = getattr(self._provider, "delete_if_unchanged", None)
        if not callable(method):
            raise TextMutationError("BACKEND_CAS_UNSUPPORTED", "远端 backend 不支持安全 compare-delete")
        method(path, expected_content_identity)

    def _call_mutation(self, name: str, *args: Any) -> TextDocument:
        """将 provider 缺失或明确不支持统一映射为 CAS 能力错误。"""
        method = getattr(self._provider, name, None)
        if not callable(method):
            raise TextMutationError("BACKEND_CAS_UNSUPPORTED", "远端 backend 未提供安全 CAS contract")
        try:
            return _coerce_document(str(args[0]), method(*args))
        except NotImplementedError as exc:
            raise TextMutationError("BACKEND_CAS_UNSUPPORTED", "远端 backend 未提供安全 CAS contract") from exc


class UnsupportedTextMutationBackend:
    """明确拒绝所有需要 text/CAS 能力的 mutation。"""

    def __init__(self, *, backend_id: str = "unsupported") -> None:
        """固定不可写身份，便于契约测试和错误观测。"""
        self._backend_id = backend_id

    @property
    def backend_id(self) -> str:
        """返回不可写 backend identity。"""
        return self._backend_id

    def read_text_document(self, _path: str) -> TextDocument:
        """不支持读取时也 fail closed。"""
        raise TextMutationError("BACKEND_TEXT_UNSUPPORTED")

    def create_text_document(self, _path: str, _content: str) -> TextDocument:
        """不允许假装通过普通 write 完成创建。"""
        raise TextMutationError("BACKEND_CAS_UNSUPPORTED")

    def compare_and_replace_text(
        self,
        _path: str,
        _expected_content_identity: ContentIdentity,
        _proposed_content: str,
    ) -> TextDocument:
        """不支持 CAS 时统一拒绝。"""
        raise TextMutationError("BACKEND_CAS_UNSUPPORTED")

    def delete_if_unchanged(self, _path: str, _expected_content_identity: ContentIdentity) -> None:
        """不支持 compare-delete 时统一拒绝。"""
        raise TextMutationError("BACKEND_CAS_UNSUPPORTED")


def _coerce_document(path: str, value: Any) -> TextDocument:
    """校验 provider 返回的 document，拒绝模糊字典和缺失 identity。"""
    if isinstance(value, TextDocument):
        return value
    if isinstance(value, Mapping):
        content = value.get("content")
        identity = value.get("identity")
        if isinstance(content, str) and isinstance(identity, ContentIdentity):
            return TextDocument(path=path, content=content, identity=identity)
    raise TextMutationError("BACKEND_DOCUMENT_INVALID")


__all__ = [
    "ContentIdentity",
    "LocalTextMutationBackend",
    "RemoteTextMutationBackend",
    "TextDocument",
    "TextMutationBackend",
    "TextMutationError",
    "UnsupportedTextMutationBackend",
]
