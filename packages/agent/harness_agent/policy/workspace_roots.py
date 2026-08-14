"""额外工作区根注册表。

``WorkspaceRootRegistry`` 是「哪些目录被允许访问」的唯一事实源：主工作区根、
会话级额外根、持久化额外根与 run 级一次性授权均由此管理。边界中间件、多根
backend 与审批决策三处共用同一实例，避免各自维护副本。

模型对主工作区仍使用 ``/`` 虚拟路径；对外部目录使用真实绝对路径。内部把额外根
映射为 ``/@ext/<root-id>/<rel>``，该形式不得泄漏给模型或用户。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

logger = logging.getLogger(__name__)

RootScope = Literal["primary", "once", "session", "project", "user"]

EXT_PREFIX = "/@ext"
VIRTUAL_HARNESS_ROOT = "/.harness"
ADDITIONAL_DIRECTORIES_KEY = "additional_directories"

_SYSTEM_DIRS_POSIX = frozenset({"/", "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys"})
_SYSTEM_DIRS_WINDOWS = frozenset(
    {
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\ProgramData",
    }
)


@dataclass(frozen=True, slots=True)
class WorkspaceRoot:
    """一个被允许访问的目录根。"""

    root_id: str
    path: Path
    scope: RootScope


@dataclass(frozen=True, slots=True)
class ResolvedPath:
    """路径判定结果。"""

    root: WorkspaceRoot
    backend_path: str
    display_path: str


@dataclass(frozen=True, slots=True)
class TrustCandidate:
    """可进入目录信任审批的外部路径候选。"""

    directory: Path
    target_path: str
    shadows_workspace: bool
    reason: str | None = None


class ExternalPathNotTrusted(Exception):
    """路径落在未授权的外部目录，可进入目录信任审批。"""

    def __init__(self, candidate: TrustCandidate) -> None:
        self.candidate = candidate
        super().__init__(f"路径不在允许的工作区内：{candidate.target_path}")


class DirectoryNotTrustable(ValueError):
    """目录因安全策略不可注册为额外根。"""


def normalize_host_path(raw: str | Path) -> Path:
    """统一规范化宿主路径：分隔符、realpath、Windows 大小写折叠。"""
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("路径必须是绝对路径")
    resolved = path.resolve(strict=False)
    if sys.platform == "win32":
        return Path(os.path.normcase(str(resolved)))
    return resolved


def is_unc_path(raw: str) -> bool:
    """判断字符串是否为 UNC / 网络共享路径。"""
    normalized = raw.replace("\\", "/")
    return raw.startswith("\\\\") or normalized.startswith("//")


def has_parent_traversal(raw: str) -> bool:
    """判断路径字符串是否包含 ``..`` 穿越段。"""
    normalized = raw.replace("\\", "/")
    return ".." in PurePosixPath(normalized).parts


def is_os_absolute_path(raw: str) -> bool:
    """判断是否为操作系统绝对路径（盘符或 POSIX 绝对路径）。"""
    if PureWindowsPath(raw).drive:
        return True
    # POSIX 绝对路径：以 / 开头，但不是我们的内部虚拟前缀
    if raw.startswith("/") and not raw.startswith(EXT_PREFIX) and not raw.startswith(VIRTUAL_HARNESS_ROOT):
        # 纯 /xxx 在本仓库中默认是主根虚拟路径；仅当它同时是真实宿主绝对路径
        # 且与主根无关时才在 registry.resolve 中另行区分。这里只报告"看起来像 OS 绝对"。
        return PurePosixPath(raw).is_absolute()
    return False


def _stable_root_id(path: Path) -> str:
    """为额外根生成稳定短哈希标识。"""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return digest[:12]


def _is_filesystem_root(path: Path) -> bool:
    """判断是否为文件系统根或盘符根。"""
    resolved = path.resolve(strict=False)
    return resolved.parent == resolved


def _is_home_directory(path: Path) -> bool:
    """判断是否恰好等于用户 home 目录本身。"""
    try:
        return path.resolve(strict=False) == Path.home().resolve(strict=False)
    except OSError:
        return False


def _is_system_directory(path: Path) -> bool:
    """判断是否落在硬编码的系统目录黑名单中。"""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return True
    if sys.platform == "win32":
        candidates = {Path(item).resolve(strict=False) for item in _SYSTEM_DIRS_WINDOWS}
        normalized = Path(os.path.normcase(str(resolved)))
        return any(
            normalized == Path(os.path.normcase(str(item)))
            or normalized.is_relative_to(Path(os.path.normcase(str(item))))
            for item in candidates
        )
    candidates = {Path(item).resolve(strict=False) for item in _SYSTEM_DIRS_POSIX if item != "/"}
    if sys.platform == "darwin":
        candidates.update({Path(item).resolve(strict=False) for item in ("/System", "/Library", "/Applications", "/private/etc")})
    return any(
        resolved == item or resolved.is_relative_to(item)
        for item in candidates
    ) or resolved == Path("/").resolve()


def _settings_path(scope: Literal["project", "user", "system"], project_dir: Path | None) -> Path | None:
    """返回对应作用域的 settings.json 路径。"""
    if scope == "project":
        base = Path(project_dir) if project_dir is not None else Path.cwd()
        return base / ".harness" / "settings.json"
    if scope == "system":
        if sys.platform == "win32":
            return Path("C:/ProgramData/harness/settings.json")
        if sys.platform == "darwin":
            return Path("/Library/Application Support/Harness/settings.json")
        return Path("/etc/harness/settings.json")
    return Path.home() / ".harness" / "settings.json"


def _read_additional_directories(path: Path) -> list[str]:
    """从 settings.json 读取 additional_directories；失败时返回空列表。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get(ADDITIONAL_DIRECTORIES_KEY)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def load_additional_directories(project_dir: Path | None = None) -> dict[RootScope, list[Path]]:
    """加载 system/user/project 层额外目录，不抛异常。"""
    result: dict[RootScope, list[Path]] = {"system": [], "user": [], "project": []}
    for scope in ("system", "user", "project"):
        path = _settings_path(scope, project_dir)  # type: ignore[arg-type]
        if path is None or not path.is_file():
            continue
        directories: list[Path] = []
        for raw in _read_additional_directories(path):
            try:
                directories.append(normalize_host_path(raw))
            except (ValueError, OSError):
                logger.warning("忽略无效的额外工作目录: scope=%s path=%r", scope, raw)
        result[scope] = directories  # type: ignore[index]
    return result


