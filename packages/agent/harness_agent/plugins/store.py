"""Plugin 安全 staging、内容寻址只读 store 与版本化 registry。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from harness_agent.plugins.model import (
    InstalledPlugin,
    PluginComponentReport,
    PluginError,
    PluginActivation,
)


MAX_SOURCE_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_FILES = 2_048
MAX_PACKAGE_DEPTH = 24
MAX_RELATIVE_PATH_BYTES = 512
MAX_ZIP_COMPRESSION_RATIO = 200
REGISTRY_VERSION = 3
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 目录安装可以来自用户的 Git checkout；这些名称只依据目录项本身判断，
# 不读取内容，避免把凭据、VCS 元数据或操作系统临时文件带入不可变 store。
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".bzr",
        "cvs",
        "rcs",
        "sccs",
        "__macosx",
        ".spotlight-v100",
        ".trashes",
        ".fseventsd",
    }
)
_EXCLUDED_FILE_NAMES = frozenset(
    {
        ".git",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".hgignore",
        ".hgsub",
        ".svnignore",
        ".env",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        ".pypirc",
        ".netrc",
        ".ds_store",
        "thumbs.db",
        "desktop.ini",
    }
)
_EXCLUDED_FILE_PREFIXES = (".env.", "._")


@dataclass(frozen=True, slots=True)
class StagedPlugin:
    """一个已复制到私有临时目录且通过文件系统约束的 Plugin 来源。"""

    root: Path
    source_id: str
    source_label: str
    name_hint: str
    package_digest: str
    origin: str


@dataclass(frozen=True, slots=True)
class PluginRegistryState:
    """registry 文件的内存快照。"""

    revision: int
    plugins: tuple[InstalledPlugin, ...]
    name_conflicts: tuple[str, ...] = ()


class PluginStore:
    """管理 user scope Plugin 的 staging、store、data 与 registry。"""

    def __init__(self, *, home: Path | None = None) -> None:
        """绑定用户级根目录；不会扫描 workspace 或外部市场。"""
        self.home = (home or Path.home()).expanduser().resolve()
        self.root = self.home / ".harness" / "plugins"
        self.store_root = self.root / "store"
        self.data_root = self.root / "data"
        self.staging_root = self.root / "staging"
        self.registry_path = self.root / "registry.json"
        self.lock_path = self.root / "registry.lock"

    @contextmanager
    def stage(self, source: Path | str) -> Iterator[StagedPlugin]:
        """把本地目录或 zip 复制到 staging，并在返回前完成结构安全检查。"""
        unresolved_source = Path(source).expanduser()
        if unresolved_source.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 来源不能是符号链接")
        try:
            source_path = unresolved_source.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise PluginError("PLUGIN_SOURCE_NOT_FOUND", "Plugin 来源不存在或无法读取") from exc
        if not source_path.is_dir() and not source_path.is_file():
            raise PluginError("PLUGIN_SOURCE_TYPE_INVALID", "Plugin 来源必须是目录或 zip 文件")
        if source_path.is_file() and source_path.suffix.lower() != ".zip":
            raise PluginError("PLUGIN_SOURCE_TYPE_INVALID", "Plugin 文件来源目前只支持 zip")

        self.staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="plugin-", dir=self.staging_root) as temporary:
            staging = Path(temporary)
            payload = staging / "payload"
            payload.mkdir(mode=0o700)
            if source_path.is_dir():
                _copy_directory_secure(source_path, payload)
                plugin_root = payload
                name_hint = source_path.name
            else:
                plugin_root, name_hint = _extract_zip_secure(source_path, payload)
            digest = package_digest(plugin_root)
            source_id = "local-" + hashlib.sha256(
                f"{'directory' if source_path.is_dir() else 'zip'}:{source_path}".encode("utf-8")
            ).hexdigest()[:16]
            yield StagedPlugin(
                root=plugin_root,
                source_id=source_id,
                source_label=source_path.name,
                name_hint=name_hint,
                package_digest=digest,
                origin=str(source_path),
            )

    def install_package(
        self,
        staged: StagedPlugin,
        *,
        plugin_name: str,
    ) -> Path:
        """把 staging 内容复制到内容寻址路径，并将完成版本收紧为只读。"""
        _require_safe_segment(staged.source_id, "source_id")
        _require_safe_segment(plugin_name, "plugin_name")
        _require_digest(staged.package_digest)
        destination = (
            self.store_root / staged.source_id / plugin_name / staged.package_digest
        )
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise PluginError("PLUGIN_STORE_CORRUPT", "Plugin store 目标不是普通目录")
            if package_digest(destination) != staged.package_digest:
                raise PluginError("PLUGIN_STORE_CORRUPT", "Plugin store 内容与 digest 不一致")
            return destination

        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        package = Path(
            tempfile.mkdtemp(prefix=f".{staged.package_digest[:12]}-", dir=destination.parent)
        )
        try:
            shutil.copytree(
                staged.root,
                package,
                dirs_exist_ok=True,
                copy_function=shutil.copyfile,
            )
            _apply_source_modes(staged.root, package)
            if package_digest(package) != staged.package_digest:
                raise PluginError("PLUGIN_STORE_COPY_FAILED", "Plugin store 复制后 digest 不一致")
            # 根目录保持 0700 直到原子 rename 完成；macOS 会拒绝移动自身为 0555 的目录。
            _make_tree_read_only(package, include_root=False)
            try:
                os.replace(package, destination)
            except OSError:
                if not destination.is_dir() or package_digest(destination) != staged.package_digest:
                    raise PluginError("PLUGIN_STORE_CORRUPT", "并发安装产生冲突内容")
            else:
                destination.chmod(0o555)
            return destination
        except OSError as exc:
            raise PluginError("PLUGIN_STORE_WRITE_FAILED", "无法写入 Plugin store") from exc
        finally:
            if package.exists():
                _make_tree_writable(package)
                shutil.rmtree(package, ignore_errors=True)

    def verify_installed(self, plugin: InstalledPlugin) -> None:
        """启用前复核 store 内容，防止只读权限被宿主外部绕过后静默生效。"""
        path = self.package_path(plugin)
        if path.is_symlink() or not path.is_dir():
            raise PluginError("PLUGIN_STORE_MISSING", "Plugin 安装内容不存在")
        if package_digest(path) != plugin.package_digest:
            raise PluginError("PLUGIN_STORE_TAMPERED", "Plugin 安装内容完整性校验失败")

    def package_path(self, plugin: InstalledPlugin) -> Path:
        """由已校验 registry 字段确定 store 路径。"""
        _require_safe_segment(plugin.source_id, "source_id")
        _require_safe_segment(plugin.name, "plugin_name")
        _require_digest(plugin.package_digest)
        return self.store_root / plugin.source_id / plugin.name / plugin.package_digest

    def data_path(self, plugin: InstalledPlugin) -> Path:
        """返回持久数据目录；调用方显式写入时才会创建。"""
        _require_safe_segment(plugin.source_id, "source_id")
        _require_safe_segment(plugin.name, "plugin_name")
        return self.data_root / plugin.source_id / plugin.name

    def purge_data(self, plugin: InstalledPlugin) -> bool:
        """显式清除一个 Plugin 的持久数据；默认 remove 不调用。"""
        path = self.data_path(plugin)
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink():
            raise PluginError("PLUGIN_DATA_CORRUPT", "Plugin data 路径不能是符号链接")
        _make_tree_writable(path)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise PluginError("PLUGIN_DATA_REMOVE_FAILED", "无法删除 Plugin data") from exc
        return True

    def read_registry(self) -> PluginRegistryState:
        """读取 registry；v2 首次读取在同一锁内原子迁移到 v3。"""
        content = self._read_registry_bytes_unlocked()
        if content is None:
            return PluginRegistryState(revision=0, plugins=())
        document = _decode_json_document(content)
        if isinstance(document, dict) and document.get("version") == 2:
            with self._exclusive_lock():
                current = self._read_registry_bytes_unlocked()
                if current is None:
                    return PluginRegistryState(revision=0, plugins=())
                latest = _decode_json_document(current)
                if isinstance(latest, dict) and latest.get("version") == 2:
                    return self._migrate_v2_locked(current, latest)
                return self._registry_state_from_document(latest)
        return self._registry_state_from_document(document)

    def mutate_registry(
        self,
        operation: Callable[[PluginRegistryState], tuple[InstalledPlugin, ...]],
    ) -> PluginRegistryState:
        """在跨进程文件锁内重读、修改并原子替换 registry。"""
        with self._exclusive_lock():
            current = self._read_registry_unlocked()
            plugins = tuple(sorted(operation(current), key=lambda item: item.plugin_id))
            ids = [plugin.plugin_id for plugin in plugins]
            if len(ids) != len(set(ids)):
                raise PluginError("PLUGIN_REGISTRY_INVALID", "Plugin registry 包含重复 ID")
            state = PluginRegistryState(
                revision=current.revision + 1,
                plugins=plugins,
                name_conflicts=_name_conflicts(plugins),
            )
            self._write_registry_atomic(state)
            return state

    def mutate_registry_if_changed(
        self,
        operation: Callable[[PluginRegistryState], tuple[InstalledPlugin, ...]],
    ) -> PluginRegistryState:
        """在同一跨进程锁内只提交真正变化的 registry。"""
        with self._exclusive_lock():
            current = self._read_registry_unlocked()
            plugins = tuple(sorted(operation(current), key=lambda item: item.plugin_id))
            ids = [plugin.plugin_id for plugin in plugins]
            if len(ids) != len(set(ids)):
                raise PluginError("PLUGIN_REGISTRY_INVALID", "Plugin registry 包含重复 ID")
            if plugins == current.plugins:
                return current
            state = PluginRegistryState(
                revision=current.revision + 1,
                plugins=plugins,
                name_conflicts=_name_conflicts(plugins),
            )
            self._write_registry_atomic(state)
            return state

    def _read_registry_unlocked(self) -> PluginRegistryState:
        """锁内读取；v2 迁移只由本方法的持锁调用路径触发。"""
        content = self._read_registry_bytes_unlocked()
        if content is None:
            return PluginRegistryState(revision=0, plugins=())
        document = _decode_json_document(content)
        if isinstance(document, dict) and document.get("version") == 2:
            return self._migrate_v2_locked(content, document)
        return self._registry_state_from_document(document)

    def _registry_state_from_document(self, document: object) -> PluginRegistryState:
        """解码当前 v3 文档。"""
        try:
            return _registry_state_from_document(document, expected_version=REGISTRY_VERSION)
        except (KeyError, TypeError, ValueError, PluginError) as exc:
            if isinstance(exc, PluginError):
                raise
            raise PluginError("PLUGIN_REGISTRY_INVALID", "Plugin registry 版本或结构无效") from exc

    def _migrate_v2_locked(
        self,
        content: bytes,
        document: dict[str, Any],
    ) -> PluginRegistryState:
        """在 registry lock 内完成 v2 严格解码、备份和 v3 原子替换。"""
        legacy = _registry_state_from_document(document, expected_version=2)
        for plugin in legacy.plugins:
            self.verify_installed(plugin)
        migrated_plugins = tuple(
            _migrate_v2_plugin(plugin)
            for plugin in legacy.plugins
        )
        name_conflicts = _name_conflicts(migrated_plugins)
        self._write_v2_backup_atomic(content)
        if name_conflicts:
            # 冲突数据必须保留在 v2 原文中，等待用户通过高级诊断修复；
            # backup 已先落盘，故重试不会丢失任何 artifact 记录。
            raise PluginError(
                "PLUGIN_NAME_CONFLICT",
                "Plugin 名称大小写不敏感地发生冲突",
            )
        reparsed_plugins = tuple(
            self._reparse_migrated_plugin(plugin)
            for plugin in migrated_plugins
        )
        migrated = PluginRegistryState(
            revision=legacy.revision,
            plugins=tuple(sorted(reparsed_plugins, key=lambda item: item.plugin_id)),
            name_conflicts=(),
        )
        self._write_registry_atomic(migrated, rollback_content=content)
        return migrated

    def _reparse_migrated_plugin(self, plugin: InstalledPlugin) -> InstalledPlugin:
        """用当前 Adapter 重建 v2 记录的组件事实，不恢复旧授权状态。"""
        # 延迟导入避免 Store 与 Adapter 在模块初始化时形成循环依赖。
        from harness_agent.plugins.adapters import load_plugin_descriptor

        requested_format = (
            plugin.format
            if plugin.format in {"agent-plugins-1.0", "claude-code", "qwen-code"}
            else "auto"
        )
        descriptor = load_plugin_descriptor(
            self.package_path(plugin),
            package_digest=plugin.package_digest,
            name_hint=plugin.name,
            requested_format=requested_format,  # type: ignore[arg-type]
        )
        if (
            descriptor.name.casefold() != plugin.name.casefold()
            or descriptor.format != plugin.format
            or descriptor.package_digest != plugin.package_digest
        ):
            raise PluginError(
                "PLUGIN_ADAPTER_IDENTITY_MISMATCH",
                "当前 Adapter 返回的 Plugin identity 无法绑定已安装 package",
            )
        return replace(
            plugin,
            version=descriptor.version,
            description=descriptor.description,
            manifest=descriptor.manifest,
            components=descriptor.components,
            diagnostics=descriptor.diagnostics,
            adapter_revision=descriptor.adapter_revision,
        )

    def _write_v2_backup_atomic(self, content: bytes) -> None:
        """持久化一次性 v2 原文备份；已有相同备份时保持幂等。"""
        backup = self.root / "registry.v2.backup.json"
        try:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            if backup.exists():
                if backup.read_bytes() != content:
                    raise PluginError(
                        "PLUGIN_REGISTRY_MIGRATION_BACKUP_CONFLICT",
                        "Plugin registry v2 backup 与当前来源不一致",
                    )
                return
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".registry-v2-backup.", suffix=".tmp", dir=self.root
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, backup)
                _fsync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)
        except PluginError:
            raise
        except OSError as exc:
            raise PluginError(
                "PLUGIN_REGISTRY_MIGRATION_BACKUP_FAILED",
                "无法写入 Plugin registry v2 backup",
            ) from exc

    def _read_registry_bytes_unlocked(self) -> bytes | None:
        """读取 registry 原始字节；不存在时返回 None，其他错误 fail closed。"""
        try:
            content = self.registry_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PluginError("PLUGIN_REGISTRY_READ_FAILED", "无法读取 Plugin registry") from exc
        return content

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """使用 registry.lock 串行化跨进程 Plugin 状态更新。"""
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - 与配置写服务保持同一平台边界。
            raise PluginError("PLUGIN_LOCK_UNAVAILABLE", "当前平台不支持安全 Plugin 锁") from exc
        try:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise PluginError("PLUGIN_LOCK_UNAVAILABLE", "无法创建 Plugin registry 锁") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _write_registry_atomic(
        self,
        state: PluginRegistryState,
        *,
        rollback_content: bytes | None = None,
    ) -> None:
        """fsync 临时文件后替换 registry，并显式区分提交不确定状态。

        replace 之前的失败不会触碰旧文件；replace 成功后目录 fsync 失败时，
        POSIX 无法证明 rename 是否已持久化，因此先尽力恢复旧 bytes，再返回
        ``PLUGIN_REGISTRY_COMMIT_UNCERTAIN``，不能伪装成普通写失败。
        """
        try:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise PluginError("PLUGIN_REGISTRY_WRITE_FAILED", "无法创建 Plugin registry 目录") from exc
        document = {
            "version": REGISTRY_VERSION,
            "revision": state.revision,
            "plugins": [plugin.to_record() for plugin in state.plugins],
        }
        serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        content = serialized.encode("utf-8")
        previous_content = rollback_content
        if previous_content is None:
            try:
                previous_content = self.registry_path.read_bytes()
            except FileNotFoundError:
                pass
            except OSError:
                # 后续 replace/fsync 仍会给出 commit-uncertain；这里不让一次
                # best-effort 读取把真实写路径改成另一个异常语义。
                previous_content = None
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".registry.", suffix=".tmp", dir=self.root
            )
            temporary = Path(temporary_name)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary, self.registry_path)
            except OSError as exc:
                try:
                    current_content = self.registry_path.read_bytes()
                except FileNotFoundError:
                    current_content = None
                except OSError as read_exc:
                    raise PluginError(
                        "PLUGIN_REGISTRY_COMMIT_UNCERTAIN",
                        "Plugin registry replace 状态无法确认",
                    ) from read_exc
                if current_content == content:
                    self._restore_registry_bytes(previous_content)
                    raise PluginError(
                        "PLUGIN_REGISTRY_COMMIT_UNCERTAIN",
                        "Plugin registry replace 已发生但提交状态不确定",
                    ) from exc
                if current_content is None and previous_content is None:
                    raise
                if previous_content is not None and current_content == previous_content:
                    raise
                self._restore_registry_bytes(previous_content)
                raise PluginError(
                    "PLUGIN_REGISTRY_COMMIT_UNCERTAIN",
                    "Plugin registry replace 状态无法确认",
                ) from exc
            try:
                _fsync_directory(self.root)
            except OSError as exc:
                self._restore_registry_bytes(previous_content)
                raise PluginError(
                    "PLUGIN_REGISTRY_COMMIT_UNCERTAIN",
                    "Plugin registry 已替换但目录 fsync 失败，提交状态不确定",
                ) from exc
        except OSError as exc:
            raise PluginError("PLUGIN_REGISTRY_WRITE_FAILED", "无法原子写入 Plugin registry") from exc
        except PluginError:
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _restore_registry_bytes(self, content: bytes | None) -> bool:
        """在 commit-uncertain 后尽力恢复旧文件；返回路径恢复是否成功。"""
        if content is None:
            return False
        descriptor: int | None = None
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".registry-restore.", suffix=".tmp", dir=self.root
            )
            temporary = Path(temporary_name)
            handle = os.fdopen(descriptor, "wb")
            descriptor = None
            with handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.registry_path)
            try:
                _fsync_directory(self.root)
            except OSError:
                # 路径内容已恢复，但目录持久化仍不确定；上层必须继续返回
                # commit-uncertain，不能把这个 best-effort 当成 durability 保证。
                pass
            return True
        except OSError:
            return False
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    """把同目录 rename 刷到磁盘；Windows 依赖文件系统日志语义。"""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _registry_state_from_document(
    document: object,
    *,
    expected_version: int,
) -> PluginRegistryState:
    """严格解码指定版本 registry，不因旧数据放宽正常读取门禁。"""
    if (
        not isinstance(document, dict)
        or document.get("version") != expected_version
        or not isinstance(document.get("revision"), int)
        or isinstance(document.get("revision"), bool)
        or document["revision"] < 0
        or not isinstance(document.get("plugins"), list)
    ):
        raise ValueError("registry version or structure is invalid")
    decoder = _installed_from_record_v2 if expected_version == 2 else _installed_from_record
    plugins = tuple(decoder(item) for item in document["plugins"])
    _ensure_unique_plugin_ids(plugins)
    return PluginRegistryState(
        revision=document["revision"],
        plugins=plugins,
        name_conflicts=_name_conflicts(plugins),
    )


def _ensure_unique_plugin_ids(plugins: tuple[InstalledPlugin, ...]) -> None:
    """拒绝重复身份，防止并发写入隐式覆盖记录。"""
    if len({plugin.plugin_id for plugin in plugins}) != len(plugins):
        raise PluginError("PLUGIN_REGISTRY_INVALID", "Plugin registry 包含重复 ID")


def package_digest(root: Path) -> str:
    """按相对路径、可执行位和内容计算确定性 package digest。"""
    digest = hashlib.sha256()
    count = 0
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 包不能包含符号链接")
        relative = path.relative_to(root).as_posix()
        relative_bytes = relative.encode("utf-8")
        if len(relative_bytes) > MAX_RELATIVE_PATH_BYTES:
            raise PluginError("PLUGIN_PATH_TOO_LONG", "Plugin 相对路径超过上限")
        depth = len(PurePosixPath(relative).parts)
        if depth > MAX_PACKAGE_DEPTH:
            raise PluginError("PLUGIN_PATH_TOO_DEEP", "Plugin 目录层级超过上限")
        mode = path.stat(follow_symlinks=False).st_mode
        if path.is_dir():
            digest.update(b"D\0" + relative_bytes + b"\0")
            continue
        if not path.is_file() or not stat.S_ISREG(mode):
            raise PluginError("PLUGIN_SPECIAL_FILE_REJECTED", "Plugin 只能包含普通文件和目录")
        count += 1
        if count > MAX_PACKAGE_FILES:
            raise PluginError("PLUGIN_FILE_COUNT_EXCEEDED", "Plugin 文件数量超过上限")
        size = path.stat(follow_symlinks=False).st_size
        if size > MAX_PACKAGE_FILE_BYTES:
            raise PluginError("PLUGIN_FILE_TOO_LARGE", "Plugin 单文件超过大小上限")
        total += size
        if total > MAX_PACKAGE_BYTES:
            raise PluginError("PLUGIN_PACKAGE_TOO_LARGE", "Plugin 总大小超过上限")
        executable = b"1" if mode & 0o111 else b"0"
        digest.update(b"F\0" + relative_bytes + b"\0" + executable + b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def plugin_workspace_binding_digest(workspace: Path | str, *, strict: bool = False) -> str:
    """按 Plugin 专用 domain 对 canonical workspace identity 做本地 hash。

    ``strict`` 只用于显式 workspace 管理 mutation；Host 的启动 catalog 可以
    在 workspace 尚未创建时使用 lexical canonical path，避免扩大 AgentHost
    构造契约，而 CLI 仍在启动 sidecar 前通过 validateWorkspace 拒绝该路径。
    """
    try:
        canonical = Path(workspace).expanduser().resolve(strict=strict)
    except OSError as exc:
        raise PluginError("PLUGIN_SCOPE_INVALID", "workspace 不存在或无法解析") from exc
    value = b"harness-plugin-workspace-v1\0" + str(canonical).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _is_staging_excluded_name(name: str, *, directory: bool) -> bool:
    """按 basename 判断本地安装时可以安全忽略的元数据项。"""
    normalized = name.casefold()
    if directory:
        return (
            normalized in _EXCLUDED_DIRECTORY_NAMES
            or normalized in {".env", ".npmrc"}
            or normalized.startswith(".env.")
            or normalized.startswith("._")
        )
    return normalized in _EXCLUDED_FILE_NAMES or normalized.startswith(_EXCLUDED_FILE_PREFIXES)


def _is_staging_excluded_path(relative: Path | PurePosixPath) -> bool:
    """判断 ZIP 相对路径是否落在不应进入 staged package 的元数据范围。"""
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    return any(_is_staging_excluded_name(part, directory=True) for part in parts) or (
        _is_staging_excluded_name(parts[-1], directory=False)
    )


def _copy_directory_secure(source: Path, destination: Path) -> None:
    """不跟随链接复制本地目录，并拒绝 hardlink 与特殊文件。"""
    count = 0
    total = 0
    for root, directories, files in os.walk(source, topdown=True, followlinks=False):
        current = Path(root)
        relative_root = current.relative_to(source)
        if len(relative_root.parts) > MAX_PACKAGE_DEPTH:
            raise PluginError("PLUGIN_PATH_TOO_DEEP", "Plugin 目录层级超过上限")
        # 先按名称剪枝，再对其他项做 lstat；这样 .git socket、.env 和
        # .npmrc 既不会被读取，也不会改变净化包的 count/digest。
        directories[:] = [
            name
            for name in directories
            if not _is_staging_excluded_name(name, directory=True)
        ]
        files[:] = [
            name
            for name in files
            if not _is_staging_excluded_name(name, directory=False)
        ]
        for name in list(directories):
            entry = current / name
            mode = entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 目录不能包含符号链接")
            if not stat.S_ISDIR(mode):
                raise PluginError("PLUGIN_SPECIAL_FILE_REJECTED", "Plugin 只能包含普通文件和目录")
            target = destination / entry.relative_to(source)
            _assert_relative_limits(entry.relative_to(source))
            target.mkdir(parents=True, exist_ok=True)
        for name in files:
            entry = current / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin 不能包含符号链接")
            if not stat.S_ISREG(info.st_mode):
                raise PluginError("PLUGIN_SPECIAL_FILE_REJECTED", "Plugin 只能包含普通文件")
            if info.st_nlink > 1:
                raise PluginError("PLUGIN_HARDLINK_REJECTED", "Plugin 不能包含 hardlink")
            count += 1
            total += info.st_size
            _assert_package_limits(count, total, info.st_size)
            relative = entry.relative_to(source)
            _assert_relative_limits(relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, target)
            target.chmod(0o700 if info.st_mode & 0o111 else 0o600)


def _extract_zip_secure(source: Path, destination: Path) -> tuple[Path, str]:
    """逐项解压 zip，拒绝路径穿越、链接、特殊文件和高压缩比条目。"""
    if source.stat().st_size > MAX_SOURCE_ARCHIVE_BYTES:
        raise PluginError("PLUGIN_ARCHIVE_TOO_LARGE", "Plugin zip 超过来源大小上限")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PluginError("PLUGIN_ARCHIVE_INVALID", "Plugin zip 无法读取") from exc
    count = 0
    total = 0
    with archive:
        members = archive.infolist()
        if len(members) > MAX_PACKAGE_FILES * 2:
            raise PluginError("PLUGIN_FILE_COUNT_EXCEEDED", "Plugin zip 条目数量超过上限")
        for member in members:
            relative = _safe_zip_path(member.filename)
            if relative is None:
                continue
            # ZIP 元数据同样在读取 external_attr 和正文前剪枝；被排除项
            # 不会静默写入 store，其他特殊项仍走原有拒绝分支。
            if _is_staging_excluded_path(relative):
                continue
            mode = (member.external_attr >> 16) & 0o177777
            file_type = stat.S_IFMT(mode)
            is_directory = member.is_dir() or file_type == stat.S_IFDIR
            if file_type == stat.S_IFLNK:
                raise PluginError("PLUGIN_SYMLINK_REJECTED", "Plugin zip 不能包含符号链接")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise PluginError("PLUGIN_SPECIAL_FILE_REJECTED", "Plugin zip 包含特殊文件")
            _assert_relative_limits(relative)
            target = destination.joinpath(*relative.parts)
            if is_directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            count += 1
            total += member.file_size
            _assert_package_limits(count, total, member.file_size)
            if (
                (member.file_size > 0 and member.compress_size == 0)
                or (
                    member.compress_size > 0
                    and member.file_size / member.compress_size > MAX_ZIP_COMPRESSION_RATIO
                )
            ):
                raise PluginError("PLUGIN_ARCHIVE_RATIO_EXCEEDED", "Plugin zip 压缩比超过上限")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(member) as source_file, target.open("xb") as output:
                    shutil.copyfileobj(source_file, output, length=64 * 1024)
            except OSError as exc:
                raise PluginError("PLUGIN_ARCHIVE_INVALID", "Plugin zip 条目无法安全解压") from exc
            target.chmod(0o700 if mode & 0o111 else 0o600)

    entries = sorted(destination.iterdir())
    top_files = [entry for entry in entries if entry.is_file()]
    top_dirs = [entry for entry in entries if entry.is_dir()]
    if not top_files and len(top_dirs) == 1:
        return top_dirs[0], top_dirs[0].name
    return destination, source.stem


def _safe_zip_path(name: str) -> PurePosixPath | None:
    """把 zip 条目规范化为安全 POSIX 相对路径。"""
    normalized = name.replace("\\", "/")
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or re.match(r"^[A-Za-z]:", path.parts[0])
    ):
        raise PluginError("PLUGIN_ARCHIVE_PATH_INVALID", "Plugin zip 包含不安全路径")
    return path


def _assert_relative_limits(relative: Path | PurePosixPath) -> None:
    """限制路径深度和 UTF-8 字节长度。"""
    value = relative.as_posix()
    if len(relative.parts) > MAX_PACKAGE_DEPTH:
        raise PluginError("PLUGIN_PATH_TOO_DEEP", "Plugin 目录层级超过上限")
    if len(value.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise PluginError("PLUGIN_PATH_TOO_LONG", "Plugin 相对路径超过上限")


def _assert_package_limits(count: int, total: int, size: int) -> None:
    """限制单文件、文件数和解压后总大小。"""
    if count > MAX_PACKAGE_FILES:
        raise PluginError("PLUGIN_FILE_COUNT_EXCEEDED", "Plugin 文件数量超过上限")
    if size > MAX_PACKAGE_FILE_BYTES:
        raise PluginError("PLUGIN_FILE_TOO_LARGE", "Plugin 单文件超过大小上限")
    if total > MAX_PACKAGE_BYTES:
        raise PluginError("PLUGIN_PACKAGE_TOO_LARGE", "Plugin 总大小超过上限")


def _apply_source_modes(source: Path, destination: Path) -> None:
    """只保留源文件可执行位，其余权限由 store 统一收紧。"""
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        target = destination / relative
        if source_path.is_dir():
            target.chmod(0o700)
        elif source_path.is_file():
            target.chmod(0o700 if source_path.stat().st_mode & 0o111 else 0o600)


def _make_tree_read_only(root: Path, *, include_root: bool = True) -> None:
    """把 store 完成版本收紧为目录 0555、文件 0444/0555。"""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o555 if include_root else 0o700)


def _make_tree_writable(root: Path) -> None:
    """为显式 purge 恢复当前用户可删除权限。"""
    for path in root.rglob("*"):
        try:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        except OSError:
            continue
    root.chmod(0o700)


def _require_safe_segment(value: str, field: str) -> None:
    """拒绝 registry 字段构造任意 store/data 路径。"""
    if not _SAFE_SEGMENT_RE.fullmatch(value):
        raise PluginError("PLUGIN_REGISTRY_INVALID", f"{field} 不是安全路径段")


def _require_digest(value: str) -> None:
    """校验内容地址 digest。"""
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PluginError("PLUGIN_REGISTRY_INVALID", "package_digest 无效")


def _installed_from_record(value: object) -> InstalledPlugin:
    """严格解码 registry v3 中的单条安装记录。"""
    if not isinstance(value, dict):
        raise TypeError("record must be object")
    expected_fields = {
        "id",
        "source_id",
        "source_label",
        "name",
        "version",
        "description",
        "format",
        "manifest",
        "package_digest",
        "components",
        "diagnostics",
        "activation",
        "installed_at_ms",
        "adapter_revision",
        "origin",
    }
    if set(value) != expected_fields:
        raise ValueError("invalid v3 record fields")
    components_raw = value["components"]
    if not isinstance(components_raw, list):
        raise TypeError("components must be list")
    components = tuple(_component_from_record(item) for item in components_raw)
    activation_user, activation_workspaces = _activation_from_record(value["activation"])
    origin = _origin_from_record(value["origin"])
    plugin = InstalledPlugin(
        plugin_id=_required_string(value, "id"),
        source_id=_required_string(value, "source_id"),
        source_label=_required_string(value, "source_label"),
        name=_required_string(value, "name"),
        version=_optional_record_string(value.get("version")),
        description=_optional_record_string(value.get("description")),
        format=_required_string(value, "format"),  # type: ignore[arg-type]
        manifest=_optional_record_string(value.get("manifest")),
        package_digest=_required_string(value, "package_digest"),
        components=components,
        diagnostics=_string_tuple(value.get("diagnostics", [])),
        activation_user=activation_user,
        activation_workspaces=activation_workspaces,
        installed_at_ms=_required_int(value, "installed_at_ms"),
        adapter_revision=_optional_record_string(value.get("adapter_revision")),
        origin=origin,
    )
    if plugin.format not in {"agent-plugins-1.0", "claude-code", "qwen-code", "hybrid"}:
        raise ValueError("invalid format")
    _require_safe_segment(plugin.source_id, "source_id")
    _require_safe_segment(plugin.name, "name")
    _require_digest(plugin.package_digest)
    if plugin.plugin_id != f"{plugin.source_id}/{plugin.name}":
        # Update 保留内部 ID，但来源目录变化时 source_id 可能更新；因此 v3
        # 只要求 ID 仍是安全 locator，而不把它当作正常名称入口。
        _require_safe_segment(plugin.plugin_id.replace("/", "-"), "id")
    return plugin


def _installed_from_record_v2(value: object) -> InstalledPlugin:
    """严格读取 v2 输入，仅用于一次性迁移，不作为 v3 兼容路径。"""
    if not isinstance(value, dict):
        raise TypeError("record must be object")
    expected_fields = {
        "id",
        "source_id",
        "source_label",
        "name",
        "version",
        "description",
        "format",
        "manifest",
        "package_digest",
        "capability_fingerprint",
        "components",
        "diagnostics",
        "enabled",
        "trusted_capability_fingerprint",
        "installed_at_ms",
        "adapter_revision",
    }
    if set(value) != expected_fields:
        raise ValueError("invalid v2 record fields")
    components_raw = value["components"]
    if not isinstance(components_raw, list):
        raise TypeError("components must be list")
    components = tuple(_component_from_record(item) for item in components_raw)
    _require_digest(_required_string(value, "capability_fingerprint"))
    trusted = _optional_record_string(value.get("trusted_capability_fingerprint"))
    if trusted is not None:
        _require_digest(trusted)
    plugin = InstalledPlugin(
        plugin_id=_required_string(value, "id"),
        source_id=_required_string(value, "source_id"),
        source_label=_required_string(value, "source_label"),
        name=_required_string(value, "name"),
        version=_optional_record_string(value.get("version")),
        description=_optional_record_string(value.get("description")),
        format=_required_string(value, "format"),  # type: ignore[arg-type]
        manifest=_optional_record_string(value.get("manifest")),
        package_digest=_required_string(value, "package_digest"),
        components=components,
        diagnostics=_string_tuple(value.get("diagnostics", [])),
        activation_user="enabled" if _required_bool(value, "enabled") else "disabled",
        activation_workspaces=(),
        installed_at_ms=_required_int(value, "installed_at_ms"),
        adapter_revision=_optional_record_string(value.get("adapter_revision")),
        origin=None,
    )
    if plugin.format not in {"agent-plugins-1.0", "claude-code", "qwen-code", "hybrid"}:
        raise ValueError("invalid format")
    _require_safe_segment(plugin.source_id, "source_id")
    _require_safe_segment(plugin.name, "name")
    _require_digest(plugin.package_digest)
    if not plugin.plugin_id:
        raise ValueError("invalid plugin id")
    return plugin


def _activation_from_record(value: object) -> tuple[PluginActivation, tuple[tuple[str, PluginActivation], ...]]:
    """严格读取 v3 activation，并拒绝绝对路径形式的 workspace key。"""
    if not isinstance(value, dict) or set(value) != {"user", "workspaces"}:
        raise TypeError("activation must contain user and workspaces")
    user = value["user"]
    workspaces = value["workspaces"]
    if user not in {"enabled", "disabled"} or not isinstance(workspaces, dict):
        raise TypeError("invalid activation")
    entries: list[tuple[str, PluginActivation]] = []
    for binding, activation in workspaces.items():
        if (
            not isinstance(binding, str)
            or re.fullmatch(r"[0-9a-f]{64}", binding) is None
            or activation not in {"enabled", "disabled"}
        ):
            raise TypeError("invalid workspace activation")
        entries.append((binding, activation))  # type: ignore[arg-type]
    return user, tuple(sorted(entries))  # type: ignore[return-value]


def _origin_from_record(value: object) -> str | None:
    """读取本地 update origin；origin 不进入正常 Protocol response。"""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "path"}:
        raise TypeError("invalid origin")
    if value.get("kind") != "local" or not isinstance(value.get("path"), str):
        raise TypeError("invalid origin")
    return str(value["path"])


def _migrate_v2_plugin(plugin: InstalledPlugin) -> InstalledPlugin:
    """将 v2 enabled 映射为 v3 user activation，并丢弃 trust 字段。"""
    return plugin


def _decode_json_document(content: bytes) -> object:
    """将 registry 原始 bytes 解码为 JSON，统一报告损坏错误。"""
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginError("PLUGIN_REGISTRY_INVALID", "Plugin registry 不是有效 JSON") from exc


def _name_conflicts(plugins: tuple[InstalledPlugin, ...]) -> tuple[str, ...]:
    """返回大小写不敏感的冲突名称，供 Manager fail closed。"""
    names: dict[str, list[str]] = {}
    for plugin in plugins:
        names.setdefault(plugin.name.casefold(), []).append(plugin.name)
    return tuple(sorted(key for key, values in names.items() if len(values) > 1))


def _component_from_record(value: object) -> PluginComponentReport:
    """严格解码一个组件兼容报告。"""
    if not isinstance(value, dict):
        raise TypeError("component must be object")
    status = _required_string(value, "status")
    if status not in {"supported", "adapted", "unsupported", "invalid"}:
        raise ValueError("invalid component status")
    return PluginComponentReport(
        kind=_required_string(value, "kind"),
        status=status,  # type: ignore[arg-type]
        count=_required_int(value, "count"),
        sources=_string_tuple(value.get("sources", [])),
        capabilities=_string_tuple(value.get("capabilities", [])),
        diagnostics=_string_tuple(value.get("diagnostics", [])),
        effective=_required_bool(value, "effective"),
    )


def _required_string(value: dict[str, Any], field: str) -> str:
    """读取 registry 必填字符串。"""
    result = value[field]
    if not isinstance(result, str) or not result:
        raise TypeError(f"{field} must be string")
    return result


def _optional_record_string(value: object) -> str | None:
    """读取 registry 可选字符串。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError("optional field must be string")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    """读取 registry 字符串数组。"""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("field must be string list")
    return tuple(value)


def _required_bool(value: dict[str, Any], field: str) -> bool:
    """读取 registry 必填布尔值。"""
    result = value[field]
    if not isinstance(result, bool):
        raise TypeError(f"{field} must be bool")
    return result


def _required_int(value: dict[str, Any], field: str) -> int:
    """读取 registry 必填非负整数。"""
    result = value[field]
    if not isinstance(result, int) or isinstance(result, bool) or result < 0:
        raise TypeError(f"{field} must be non-negative int")
    return result
