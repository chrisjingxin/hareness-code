"""把 enabled Plugin 的 portable/Claude MCP 转换为统一 McpServerConfig。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from harness_agent.extensions.mcp import DEFAULT_CONNECT_TIMEOUT_SECONDS, McpServerConfig
from harness_agent.plugins.common import read_json_object, safe_package_path
from harness_agent.plugins.model import ExtensionCatalogSnapshot, InstalledPlugin, PluginError
from harness_agent.plugins.store import PluginStore


_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class PluginMcpLoadResult:
    """Plugin MCP 转换结果；坏 Plugin 只进入 diagnostics。"""

    servers: tuple[McpServerConfig, ...]
    diagnostics: tuple[str, ...]


def load_plugin_mcp_servers(
    catalog: ExtensionCatalogSnapshot,
    *,
    store: PluginStore,
    workspace: Path,
) -> PluginMcpLoadResult:
    """从一个不可变 catalog 读取并转换所有有效 MCP 组件。"""
    servers: list[McpServerConfig] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    for plugin in catalog.plugins:
        component = next((item for item in plugin.components if item.kind == "mcp"), None)
        if component is None or component.status not in {"supported", "adapted"}:
            continue
        try:
            store.verify_installed(plugin)
            root = store.package_path(plugin)
            data = store.data_path(plugin)
            _prepare_data_path(data)
            loaded = (
                _load_portable(plugin, root, data)
                if plugin.format in {"agent-plugins-1.0", "hybrid"}
                else _load_claude(plugin, root, data, workspace)
            )
            diagnostics.extend(
                f"plugin:{plugin.plugin_id}: {diagnostic}"
                for diagnostic in loaded.diagnostics
            )
            for server in loaded.servers:
                if server.name in seen:
                    diagnostics.append(
                        f"plugin:{plugin.plugin_id}: PLUGIN_MCP_DUPLICATE: "
                        f"MCP server {server.name} namespace 冲突"
                    )
                    continue
                seen.add(server.name)
                servers.append(server)
        except PluginError as exc:
            diagnostics.append(f"plugin:{plugin.plugin_id}: {exc.code}: {exc}")
    return PluginMcpLoadResult(
        servers=tuple(sorted(servers, key=lambda item: item.name)),
        diagnostics=tuple(diagnostics),
    )


def _load_portable(
    plugin: InstalledPlugin,
    root: Path,
    data: Path,
) -> PluginMcpLoadResult:
    """转换 Agent Plugins 1.0 固定 `mcp.json`。"""
    document = read_json_object(root, "mcp.json")
    raw_servers = document.get("mcpServers")
    if not isinstance(raw_servers, Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", "mcpServers 必须是 object")
    result: list[McpServerConfig] = []
    diagnostics: list[str] = []
    replacements = {
        "PLUGIN_ROOT": str(root),
        "PLUGIN_DATA": str(data),
    }
    for name, raw in sorted(raw_servers.items()):
        try:
            if not isinstance(name, str) or not isinstance(raw, Mapping):
                raise PluginError("PLUGIN_MCP_INVALID", "MCP server 定义无效")
            result.append(
                _server_config(
                    plugin,
                    name,
                    raw,
                    root=root,
                    replacements=replacements,
                    portable=True,
                )
            )
        except PluginError as exc:
            diagnostics.append(f"{name}: {exc.code}: {exc}")
    return PluginMcpLoadResult(tuple(result), tuple(diagnostics))


def _load_claude(
    plugin: InstalledPlugin,
    root: Path,
    data: Path,
    workspace: Path,
) -> PluginMcpLoadResult:
    """合并 Claude 默认、manifest path 和 inline `mcpServers`。"""
    merged: dict[str, object] = {}
    manifest: Mapping[str, object] = {}
    manifest_path = root / ".claude-plugin" / "plugin.json"
    if manifest_path.is_file():
        manifest = read_json_object(root, ".claude-plugin/plugin.json")
    default = root / ".mcp.json"
    if default.is_file():
        _merge_server_document(merged, read_json_object(root, ".mcp.json"))
    raw = manifest.get("mcpServers")
    if isinstance(raw, Mapping):
        _merge_server_document(merged, dict(raw))
    elif raw is not None:
        paths = (raw,) if isinstance(raw, str) else tuple(raw) if isinstance(raw, list) else ()
        if not paths or not all(isinstance(item, str) for item in paths):
            raise PluginError("PLUGIN_MCP_INVALID", "Claude mcpServers 必须是 object 或路径")
        for relative in paths:
            _merge_server_document(merged, read_json_object(root, relative))
    replacements = {
        "CLAUDE_PLUGIN_ROOT": str(root),
        "CLAUDE_PLUGIN_DATA": str(data),
        "CLAUDE_PROJECT_DIR": str(workspace),
    }
    servers: list[McpServerConfig] = []
    diagnostics: list[str] = []
    for name, raw_server in sorted(merged.items()):
        try:
            if not isinstance(name, str) or not isinstance(raw_server, Mapping):
                raise PluginError("PLUGIN_MCP_INVALID", "MCP server 定义无效")
            servers.append(
                _server_config(
                    plugin,
                    name,
                    raw_server,
                    root=root,
                    replacements=replacements,
                    portable=False,
                )
            )
        except PluginError as exc:
            diagnostics.append(f"{name}: {exc.code}: {exc}")
    return PluginMcpLoadResult(tuple(servers), tuple(diagnostics))


def _merge_server_document(target: dict[str, object], document: Mapping[str, object]) -> None:
    """兼容顶层 servers mapping 和 `{mcpServers: ...}` 包装。"""
    raw = document.get("mcpServers", document)
    if not isinstance(raw, Mapping):
        raise PluginError("PLUGIN_MCP_INVALID", "Claude MCP 配置根节点无效")
    for name, server in raw.items():
        if not isinstance(name, str) or name in target:
            raise PluginError("PLUGIN_MCP_DUPLICATE", "Claude MCP server name 重复")
        target[name] = server


def _server_config(
    plugin: InstalledPlugin,
    server_name: str,
    raw: Mapping[str, object],
    *,
    root: Path,
    replacements: Mapping[str, str],
    portable: bool,
) -> McpServerConfig:
    """转换一个 MCP server，并限制路径变量与进程环境。"""
    name = _namespaced_server_name(plugin, server_name)
    transport = raw.get("type")
    if transport is None:
        transport = "stdio" if "command" in raw else "http"
    if transport == "streamable-http":
        transport = "http"
    timeout = raw.get("timeout", DEFAULT_CONNECT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {server_name} timeout 无效")
    if transport == "stdio":
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {server_name} 缺少 command")
        command = _replace_known(command, replacements, allow_unknown=not portable)
        if portable and "${" in command:
            raise PluginError("PLUGIN_MCP_INVALID", "portable MCP command 不允许 placeholder")
        path_candidate = Path(command)
        if command.startswith("./"):
            path = safe_package_path(root, command, require_exists=True)
            command = _checked_executable(path)
        elif path_candidate.is_absolute() and _is_within(path_candidate, root):
            command = _checked_executable(path_candidate)
        args = _string_list(raw.get("args", []), f"{server_name}.args")
        env = _string_map(raw.get("env", {}), f"{server_name}.env")
        cwd_value = raw.get("cwd")
        if cwd_value is not None and not isinstance(cwd_value, str):
            raise PluginError("PLUGIN_MCP_INVALID", f"{server_name}.cwd 必须是字符串")
        cwd = (
            _replace_known(cwd_value, replacements, allow_unknown=not portable)
            if cwd_value
            else str(root)
        )
        allowed_roots = tuple(
            Path(value)
            for key, value in replacements.items()
            if key
            in {
                "PLUGIN_ROOT",
                "PLUGIN_DATA",
                "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_PLUGIN_DATA",
                "CLAUDE_PROJECT_DIR",
            }
        )
        cwd_path = Path(cwd)
        if (
            "${" in cwd
            or not cwd_path.is_absolute()
            or not any(_is_within(cwd_path, allowed) for allowed in allowed_roots)
        ):
            raise PluginError(
                "PLUGIN_MCP_CWD_INVALID",
                "Plugin MCP cwd 必须位于 Plugin root、data 或当前 workspace",
            )
        return McpServerConfig(
            name=name,
            transport="stdio",
            command=command,
            args=tuple(
                _replace_known(value, replacements, allow_unknown=not portable)
                for value in args
            ),
            env={
                key: _replace_known(value, replacements, allow_unknown=not portable)
                for key, value in env.items()
            },
            timeout_seconds=float(timeout),
            cwd=cwd,
            source=f"plugin:{plugin.plugin_id}",
            source_fingerprint=plugin.package_digest,
            inherit_environment=False,
        )
    if transport not in {"http", "sse"}:
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {server_name} transport 无效")
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise PluginError("PLUGIN_MCP_INVALID", f"MCP server {server_name} 缺少 url")
    url = _replace_known(url, replacements, allow_unknown=not portable)
    if portable and "${" in url:
        raise PluginError("PLUGIN_MCP_INVALID", "portable MCP url 不允许未知 placeholder")
    headers = _string_map(raw.get("headers", {}), f"{server_name}.headers")
    return McpServerConfig(
        name=name,
        transport=transport,  # type: ignore[arg-type]
        url=url,
        headers={
            key: _replace_known(value, replacements, allow_unknown=not portable)
            for key, value in headers.items()
        },
        timeout_seconds=float(timeout),
        source=f"plugin:{plugin.plugin_id}",
        source_fingerprint=plugin.package_digest,
        inherit_environment=False,
    )


def _replace_known(
    value: str,
    replacements: Mapping[str, str],
    *,
    allow_unknown: bool,
) -> str:
    """替换 Plugin 自有路径变量；Claude 显式环境引用留到连接时解析。"""
    unknown: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = replacements.get(name)
        if replacement is not None:
            return replacement
        unknown.add(name)
        return match.group(0)

    result = _PLACEHOLDER_RE.sub(_replace, value)
    if unknown and not allow_unknown:
        raise PluginError(
            "PLUGIN_MCP_PLACEHOLDER_INVALID",
            f"portable MCP 使用未知 placeholder：{', '.join(sorted(unknown))}",
        )
    return result


def _namespaced_server_name(plugin: InstalledPlugin, name: str) -> str:
    """生成不会覆盖用户 MCP 的稳定 ASCII namespace。"""
    segments = (plugin.source_id, plugin.name, name)
    normalized = [
        re.sub(r"[^A-Za-z0-9_]", "_", segment).strip("_")
        for segment in segments
    ]
    if any(not segment for segment in normalized):
        raise PluginError("PLUGIN_MCP_NAME_INVALID", "Plugin MCP server name 无效")
    return "plugin__" + "__".join(normalized)


def _string_list(value: object, field: str) -> tuple[str, ...]:
    """校验 MCP string list。"""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginError("PLUGIN_MCP_INVALID", f"{field} 必须是字符串数组")
    return tuple(value)


def _string_map(value: object, field: str) -> dict[str, str]:
    """校验 MCP string mapping。"""
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise PluginError("PLUGIN_MCP_INVALID", f"{field} 必须是字符串映射")
    return dict(value)


def _checked_executable(path: Path) -> str:
    """复核解析后的包内 MCP 命令是普通可执行文件。"""
    if not path.is_file() or path.is_symlink():
        raise PluginError("PLUGIN_MCP_COMMAND_INVALID", "Plugin MCP command 必须是普通文件")
    if os.name != "nt" and not os.access(path, os.X_OK):
        raise PluginError("PLUGIN_MCP_COMMAND_INVALID", "Plugin MCP command 不可执行")
    return str(path)


def _is_within(path: Path, root: Path) -> bool:
    """判断规范化路径是否位于给定边界内。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _prepare_data_path(path: Path) -> None:
    """按需创建 Plugin data，拒绝用符号链接替换持久化边界。"""
    if path.is_symlink():
        raise PluginError("PLUGIN_DATA_CORRUPT", "Plugin data 路径不能是符号链接")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise PluginError("PLUGIN_DATA_WRITE_FAILED", "无法创建 Plugin data 目录") from exc
    if not path.is_dir() or path.is_symlink():
        raise PluginError("PLUGIN_DATA_CORRUPT", "Plugin data 路径必须是普通目录")
