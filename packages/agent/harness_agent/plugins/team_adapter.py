"""Harness Plugin Team Adapter：把 JSON/YAML 文件转换为固定 TeamDefinition。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from harness_agent.runtime.team_coordinator import (
    TeamDefinition,
    TeamError,
    TeamFailurePolicy,
    TeamTaskAccess,
    TeamTaskDefinition,
)


MAX_TEAM_FILE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PluginTeamAdapterResult:
    """逐项隔离后的 Team 定义和脱敏诊断。"""

    teams: tuple[TeamDefinition, ...]
    diagnostics: tuple[str, ...]


def load_plugin_teams(
    root: Path,
    *,
    sources: tuple[Path, ...],
    plugin_id: str,
) -> PluginTeamAdapterResult:
    """加载显式 sources；单个坏 Team 不阻断同包其他组件。"""
    teams: list[TeamDefinition] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    for source in sorted(sources):
        try:
            path = _inside_root(root, source)
            definition = _parse_team(path)
            if definition.team_id in seen:
                raise TeamError("TEAM_ID_DUPLICATE")
            seen.add(definition.team_id)
            teams.append(definition)
        except (OSError, ValueError, yaml.YAMLError, TeamError) as exc:
            code = exc.code if isinstance(exc, TeamError) else "TEAM_DEFINITION_INVALID"
            diagnostics.append(f'plugin:{plugin_id} team "{source.name}": {code}')
    return PluginTeamAdapterResult(tuple(teams), tuple(diagnostics))


def _parse_team(path: Path) -> TeamDefinition:
    """解析标准 TeamDefinition，拒绝未知字段和任意模板执行。"""
    data = _read_object(path)
    _reject_unknown(
        data,
        {"id", "description", "tasks", "maxParallelism", "failurePolicy"},
        "team",
    )
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise TeamError("TEAM_TASKS_INVALID")
    tasks: list[TeamTaskDefinition] = []
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise TeamError("TEAM_TASK_INVALID")
        _reject_unknown(
            raw,
            {"id", "agent", "input", "dependsOn", "access", "timeoutSeconds"},
            "team.task",
        )
        depends_on = raw.get("dependsOn", [])
        if not isinstance(depends_on, list) or not all(
            isinstance(item, str) for item in depends_on
        ):
            raise TeamError("TEAM_DEPENDENCIES_INVALID")
        access = raw.get("access", "read")
        try:
            access_mode = TeamTaskAccess(str(access))
        except ValueError as exc:
            raise TeamError("TEAM_TASK_ACCESS_INVALID") from exc
        timeout = raw.get("timeoutSeconds", 300)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise TeamError("TEAM_TASK_TIMEOUT_INVALID")
        tasks.append(
            TeamTaskDefinition(
                task_id=_required_string(raw.get("id"), "task.id"),
                agent_id=_required_string(raw.get("agent"), "task.agent"),
                input_template=_required_string(raw.get("input"), "task.input", max_length=32_768),
                depends_on=tuple(depends_on),
                access=access_mode,
                timeout_seconds=float(timeout),
            )
        )
    parallelism = data.get("maxParallelism", 4)
    if not isinstance(parallelism, int) or isinstance(parallelism, bool):
        raise TeamError("TEAM_PARALLELISM_INVALID")
    try:
        failure_policy = TeamFailurePolicy(
            str(data.get("failurePolicy", TeamFailurePolicy.FAIL_FAST))
        )
    except ValueError as exc:
        raise TeamError("TEAM_FAILURE_POLICY_INVALID") from exc
    description = data.get("description")
    return TeamDefinition(
        team_id=_required_string(data.get("id"), "team.id"),
        description=(
            _required_string(description, "team.description", max_length=2_000)
            if description is not None
            else None
        ),
        tasks=tuple(tasks),
        max_parallelism=parallelism,
        failure_policy=failure_policy,
    )


def _read_object(path: Path) -> dict[str, object]:
    """限制定义大小，只接受 JSON 或 YAML object。"""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TEAM_FILE_BYTES:
        raise TeamError("TEAM_FILE_INVALID")
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        value = json.loads(text)
    elif path.suffix in {".yaml", ".yml"}:
        value = yaml.safe_load(text)
    else:
        raise TeamError("TEAM_FILE_FORMAT_INVALID")
    if not isinstance(value, dict):
        raise TeamError("TEAM_FILE_ROOT_INVALID")
    return value


def _inside_root(root: Path, source: Path) -> Path:
    """证明 Team 文件仍位于已校验 Plugin store 根目录。"""
    resolved_root = root.resolve()
    resolved = source.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise TeamError("TEAM_FILE_OUTSIDE_PLUGIN") from exc
    return resolved


def _required_string(value: object, field: str, *, max_length: int = 128) -> str:
    """读取有界非空字符串。"""
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise TeamError("TEAM_FIELD_INVALID", field)
    return value.strip()


def _reject_unknown(
    value: Mapping[str, object],
    allowed: set[str],
    field: str,
) -> None:
    """拒绝未来字段静默进入执行语义。"""
    if set(value) - allowed:
        raise TeamError("TEAM_FIELD_UNSUPPORTED", field)
