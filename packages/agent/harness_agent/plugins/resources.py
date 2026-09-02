"""已安装 Plugin 的静态只读资源快照与虚拟路径边界。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from harness_agent.plugins.common import (
    list_regular_files,
    parse_qwen_markdown,
    read_json_object,
    safe_package_path,
)
from harness_agent.plugins.model import InstalledPlugin, PluginError
from harness_agent.plugins.mcp_schema import validate_qwen_mcp_server
from harness_agent.plugins.store import PluginStore


_RESOURCE_KINDS = frozenset({"commands", "skills", "agents", "mcp", "contexts", "hooks"})
_SAFE_DEPENDENCY_ROOTS = frozenset({"references", "scripts", "mcp"})
_MAX_RESOURCE_FILE_BYTES = 8 * 1024 * 1024
_MAX_RESOURCE_TOTAL_BYTES = 32 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLACEHOLDER_RE = re.compile(
    r"\$\{extensionPath\}\$\{/\}|<extensionPath>|"
    r"\$\{extensionPath\}|\$\{workspacePath\}|"
    r"\$\{pathSeparator\}|\$\{/\}|\$\{CLAUDE_PLUGIN_ROOT\}"
)
_UNKNOWN_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}|<[^>\r\n]+>")


@dataclass(frozen=True, slots=True)
class _ResolvedPlaceholder:
    """受控 token 解析结果，同时保留需要纳入快照的相对目标。"""

    value: str
    targets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginResourceAsset:
    """快照中的单个文件或静态逻辑资源，不携带宿主绝对路径。"""

    kind: str
    source: str
    virtual_path: str
    digest: str
    size: int
    content: bytes = field(repr=False, compare=False)
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        """冻结内容和 metadata 外壳，避免快照建立后被调用方替换。"""
        object.__setattr__(self, "content", bytes(self.content))
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))

    def to_dict(self) -> dict[str, object]:
        """返回可进入列表/协议摘要的脱敏资源描述。"""
        result: dict[str, object] = {
            "kind": self.kind,
            "source": self.source,
            "virtual_path": self.virtual_path,
            "digest": self.digest,
            "size": self.size,
            "read_only": True,
        }
        result.update(_thaw_metadata(self.metadata))
        return result


@dataclass(frozen=True, slots=True)
class PluginResourceSnapshot:
    """一个已安装 Plugin 的不可变静态资源目录。"""

    plugin_id: str
    package_digest: str
    virtual_root: str
    snapshot_id: str
    component_counts: Mapping[str, int]
    resources: tuple[PluginResourceAsset, ...]

    def __post_init__(self) -> None:
        """冻结组件计数和资源顺序。"""
        object.__setattr__(self, "component_counts", MappingProxyType(dict(self.component_counts)))
        object.__setattr__(self, "resources", tuple(self.resources))

    @property
    def counts(self) -> Mapping[str, int]:
        """返回阶段二静态组件计数。"""
        return self.component_counts

    def to_dict(self) -> dict[str, object]:
        """返回不含资源正文和宿主路径的 snapshot 摘要。"""
        return {
            "id": self.snapshot_id,
            "plugin_id": self.plugin_id,
            "package_digest": self.package_digest,
            "virtual_root": self.virtual_root,
            "read_only": True,
            "counts": dict(self.component_counts),
            "resources": [asset.to_dict() for asset in self.resources],
        }

    def read_bytes(self, virtual_path: str) -> bytes:
        """从内存快照读取已声明资源，拒绝绝对宿主路径和路径穿越。"""
        normalized = _validate_virtual_resource_path(self.virtual_root, virtual_path)
        for asset in self.resources:
            if asset.virtual_path == normalized:
                return bytes(asset.content)
        raise PluginError(
            "PLUGIN_RESOURCE_NOT_FOUND",
            "虚拟 Plugin 资源不在当前快照中",
        )

    def read_text(self, virtual_path: str) -> str:
        """以 UTF-8 读取静态资源；二进制或坏编码不会静默转换。"""
        try:
            return self.read_bytes(virtual_path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PluginError(
                "PLUGIN_RESOURCE_ENCODING_INVALID",
                "虚拟 Plugin 资源不是有效 UTF-8",
            ) from exc

    def resolve_relative(self, base_virtual_path: str, relative_path: str) -> str:
        """从快照内文件解析包内相对引用，允许回到虚拟根但不能越界。"""
        base = _validate_virtual_resource_path(self.virtual_root, base_virtual_path)
        if not isinstance(relative_path, str) or not relative_path:
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                "相对资源路径不能为空",
            )
        normalized = relative_path.replace("\\", "/")
        if PurePosixPath(normalized).is_absolute():
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                "相对资源路径不能是绝对路径",
            )
        relative = base[len(self.virtual_root) + 1 :]
        parts = list(PurePosixPath(relative).parent.parts)
        for part in normalized.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                if not parts:
                    raise PluginError(
                        "PLUGIN_RESOURCE_PATH_INVALID",
                        "相对资源路径越过 Plugin 根目录",
                    )
                parts.pop()
                continue
            parts.append(part)
        if not parts:
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                "相对资源路径不能指向 Plugin 根目录",
            )
        candidate = f"{self.virtual_root}/{'/'.join(parts)}"
        self.read_bytes(candidate)
        return candidate

    def read_relative_bytes(self, base_virtual_path: str, relative_path: str) -> bytes:
        """读取相对于快照文件的包内资源正文。"""
        return self.read_bytes(self.resolve_relative(base_virtual_path, relative_path))

    def read_relative_text(self, base_virtual_path: str, relative_path: str) -> str:
        """以 UTF-8 读取相对于快照文件的包内资源正文。"""
        return self.read_text(self.resolve_relative(base_virtual_path, relative_path))

def build_plugin_resource_snapshot(
    plugin: InstalledPlugin,
    *,
    store: PluginStore,
) -> PluginResourceSnapshot:
    """从已安装 store 建立静态资源快照，不把任何资产交给运行时 consumer。"""
    store.verify_installed(plugin)
    root = store.package_path(plugin)
    virtual_root = _virtual_root(plugin.plugin_id)
    resources: list[PluginResourceAsset] = []
    counts: dict[str, int] = {}
    seen: set[str] = set()
    total_bytes = 0

    def capture_file(kind: str, relative: str, path: Path) -> None:
        """捕获单个已允许文件并统一应用大小与去重边界。"""
        nonlocal total_bytes
        if not _safe_snapshot_relative(relative):
            return
        if relative in seen:
            return
        asset = _file_asset(
            kind,
            relative,
            path,
            virtual_root=virtual_root,
            package_root=root,
            strict=plugin.format == "qwen-code",
            available_relatives=seen,
        )
        total_bytes += asset.size
        if total_bytes > _MAX_RESOURCE_TOTAL_BYTES:
            raise PluginError(
                "PLUGIN_RESOURCE_SNAPSHOT_LIMIT",
                "Plugin 静态资源快照超过大小上限",
            )
        seen.add(relative)
        resources.append(asset)

    # Qwen/DevAgent 的运行字段会从根 scripts、mcp 和 references 引用资产。
    # 这里只允许明确的三个根目录，并过滤隐藏开发文件和秘密命名文件；不扫描整个包。
    if plugin.format == "qwen-code":
        for dependency_root in sorted(_SAFE_DEPENDENCY_ROOTS):
            dependency_path = root / dependency_root
            files = (
                _safe_reference_files(dependency_path, root)
                if dependency_root == "references"
                else _safe_dependency_files(dependency_path, root)
            )
            for file in files:
                capture_file(
                    "resources",
                    file.relative_to(root).as_posix(),
                    file,
                )
        for field, suffixes in (("commands", (".md",)), ("skills", (".md",))):
            for path in _qwen_component_paths(root, field):
                for file in _safe_qwen_files(path, suffixes=suffixes):
                    if field == "skills" and file.name != "SKILL.md":
                        continue
                    capture_file(field, file.relative_to(root).as_posix(), file)

    for component in plugin.components:
        if component.kind not in _RESOURCE_KINDS or component.count <= 0:
            continue
        counts[component.kind] = counts.get(component.kind, 0) + component.count
        if component.kind == "mcp":
            resources.extend(
                _mcp_assets(
                    plugin,
                    root=root,
                    virtual_root=virtual_root,
                    component_sources=component.sources,
                    available_relatives=seen,
                )
            )
            continue
        if component.kind == "hooks":
            resources.extend(
                _hook_assets(
                    plugin,
                    root=root,
                    virtual_root=virtual_root,
                    count=component.count,
                    available_relatives=seen,
                )
            )
            continue
        for source in component.sources:
            path = safe_package_path(root, source, require_exists=True)
            base = path.parent if component.kind == "skills" and path.name == "SKILL.md" else path
            files = list_regular_files(base) if base.is_dir() else (base,)
            for file in files:
                relative = file.relative_to(root).as_posix()
                capture_file(component.kind, relative, file)

    resources.sort(key=lambda item: (item.kind, item.source, item.virtual_path))
    counts = dict(sorted(counts.items()))
    snapshot_id = _snapshot_id(
        plugin,
        counts,
        resources,
    )
    return PluginResourceSnapshot(
        plugin_id=plugin.plugin_id,
        package_digest=plugin.package_digest,
        virtual_root=virtual_root,
        snapshot_id=snapshot_id,
        component_counts=counts,
        resources=tuple(resources),
    )


def _file_asset(
    kind: str,
    relative: str,
    path: Path,
    *,
    virtual_root: str,
    package_root: Path | None = None,
    strict: bool = False,
    available_relatives: set[str] = frozenset(),
) -> PluginResourceAsset:
    """复制一个包内普通文件的正文到内存快照并校验命令 token。"""
    try:
        size = path.stat().st_size
        if size > _MAX_RESOURCE_FILE_BYTES:
            raise PluginError(
                "PLUGIN_RESOURCE_SNAPSHOT_LIMIT",
                "Plugin 静态资源文件超过大小上限",
            )
        content = path.read_bytes()
    except PluginError:
        raise
    except OSError as exc:
        raise PluginError(
            "PLUGIN_RESOURCE_READ_FAILED",
            "无法读取已安装 Plugin 静态资源",
        ) from exc
    metadata: dict[str, object] = {"runnable": False}
    if kind == "commands" and package_root is not None:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            metadata["diagnostic"] = "PLUGIN_COMPONENT_INVALID: QWEN_MARKDOWN_ENCODING_INVALID"
            text = ""
        if strict and text:
            try:
                parse_qwen_markdown(
                    path,
                    name_hint=_qwen_command_name(relative),
                    kind="command",
                )
            except ValueError as exc:
                metadata["diagnostic"] = f"PLUGIN_COMPONENT_INVALID: {exc}"
        resolved = _resolve_known_placeholder(
            text,
            virtual_root,
            package_root=package_root,
            strict=strict,
            available_relatives=available_relatives,
            field="command",
        )
        if strict and resolved.value != text:
            content = resolved.value.encode("utf-8")
        if resolved.targets:
            metadata["placeholder_targets"] = list(
                f"{virtual_root}/{target}" for target in resolved.targets
            )
    return PluginResourceAsset(
        kind=kind,
        source=relative,
        virtual_path=f"{virtual_root}/{relative}",
        digest=_digest(content),
        size=len(content),
        content=content,
        metadata=metadata,
    )


def _safe_dependency_files(path: Path, root: Path) -> tuple[Path, ...]:
    """列出允许的静态依赖文件，排除隐藏目录、环境文件和凭据命名。"""
    if not path.exists():
        return ()
    files = list_regular_files(path)
    return tuple(
        file
        for file in files
        if _safe_dependency_relative(file.relative_to(root).as_posix())
    )


def _safe_reference_files(path: Path, root: Path) -> tuple[Path, ...]:
    """只列出 Qwen Skill 顶层 references Markdown，不递归 origin 素材。"""
    if path.is_symlink() or not path.is_dir():
        return ()
    try:
        entries = sorted(path.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    return tuple(
        item
        for item in entries
        if not item.is_symlink()
        and item.is_file()
        and item.suffix.lower() == ".md"
        and _safe_dependency_relative(item.relative_to(root).as_posix())
    )


def _qwen_component_paths(root: Path, field: str) -> tuple[Path, ...]:
    """按 Qwen/DevAgent 清单解析 commands/skills 的候选根。"""
    manifest_paths = [
        root / name
        for name in ("qwen-extension.json", "devagent-extension.json")
        if (root / name).is_file()
    ]
    if len(manifest_paths) != 1:
        return ()
    try:
        manifest = read_json_object(root, manifest_paths[0].name)
        value = manifest.get(field)
        if value is None:
            values = (field,) if (root / field).exists() else ()
        elif isinstance(value, str):
            values = (value,)
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = tuple(value)
        else:
            return ()
        paths: list[Path] = []
        for relative in values:
            try:
                paths.append(safe_package_path(root, relative, require_exists=True))
            except PluginError:
                continue
        return tuple(paths)
    except PluginError:
        return ()


def _qwen_command_name(relative: str) -> str:
    """把静态 Qwen Command 来源转换为 front matter 校验名。"""
    parts = relative.replace("\\", "/").split("/")
    if "commands" in parts:
        parts = parts[parts.index("commands") + 1 :]
    if parts and parts[-1].lower().endswith(".md"):
        parts[-1] = parts[-1][:-3]
    return ":".join(part for part in parts if part)


def _safe_qwen_files(path: Path, *, suffixes: tuple[str, ...]) -> tuple[Path, ...]:
    """静态预览扫描不跟随 Qwen 包内 symlink，并保留其他条目。"""
    if path.is_symlink() or not path.exists():
        return ()
    if path.is_file():
        return (path,) if path.suffix.lower() in suffixes else ()
    if not path.is_dir():
        return ()
    files: list[Path] = []
    for directory, dir_names, file_names in os.walk(path, followlinks=False):
        directory_path = Path(directory)
        dir_names[:] = [
            name for name in dir_names if not (directory_path / name).is_symlink()
        ]
        for name in sorted(file_names):
            file = directory_path / name
            if not file.is_symlink() and file.suffix.lower() in suffixes:
                files.append(file)
    return tuple(files)


def _safe_dependency_relative(relative: str) -> bool:
    """判断资源闭包内相对路径是否不会引入开发期或秘密文件。"""
    path = PurePosixPath(relative)
    if not _safe_snapshot_relative(relative):
        return False
    return bool(path.parts) and path.parts[0] in _SAFE_DEPENDENCY_ROOTS


def _safe_snapshot_relative(relative: str) -> bool:
    """拒绝所有静态快照中的隐藏开发文件和常见凭据文件。"""
    path = PurePosixPath(relative)
    if any(part.startswith(".") or part == ".git" for part in path.parts):
        return False
    filename = path.name.lower()
    if filename == ".env" or filename.startswith(".env."):
        return False
    if filename in {"id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json"}:
        return False
    if filename.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    return bool(path.parts)


def _mcp_assets(
    plugin: InstalledPlugin,
    *,
    root: Path,
    virtual_root: str,
    component_sources: tuple[str, ...],
    available_relatives: set[str],
) -> list[PluginResourceAsset]:
    """把 MCP server 转成仅用于展示的虚拟静态条目，不生成运行配置。"""
    documents: list[tuple[str, Mapping[str, object]]] = []
    for source in component_sources:
        if source == "mcpServers" or source.startswith("inline:"):
            documents.append((source, _manifest_document(plugin, root)))
            continue
        path = safe_package_path(root, source, require_exists=True)
        if not path.is_file():
            continue
        documents.append((source, read_json_object(root, source)))

    assets: list[PluginResourceAsset] = []
    for source, document in documents:
        raw_servers = document.get("mcpServers", document)
        if not isinstance(raw_servers, Mapping):
            continue
        for raw_name, raw_server in sorted(raw_servers.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_name, str) or not raw_name.strip() or not isinstance(raw_server, Mapping):
                continue
            try:
                if plugin.format == "qwen-code":
                    validate_qwen_mcp_server(raw_name, raw_server, root=root)
                metadata = _mcp_metadata(
                    raw_name,
                    raw_server,
                    virtual_root,
                    package_root=root,
                    strict=plugin.format == "qwen-code",
                    available_relatives=available_relatives,
                )
            except PluginError as exc:
                # 静态资源快照不能让一个坏 MCP server 阻断同包其他条目；
                # 只保留稳定 code 供 disabled/invalid preview 展示。
                metadata = {
                    "server": raw_name,
                    "transport": (
                        raw_server.get("type", "stdio")
                        if isinstance(raw_server, Mapping)
                        else "unknown"
                    ),
                    "runnable": False,
                    "diagnostic": exc.code,
                }
            if metadata is None:
                continue
            content = _json_bytes(metadata)
            safe_name = _safe_virtual_name(raw_name)
            relative = f"mcp/{safe_name}.json"
            assets.append(
                PluginResourceAsset(
                    kind="mcp",
                    source=f"{source}.{raw_name}",
                    virtual_path=f"{virtual_root}/{relative}",
                    digest=_digest(content),
                    size=len(content),
                    content=content,
                    metadata=metadata,
                )
            )
    return assets


def _mcp_metadata(
    name: str,
    raw_server: Mapping[str, object],
    virtual_root: str,
    *,
    package_root: Path,
    strict: bool,
    available_relatives: set[str],
) -> dict[str, object] | None:
    """提取不含 env/header 秘密的 MCP 静态字段，并受控解析根 placeholder。"""
    command = raw_server.get("command")
    url = raw_server.get("url", raw_server.get("httpUrl"))
    if command is not None and (not isinstance(command, str) or not command.strip()):
        return None
    if url is not None and (not isinstance(url, str) or not url.strip()):
        return None
    if command is None and url is None:
        return None
    transport = raw_server.get("type")
    if not isinstance(transport, str) or not transport.strip():
        transport = "stdio" if command is not None else "http"
    result: dict[str, object] = {
        "server": name,
        "transport": transport,
        "runnable": False,
    }
    target_paths: list[str] = []
    if command is not None:
        resolved_command = _resolve_known_placeholder(
            command,
            virtual_root,
            package_root=package_root,
            strict=strict,
            available_relatives=available_relatives,
            field="mcp.command",
        )
        result["command"] = resolved_command.value
        target_paths.extend(resolved_command.targets)
        args = raw_server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            return None
        resolved_args = [
            _resolve_known_placeholder(
                item,
                virtual_root,
                package_root=package_root,
                strict=strict,
                available_relatives=available_relatives,
                field="mcp.args",
            )
            for item in args
        ]
        result["args"] = [item.value for item in resolved_args]
        target_paths.extend(target for item in resolved_args for target in item.targets)
    if url is not None:
        resolved_url = _resolve_known_placeholder(
            url,
            virtual_root,
            package_root=package_root,
            strict=strict,
            available_relatives=available_relatives,
            field="mcp.url",
        )
        result["url"] = resolved_url.value
        target_paths.extend(resolved_url.targets)
    cwd = raw_server.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            return None
        resolved_cwd = _resolve_known_placeholder(
            cwd,
            virtual_root,
            package_root=package_root,
            strict=strict,
            available_relatives=available_relatives,
            field="mcp.cwd",
        )
        result["cwd"] = resolved_cwd.value
        target_paths.extend(resolved_cwd.targets)
    if target_paths:
        result["target_paths"] = list(dict.fromkeys(f"{virtual_root}/{target}" for target in target_paths))
    return result


def _hook_assets(
    plugin: InstalledPlugin,
    *,
    root: Path,
    virtual_root: str,
    count: int,
    available_relatives: set[str],
) -> list[PluginResourceAsset]:
    """保留 Hook matcher/command 形状，但只生成不可执行静态摘要。"""
    if plugin.manifest is None:
        return []
    document = _manifest_document(plugin, root)
    raw_hooks = document.get("hooks")
    events: list[dict[str, object]] = []
    target_paths: list[str] = []
    if isinstance(raw_hooks, Mapping):
        for raw_event, raw_rules in sorted(raw_hooks.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_event, str) or not isinstance(raw_rules, list):
                continue
            for raw_rule in raw_rules:
                if not isinstance(raw_rule, Mapping):
                    continue
                matcher = raw_rule.get("matcher")
                if matcher is not None and not isinstance(matcher, str):
                    continue
                raw_handlers = raw_rule.get("hooks")
                if not isinstance(raw_handlers, list):
                    continue
                handlers: list[dict[str, object]] = []
                for raw_handler in raw_handlers:
                    if not isinstance(raw_handler, Mapping):
                        continue
                    command = raw_handler.get("command")
                    handler_type = raw_handler.get("type")
                    if handler_type != "command" or not isinstance(command, str):
                        continue
                    resolved = _resolve_known_placeholder(
                        command,
                        virtual_root,
                        package_root=root,
                        strict=plugin.format == "qwen-code",
                        available_relatives=available_relatives,
                        field="hook.command",
                    )
                    handler: dict[str, object] = {
                        "type": "command",
                        "command": resolved.value,
                    }
                    for key in ("name", "description", "timeout"):
                        if key in raw_handler and isinstance(raw_handler[key], (str, int, float)):
                            handler[key] = raw_handler[key]
                    handlers.append(handler)
                    target_paths.extend(resolved.targets)
                if handlers:
                    event: dict[str, object] = {
                        "event": raw_event,
                        "matcher": matcher,
                        "handlers": handlers,
                    }
                    events.append(event)
    metadata = {
        "events": events,
        "handlers": count,
        "runnable": False,
    }
    if target_paths:
        metadata["target_paths"] = list(
            dict.fromkeys(f"{virtual_root}/{target}" for target in target_paths)
        )
    content = _json_bytes(metadata)
    return [
        PluginResourceAsset(
            kind="hooks",
            source="hooks",
            virtual_path=f"{virtual_root}/hooks/hooks.json",
            digest=_digest(content),
            size=len(content),
            content=content,
            metadata=metadata,
        )
    ]


def _resolve_known_placeholder(
    value: str,
    virtual_root: str,
    *,
    package_root: Path,
    strict: bool,
    available_relatives: set[str],
    field: str,
) -> _ResolvedPlaceholder:
    """逐个解析已知根 token，并证明每个文件目标存在于安全资源闭包。"""
    if not isinstance(value, str):
        raise PluginError(
            "PLUGIN_RESOURCE_PLACEHOLDER_INVALID",
            f"{field} 必须是字符串",
        )
    matches = list(_PLACEHOLDER_RE.finditer(value))
    if strict:
        unknown = _unknown_root_placeholder(value, matches)
        if unknown is not None:
            raise PluginError(
                "PLUGIN_RESOURCE_PLACEHOLDER_INVALID",
                f"{field} 含有未知 Plugin 根 token：{unknown}",
            )
        if not matches and _looks_like_host_path(value):
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                f"{field} 不能包含宿主绝对路径",
            )

    if not matches:
        return _ResolvedPlaceholder(value)

    output: list[str] = []
    targets: list[str] = []
    cursor = 0
    for match in matches:
        if match.start() < cursor:
            # 上一个 root token 已经消费了其后的 `${/}`/`${pathSeparator}`
            # 和同一路径后缀；不要再次处理被消费的嵌套 match。
            continue
        prefix = value[cursor : match.start()]
        if strict and _prefix_contains_host_path(prefix):
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                f"{field} 的 token 前缀包含宿主绝对路径",
            )
        if strict and _prefix_contains_embedded_path(prefix):
            raise PluginError(
                "PLUGIN_RESOURCE_PATH_INVALID",
                f"{field} 的 token 不是独立路径片段",
            )
        output.append(prefix)
        if match.group() in {"${/}", "${pathSeparator}"}:
            output.append("/")
            cursor = match.end()
            continue
        suffix_end = _placeholder_suffix_end(value, match.end())
        raw_suffix = value[match.end() : suffix_end]
        normalized_suffix = (
            raw_suffix
            .replace("${/}", "/")
            .replace("${pathSeparator}", "/")
            .replace("\\", "/")
        )
        target = (
            _placeholder_target(
                normalized_suffix,
                package_root=package_root,
                strict=strict,
                available_relatives=available_relatives,
                field=field,
            )
            if match.group() not in {"${workspacePath}", "<workspacePath>"}
            else None
        )
        if target is not None:
            targets.append(target)
        replacement_root = (
            "/.harness/workspace"
            if match.group() in {"${workspacePath}", "<workspacePath>"}
            else virtual_root
        )
        replacement = replacement_root + (
            "/" if match.group().endswith("${/}") else ""
        )
        output.append(replacement + normalized_suffix)
        cursor = suffix_end
    output.append(value[cursor:])
    return _ResolvedPlaceholder("".join(output), tuple(dict.fromkeys(targets)))


def _unknown_root_placeholder(
    value: str,
    known_matches: list[re.Match[str]],
) -> str | None:
    """只把路径形状的未知根 token 视为资源 token，保留 Markdown 文案占位符。"""
    known_ranges = [(match.start(), match.end()) for match in known_matches]
    for match in _UNKNOWN_PLACEHOLDER_RE.finditer(value):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in known_ranges
        ):
            continue
        token = match.group(0)
        after = value[match.end() : match.end() + 1]
        name = token.lower()
        # `<模板目录名>` 这类人类可读占位符可以出现在命令正文的工作区
        # 示例中；它不是 Plugin root token，也不会被本阶段执行或替换。
        # `${UNKNOWN_ROOT}` 等 shell/配置风格 token 仍按路径规则拒绝。
        if token.startswith("<") and not any(
            marker in name for marker in ("root", "extension", "plugin")
        ):
            continue
        if after in {"/", "\\"} or any(
            marker in name for marker in ("root", "extension", "plugin")
        ):
            return token
    return None


def _placeholder_suffix_end(value: str, start: int) -> int:
    """读取 token 后的一个路径片段，保留 `--file=` 和引号等命令前缀。"""
    index = start
    while index < len(value):
        if value.startswith("${/}", index):
            index += len("${/}")
            continue
        if value.startswith("${pathSeparator}", index):
            index += len("${pathSeparator}")
            continue
        if value.startswith("${", index) or value.startswith("<", index):
            break
        if value[index].isspace() or value[index] in {'"', "'", "`", ";", ")", ","}:
            break
        index += 1
    return index


def _placeholder_target(
    normalized_suffix: str,
    *,
    package_root: Path,
    strict: bool,
    available_relatives: set[str],
    field: str,
) -> str | None:
    """把 token 后缀转换为包内相对文件，并检查真实快照闭包。"""
    if not strict:
        return None
    relative = normalized_suffix.lstrip("/")
    if not relative:
        return None
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginError(
            "PLUGIN_RESOURCE_PATH_INVALID",
            f"{field} 的 token 目标不能越过 Plugin 根目录",
        )
    if not _safe_dependency_relative(relative):
        raise PluginError(
            "PLUGIN_RESOURCE_PATH_INVALID",
            f"{field} 的 token 目标不在允许的资源闭包中",
        )
    try:
        candidate = safe_package_path(package_root, relative, require_exists=True)
    except PluginError as exc:
        if exc.code in {"PLUGIN_COMPONENT_MISSING", "PLUGIN_COMPONENT_PATH_INVALID"}:
            raise PluginError(
                "PLUGIN_RESOURCE_TARGET_MISSING",
                f"{field} 的 token 目标不存在：{relative}",
            ) from exc
        raise
    if not candidate.is_file() or relative not in available_relatives:
        raise PluginError(
            "PLUGIN_RESOURCE_TARGET_MISSING",
            f"{field} 的 token 目标没有进入静态快照：{relative}",
        )
    return relative


def _looks_like_host_path(value: str) -> bool:
    """识别字段自身或 `--file=/...` 形式的宿主绝对路径。"""
    if "\n" in value or "\r" in value:
        return False
    stripped = value.strip().strip('"\'')
    return bool(
        stripped.startswith("/")
        or re.search(r"(?:^|[=\s])/", stripped)
        or re.match(r"^[A-Za-z]:[\\/]", stripped) is not None
    )


def _prefix_contains_host_path(prefix: str) -> bool:
    """拒绝 token 前已有 `/tmp/` 等绝对路径，避免替换后前缀绕过根校验。"""
    stripped = prefix.strip()
    return bool(
        stripped.startswith("/")
        or re.search(r"(?:^|[=:\s\"'])/[A-Za-z0-9._-]+/?$", stripped)
    )


def _prefix_contains_embedded_path(prefix: str) -> bool:
    """拒绝 `prefix/<token>` 或 `prefix<token>` 形式的虚拟根拼接绕过。"""
    if not prefix:
        return False
    return prefix[-1] not in {
        "=",
        ":",
        " ",
        "\t",
        "\n",
        "\r",
        '"',
        "'",
        "`",
        "(",
        "[",
        "{",
    }


def _manifest_document(
    plugin: InstalledPlugin,
    root: Path,
) -> Mapping[str, object]:
    """读取单一可定位 manifest，兼容 Hybrid 的组合 manifest 摘要。"""
    candidates: list[str] = []
    if plugin.manifest and " + " not in plugin.manifest:
        candidates.append(plugin.manifest)
    if plugin.format in {"claude-code", "hybrid"}:
        candidates.append(".claude-plugin/plugin.json")
    if plugin.format == "qwen-code":
        candidates.extend(("qwen-extension.json", "devagent-extension.json"))
    candidates.append("plugin.json")
    for relative in dict.fromkeys(candidates):
        path = root / relative
        if path.is_file() and not path.is_symlink():
            return read_json_object(root, relative)
    return {}


def _validate_virtual_resource_path(virtual_root: str, value: str) -> str:
    """校验并规范化快照读取路径，只允许当前虚拟 Plugin 根下的资源。"""
    if not isinstance(value, str) or not value.startswith(f"{virtual_root}/"):
        raise PluginError(
            "PLUGIN_RESOURCE_PATH_INVALID",
            "虚拟资源路径不属于当前 Plugin",
        )
    relative = value[len(virtual_root) + 1 :].replace("\\", "/")
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PluginError(
            "PLUGIN_RESOURCE_PATH_INVALID",
            "虚拟资源路径不能包含越界片段",
        )
    return f"{virtual_root}/{path.as_posix()}"


def _virtual_root(plugin_id: str) -> str:
    """将 registry Plugin ID 映射为稳定虚拟根，不接受路径语义。"""
    parts = plugin_id.split("/") if isinstance(plugin_id, str) else []
    if not parts or any(not _SAFE_NAME_RE.fullmatch(part) for part in parts):
        raise PluginError("PLUGIN_ID_INVALID", "Plugin ID 不能转换为虚拟路径")
    return "/.harness/plugins/" + "/".join(parts)


def _safe_virtual_name(value: str) -> str:
    """把 MCP 名称变成不会产生路径语义的虚拟文件名。"""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return normalized or "server"


def _snapshot_id(
    plugin: InstalledPlugin,
    counts: Mapping[str, int],
    resources: list[PluginResourceAsset],
) -> str:
    """按安装内容和静态资源摘要计算稳定 snapshot ID。"""
    payload = {
        "plugin_id": plugin.plugin_id,
        "package_digest": plugin.package_digest,
        "counts": dict(counts),
        "resources": [
            {
                "kind": asset.kind,
                "source": asset.source,
                "virtual_path": asset.virtual_path,
                "digest": asset.digest,
                "metadata": _thaw_metadata(asset.metadata),
            }
            for asset in resources
        ],
    }
    return _digest(_json_bytes(payload))[:16]


def _json_bytes(value: object) -> bytes:
    """生成稳定 UTF-8 JSON 内容。"""
    return json.dumps(
        _thaw_metadata(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freeze_metadata(value: object) -> Mapping[str, object]:
    """递归冻结静态 metadata，避免嵌套 args/events 被调用方改写。"""
    if not isinstance(value, Mapping):
        raise TypeError("Plugin resource metadata must be a mapping")
    return MappingProxyType({
        str(key): _freeze_metadata_value(item)
        for key, item in value.items()
    })


def _freeze_metadata_value(value: object) -> object:
    """冻结 metadata 的嵌套 object/list，同时保留 JSON 形状。"""
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_metadata_value(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    return value


def _thaw_metadata(value: object) -> object:
    """把冻结 metadata 复制为可序列化、与 JSON 一致的 dict/list 形状。"""
    if isinstance(value, Mapping):
        return {str(key): _thaw_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_metadata(item) for item in value]
    return value


def _digest(content: bytes) -> str:
    """计算资源内容摘要。"""
    return hashlib.sha256(content).hexdigest()