def save_additional_directory(directory: Path, *, project_dir: Path | None = None) -> None:
    """把目录追加写入 project 层 ``additional_directories``，保留其它字段。"""
    path = _settings_path("project", project_dir)
    if path is None:
        raise OSError("无法确定 project settings 路径")
    normalized = str(normalize_host_path(directory))
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    existing = data.get(ADDITIONAL_DIRECTORIES_KEY)
    entries = [str(item) for item in existing] if isinstance(existing, list) else []
    if normalized not in entries:
        entries.append(normalized)
    data[ADDITIONAL_DIRECTORIES_KEY] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class WorkspaceRootRegistry:
    """可变的允许根集合；内容不参与执行资源池 fingerprint。"""

    def __init__(
        self,
        primary: str | Path,
        *,
        project_dir: Path | None = None,
        load_persisted: bool = True,
    ) -> None:
        """绑定主根，可选加载持久化额外根。"""
        self._lock = threading.RLock()
        primary_path = normalize_host_path(primary)
        self._primary = WorkspaceRoot(root_id="primary", path=primary_path, scope="primary")
        self._project_dir = Path(project_dir).resolve(strict=False) if project_dir is not None else primary_path
        self._extra: dict[str, WorkspaceRoot] = {}
        # once: (run_id, normalized_dir_str) -> WorkspaceRoot，单次消费
        self._once: dict[tuple[str, str], WorkspaceRoot] = {}
        if load_persisted:
            loaded = load_additional_directories(self._project_dir)
            for scope in ("system", "user", "project"):
                for directory in loaded[scope]:  # type: ignore[index]
                    try:
                        self._register_unlocked(directory, scope)  # type: ignore[arg-type]
                    except (DirectoryNotTrustable, ValueError, OSError) as exc:
                        logger.warning("启动时跳过不可用额外根 %s: %s", directory, exc)

    @property
    def primary(self) -> WorkspaceRoot:
        """返回主工作区根。"""
        return self._primary

    def roots(self) -> tuple[WorkspaceRoot, ...]:
        """返回当前全部允许根（主根在前）。"""
        with self._lock:
            return (self._primary, *self._extra.values())

    def readonly_view(self) -> WorkspaceRootRegistry:
        """返回共享同一状态、但 ``trust`` 会拒绝的只读视图（供子 Agent 使用）。"""
        view = object.__new__(WorkspaceRootRegistry)
        view._lock = self._lock
        view._primary = self._primary
        view._project_dir = self._project_dir
        view._extra = self._extra
        view._once = self._once
        view._readonly = True
        return view

    def _is_readonly(self) -> bool:
        return bool(getattr(self, "_readonly", False))

    def resolve(self, raw: str, *, run_id: str | None = None) -> ResolvedPath:
        """判定路径属于哪个允许根；未授权外部路径抛 ``ExternalPathNotTrusted``。"""
        if not isinstance(raw, str) or not raw:
            raise ValueError("路径必须是非空字符串")
        if is_unc_path(raw):
            raise ValueError("不支持 UNC 文件路径")
        if has_parent_traversal(raw):
            raise ValueError("文件路径不能包含 '..' 路径段")

        # 1. /.harness 虚拟命名空间：不经 registry 解析，由调用方单独处理
        if raw == VIRTUAL_HARNESS_ROOT or raw.startswith(f"{VIRTUAL_HARNESS_ROOT}/"):
            raise ValueError("/.harness 由虚拟 backend 处理")

        # 内部挂载路径回解
        if raw == EXT_PREFIX or raw.startswith(f"{EXT_PREFIX}/"):
            return self._resolve_ext_backend_path(raw)

        with self._lock:
            # Windows 盘符路径：明确的宿主绝对路径
            if PureWindowsPath(raw).drive:
                try:
                    host = normalize_host_path(raw)
                except (ValueError, OSError) as exc:
                    raise ValueError(f"无效的绝对路径：{raw}") from exc
                return self._resolve_host_path(host, raw=raw, run_id=run_id)

            # 以 / 开头：先检查已注册额外根遮蔽，再区分「宿主绝对」与「主根虚拟」
            if raw == "/":
                return ResolvedPath(
                    root=self._primary,
                    backend_path="/",
                    display_path="/",
                )
            if raw.startswith("/"):
                # 尝试按宿主绝对路径解释，用于额外根匹配与外部信任
                host_candidate = self._host_absolute_if_external(raw)
                if host_candidate is not None:
                    return self._resolve_host_path(host_candidate, raw=raw, run_id=run_id)

                # 主根虚拟路径
                lexical = self._primary.path / raw.lstrip("/")
                real = lexical.resolve(strict=False)
                # 若 virtual→real 落在额外根内（例如主根内 symlink），仍走额外根
                matched = self._match_extra_or_once(real, run_id=run_id)
                if matched is not None and matched.root_id != "primary":
                    return self._resolved_for(matched, real, display_as_host=True)
                try:
                    real.relative_to(self._primary.path)
                except ValueError as exc:
                    raise ValueError(
                        f"只能访问工作目录 `{self._primary.path}` 内的文件"
                    ) from exc
                # 交给 backend 时保留字面路径，不提前解引用工作区内 symlink
                return ResolvedPath(
                    root=self._primary,
                    backend_path="/" if raw == "/" else raw,
                    display_path="/" if raw == "/" else raw,
                )

            raise ValueError("必须使用以 `/` 开头的工作区虚拟路径或已授权的绝对路径")

    def trust_candidate(self, raw: str) -> TrustCandidate:
        """计算待信任目录并校验可注册性；不可注册时抛 ``DirectoryNotTrustable``。"""
        if not isinstance(raw, str) or not raw:
            raise ValueError("路径必须是非空字符串")
        if is_unc_path(raw):
            raise DirectoryNotTrustable("不支持 UNC 文件路径")
        if has_parent_traversal(raw):
            raise DirectoryNotTrustable("文件路径不能包含 '..' 路径段")
        if not (PureWindowsPath(raw).drive or raw.startswith("/")):
            raise DirectoryNotTrustable("待信任路径必须是绝对路径")
        try:
            host = normalize_host_path(raw)
        except (ValueError, OSError) as exc:
            raise DirectoryNotTrustable(f"无效的绝对路径：{raw}") from exc
        # 已在主根内
        if _is_relative_to(host, self._primary.path):
            raise DirectoryNotTrustable("目标已在主工作区内，无需信任")
        with self._lock:
            if self._match_extra_or_once(host, run_id=None) is not None:
                # 已信任：仍返回候选，调用方可据此跳过审批
                directory = host if host.is_dir() else host.parent
                return TrustCandidate(
                    directory=normalize_host_path(directory),
                    target_path=str(host),
                    shadows_workspace=self._shadows_workspace(normalize_host_path(directory)),
                    reason=None,
                )
        return self._build_candidate(raw, host, require_trustable=True)

    def trust(
        self,
        directory: str | Path,
        scope: RootScope,
        *,
        run_id: str | None = None,
        persist: bool = True,
    ) -> WorkspaceRoot:
        """注册额外根；``project`` 作用域默认落盘。"""
        if self._is_readonly():
            raise DirectoryNotTrustable("只读 registry 不能注册额外根")
        if scope == "primary":
            raise ValueError("不能以 primary 作用域注册额外根")
        if scope == "once" and not run_id:
            raise ValueError("once 作用域必须提供 run_id")
        normalized = normalize_host_path(directory)
        self._assert_trustable(normalized)
        with self._lock:
            root = self._register_unlocked(normalized, scope, run_id=run_id)
        if scope == "project" and persist:
            try:
                save_additional_directory(normalized, project_dir=self._project_dir)
            except OSError as exc:
                logger.warning("额外根落盘失败，降级为 session: %s", exc)
                with self._lock:
                    # 把刚写入的 project 根降级为 session
                    if root.root_id in self._extra:
                        self._extra[root.root_id] = WorkspaceRoot(
                            root_id=root.root_id, path=root.path, scope="session"
                        )
                        root = self._extra[root.root_id]
        return root

    def consume_once(self, directory: str | Path, *, run_id: str) -> None:
        """消费 once 授权，使同目录下一次访问再次询问。"""
        key = (run_id, str(normalize_host_path(directory)))
        with self._lock:
            self._once.pop(key, None)

    def clear_once_for_run(self, run_id: str) -> None:
        """清理指定 run 的全部一次性授权。"""
        with self._lock:
            stale = [key for key in self._once if key[0] == run_id]
            for key in stale:
                del self._once[key]

    def to_display(self, backend_path: str) -> str:
        """把内部 backend 路径投影为面向模型/用户的路径。"""
        if not isinstance(backend_path, str) or not backend_path:
            return backend_path
        if backend_path.startswith(f"{EXT_PREFIX}/"):
            try:
                resolved = self._resolve_ext_backend_path(backend_path)
            except ValueError:
                return backend_path
            return resolved.display_path
        return backend_path

    def get_root(self, root_id: str) -> WorkspaceRoot | None:
        """按 root_id 查询根。"""
        if root_id == "primary":
            return self._primary
        with self._lock:
            return self._extra.get(root_id)

    def display_extra_roots(self) -> list[str]:
        """返回已信任额外根的真实绝对路径列表，供动态提示词使用。"""
        with self._lock:
            return [str(root.path) for root in self._extra.values()]

    def _host_absolute_if_external(self, raw: str) -> Path | None:
        """POSIX ``/`` 路径：区分主根虚拟路径与宿主绝对路径。

        规则：宿主解释落在主根外，且目标或其父目录真实存在时，视为外部绝对路径。
        已注册额外根的前缀匹配不依赖存在性。落在主根内的宿主绝对路径也返回，
        以便 ``D:`` 风格之外的真实路径访问主根文件。

        注意：若系统存在 ``/tmp`` 等顶层目录，模型用 ``/tmp/x`` 作为虚拟路径时
        会被解释为宿主 ``/tmp/x``；主根内应使用 ``/src/...`` 等不会与宿主顶层
        冲突的相对虚拟路径（与改造前常见用法一致）。
        """
        try:
            host = normalize_host_path(raw)
        except (ValueError, OSError):
            return None
        # 已注册额外根：无论目标是否存在都按宿主绝对处理（遮蔽优先）
        if self._match_extra_or_once(host, run_id=None) is not None:
            return host
        try:
            host.relative_to(self._primary.path)
            return host
        except ValueError:
            pass
        if host.exists():
            return host
        if host.parent.exists() and not _is_filesystem_root(host.parent) and not _is_system_directory(host.parent):
            return host
        return None

    def _resolve_host_path(
        self, host: Path, *, raw: str, run_id: str | None
    ) -> ResolvedPath:
        """解析已确认的宿主绝对路径。"""
        matched = self._match_extra_or_once(host, run_id=run_id)
        if matched is not None:
            return self._resolved_for(matched, host, display_as_host=True)
        try:
            host.relative_to(self._primary.path)
            return self._resolved_for(self._primary, host, display_as_host=False)
        except ValueError:
            candidate = self._build_candidate(raw, host)
            raise ExternalPathNotTrusted(candidate) from None

    def _match_extra_or_once(
        self, host: Path, *, run_id: str | None
    ) -> WorkspaceRoot | None:
        """在额外根与 once 授权中查找包含 host 的最长前缀匹配。"""
        best: WorkspaceRoot | None = None
        best_len = -1
        for root in self._extra.values():
            if not _is_relative_to(host, root.path):
                continue
            length = len(str(root.path))
            if length > best_len:
                best = root
                best_len = length
        if run_id:
            for (stored_run, _dir), root in self._once.items():
                if stored_run != run_id:
                    continue
                if not _is_relative_to(host, root.path):
                    continue
                length = len(str(root.path))
                if length > best_len:
                    best = root
                    best_len = length
        return best

    def _resolved_for(
        self, root: WorkspaceRoot, host: Path, *, display_as_host: bool
    ) -> ResolvedPath:
        """构造 ResolvedPath。"""
        try:
            relative = host.relative_to(root.path)
        except ValueError as exc:
            raise ValueError(f"路径不在根 `{root.path}` 内") from exc
        if root.root_id == "primary":
            backend = "/" if not relative.parts else f"/{relative.as_posix()}"
            display = str(host) if display_as_host else backend
            return ResolvedPath(root=root, backend_path=backend, display_path=display)
        rel = relative.as_posix()
        backend = f"{EXT_PREFIX}/{root.root_id}" if not relative.parts else f"{EXT_PREFIX}/{root.root_id}/{rel}"
        return ResolvedPath(root=root, backend_path=backend, display_path=str(host))

    def _resolve_ext_backend_path(self, raw: str) -> ResolvedPath:
        """解析内部 ``/@ext/<root-id>/...`` 路径。"""
        rest = raw[len(EXT_PREFIX) :].lstrip("/")
        if not rest:
            raise ValueError("内部扩展路径缺少 root_id")
        parts = PurePosixPath(rest).parts
        root_id = parts[0]
        root = self.get_root(root_id)
        if root is None:
            raise ValueError(f"未知的扩展根：{root_id}")
        relative = PurePosixPath(*parts[1:]) if len(parts) > 1 else PurePosixPath()
        host = root.path / relative
        return self._resolved_for(root, normalize_host_path(host), display_as_host=True)

    def _shadows_workspace(self, directory: Path) -> bool:
        """额外根的路径名是否会遮蔽主工作区内的同名相对路径。"""
        # POSIX：额外根 /data 会遮蔽主根下的 /data 虚拟路径
        if sys.platform == "win32":
            return False
        name = directory.name
        if not name:
            return False
        return (self._primary.path / name.lstrip("/")).exists()

    def _build_candidate(
        self, raw: str, host: Path, *, require_trustable: bool = False
    ) -> TrustCandidate:
        """从目标路径推导待信任目录。"""
        directory = host if host.is_dir() else host.parent
        try:
            directory = normalize_host_path(directory)
        except (ValueError, OSError) as exc:
            raise DirectoryNotTrustable(f"无法规范化待信任目录：{directory}") from exc
        reason: str | None = None
        try:
            self._assert_trustable(directory)
        except DirectoryNotTrustable as exc:
            if require_trustable:
                raise
            reason = str(exc)
        return TrustCandidate(
            directory=directory,
            target_path=str(host) if host.is_absolute() else raw,
            shadows_workspace=self._shadows_workspace(directory),
            reason=reason,
        )

    def _assert_trustable(self, directory: Path) -> None:
        """校验目录是否允许注册为额外根。"""
        if is_unc_path(str(directory)):
            raise DirectoryNotTrustable("不支持 UNC 文件路径")
        if _is_filesystem_root(directory):
            raise DirectoryNotTrustable("不能把文件系统根或盘符根注册为额外工作目录")
        if _is_home_directory(directory):
            raise DirectoryNotTrustable("不能把用户 home 目录本身注册为额外工作目录")
        if _is_system_directory(directory):
            raise DirectoryNotTrustable("不能把系统目录注册为额外工作目录")
        if _is_relative_to(directory, self._primary.path):
            raise DirectoryNotTrustable("目标已在主工作区内，无需信任")
        if not directory.exists():
            raise DirectoryNotTrustable(f"目录不存在：{directory}")
        if not directory.is_dir():
            raise DirectoryNotTrustable(f"不是目录：{directory}")

    def _register_unlocked(
        self,
        directory: Path,
        scope: RootScope,
        *,
        run_id: str | None = None,
    ) -> WorkspaceRoot:
        """在已持锁的前提下注册根，处理嵌套吸收。"""
        self._assert_trustable(directory)
        # 已落在现有根内 → 不重复注册，返回现有根
        existing = self._match_extra_or_once(directory, run_id=run_id)
        if existing is not None:
            return existing
        try:
            directory.relative_to(self._primary.path)
            return self._primary
        except ValueError:
            pass

        # 新根包含已有额外根 → 吸收（删除被包含的）
        absorbed = [
            root_id
            for root_id, root in self._extra.items()
            if _is_relative_to(root.path, directory)
        ]
        for root_id in absorbed:
            del self._extra[root_id]

        root = WorkspaceRoot(root_id=_stable_root_id(directory), path=directory, scope=scope)
        if scope == "once":
            assert run_id is not None
            self._once[(run_id, str(directory))] = root
            # once 同时也要能被 resolve 匹配；临时放入 _extra 会在 consume 时清理
            # 设计：once 只在 _once 中，resolve 通过 _match_extra_or_once 查找
        else:
            self._extra[root.root_id] = root
        return root


def _is_relative_to(path: Path, other: Path) -> bool:
    """判断 path 是否位于 other 之下（Windows 上忽略大小写）。"""
    try:
        if sys.platform == "win32":
            path = Path(os.path.normcase(str(path.resolve(strict=False))))
            other = Path(os.path.normcase(str(other.resolve(strict=False))))
        path.relative_to(other)
        return True
    except ValueError:
        return False
