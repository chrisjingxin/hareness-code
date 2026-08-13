"""校验随 Harness 固定打包的原版 Skill bundle。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_FORMAT_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[a-z0-9]+(?:[-_:][a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WORK_MODES = frozenset({"build", "compose"})
_MAX_MANIFEST_BYTES = 256 * 1024


class BuiltinSkillBundleError(ValueError):
    """内置 Skill bundle 缺失、结构错误或完整性校验失败。"""


@dataclass(frozen=True, slots=True)
class BuiltinSkillFile:
    """一个由 manifest 精确固定的 Skill 文件。"""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class BuiltinSkillDefinition:
    """一个不可被用户、项目或 Plugin 同名资产遮蔽的内置 Skill 定义。"""

    upstream_id: str
    canonical_id: str
    directory: str
    license_id: str
    upstream_url: str
    upstream_revision: str
    upstream_version: str
    work_modes: tuple[str, ...]
    activities: tuple[str, ...]
    files: tuple[BuiltinSkillFile, ...]

    @property
    def skill_digest(self) -> str:
        """返回入口 SKILL.md 的 manifest 摘要。"""
        return next(item.sha256 for item in self.files if item.path == "SKILL.md")


class BuiltinSkillBundle:
    """读取并严格校验 ``harness_agent/skills/builtin`` 原版资产。"""

    def __init__(self, *, root: Path | str | None = None) -> None:
        """固定 bundle 根目录，并在发布任何定义前完成全量完整性校验。"""
        configured_root = Path(root) if root is not None else Path(__file__).with_name("builtin")
        self.root = configured_root.expanduser().resolve()
        self._definitions, self._resources = self._load()

    @property
    def definitions(self) -> tuple[BuiltinSkillDefinition, ...]:
        """返回按 upstream identity 排序的已验证定义。"""
        return self._definitions

    @property
    def resources(self) -> tuple[BuiltinSkillFile, ...]:
        """返回不属于单个 Skill 目录的已验证直接引用资源。"""
        return self._resources

    def resolve(self, upstream_id: str) -> BuiltinSkillDefinition:
        """按上游固定 identity 解析一个已经完整性校验的内置 Skill。"""
        for definition in self._definitions:
            if definition.upstream_id == upstream_id:
                return definition
        raise BuiltinSkillBundleError(f'required builtin Skill "{upstream_id}" is missing')

    def verify(self) -> None:
        """重新校验磁盘内容；调用方可在 Activity 受理前 fail closed。"""
        if self._load() != (self._definitions, self._resources):
            raise BuiltinSkillBundleError("builtin Skill manifest changed after snapshot")

    def _load(self) -> tuple[tuple[BuiltinSkillDefinition, ...], tuple[BuiltinSkillFile, ...]]:
        """解析 manifest，并确认所有声明与实际打包文件一一对应。"""
        manifest_path = self.root / "manifest.json"
        raw_manifest = _read_regular_file(manifest_path, _MAX_MANIFEST_BYTES)
        payload = _decode_json(raw_manifest, "builtin Skill manifest")
        if set(payload) != {"format_version", "licenses", "resources", "skills"}:
            raise BuiltinSkillBundleError("builtin Skill manifest has unsupported fields")
        if payload.get("format_version") != _FORMAT_VERSION:
            raise BuiltinSkillBundleError("builtin Skill manifest format_version is unsupported")
        licenses = _parse_licenses(payload.get("licenses"))
        resources = _files(payload.get("resources"), label="resources", allow_empty=True)
        definitions = _parse_definitions(payload.get("skills"), licenses)
        _verify_root_layout(self.root, definitions, licenses, resources)
        return definitions, resources


def _decode_json(raw: bytes, label: str) -> dict[str, Any]:
    """解码严格 JSON，拒绝 NaN/Infinity 和非对象根节点。"""
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_raise_json_constant(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuiltinSkillBundleError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise BuiltinSkillBundleError(f"{label} must be an object")
    return value


def _raise_json_constant(value: str) -> None:
    """让 JSON decoder 将非标准数值作为结构错误。"""
    raise ValueError(f"unsupported JSON constant: {value}")


def _parse_licenses(raw: object) -> dict[str, tuple[str, str]]:
    """校验许可证文件并按稳定 ID 建立索引。"""
    if not isinstance(raw, list) or not raw:
        raise BuiltinSkillBundleError("builtin Skill manifest licenses must be a non-empty list")
    licenses: dict[str, tuple[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "path", "sha256"}:
            raise BuiltinSkillBundleError("builtin Skill license entry is invalid")
        license_id = _identifier(item["id"], "license id")
        path = _relative_path(item["path"], "license path")
        digest = _digest(item["sha256"], "license digest")
        if license_id in licenses:
            raise BuiltinSkillBundleError("builtin Skill manifest has duplicate license id")
        licenses[license_id] = (path, digest)
    return licenses


def _parse_definitions(
    raw: object,
    licenses: dict[str, tuple[str, str]],
) -> tuple[BuiltinSkillDefinition, ...]:
    """校验每个 Skill 的来源、可见性与完整文件清单。"""
    if not isinstance(raw, list) or not raw:
        raise BuiltinSkillBundleError("builtin Skill manifest skills must be a non-empty list")
    definitions: list[BuiltinSkillDefinition] = []
    upstream_ids: set[str] = set()
    canonical_ids: set[str] = set()
    directories: set[str] = set()
    expected_fields = {
        "upstream_id",
        "canonical_id",
        "directory",
        "license_id",
        "upstream",
        "work_modes",
        "activities",
        "files",
    }
    for item in raw:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise BuiltinSkillBundleError("builtin Skill definition has unsupported fields")
        upstream_id = _upstream_id(item["upstream_id"])
        canonical_id = _canonical_id(item["canonical_id"])
        directory = _relative_path(item["directory"], "Skill directory")
        if canonical_id != f"builtin/{Path(directory).name}":
            raise BuiltinSkillBundleError("builtin Skill canonical_id must match its directory")
        license_id = _identifier(item["license_id"], "Skill license_id")
        if license_id not in licenses:
            raise BuiltinSkillBundleError("builtin Skill references an unknown license")
        upstream_url, upstream_revision, upstream_version = _upstream(item["upstream"])
        work_modes = _string_list(item["work_modes"], "work_modes", allowed=_WORK_MODES)
        activities = _string_list(item["activities"], "activities")
        files = _files(item["files"], label="files")
        if not any(entry.path == "SKILL.md" for entry in files):
            raise BuiltinSkillBundleError("builtin Skill files must include SKILL.md")
        if upstream_id in upstream_ids or canonical_id in canonical_ids or directory in directories:
            raise BuiltinSkillBundleError("builtin Skill manifest has duplicate identity")
        upstream_ids.add(upstream_id)
        canonical_ids.add(canonical_id)
        directories.add(directory)
        definitions.append(
            BuiltinSkillDefinition(
                upstream_id=upstream_id,
                canonical_id=canonical_id,
                directory=directory,
                license_id=license_id,
                upstream_url=upstream_url,
                upstream_revision=upstream_revision,
                upstream_version=upstream_version,
                work_modes=work_modes,
                activities=activities,
                files=files,
            )
        )
    return tuple(sorted(definitions, key=lambda definition: definition.upstream_id))


def _verify_root_layout(
    root: Path,
    definitions: tuple[BuiltinSkillDefinition, ...],
    licenses: dict[str, tuple[str, str]],
    resources: tuple[BuiltinSkillFile, ...],
) -> None:
    """确认所有打包文件均有 digest，且没有未声明或符号链接资源。"""
    expected_files = {"manifest.json", *(path for path, _digest_value in licenses.values())}
    expected_files.update(resource.path for resource in resources)
    expected_files.update(
        f"{definition.directory}/{entry.path}"
        for definition in definitions
        for entry in definition.files
    )
    expected_directories: set[str] = set()
    for path in expected_files:
        parent = Path(path).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    try:
        entries = sorted(root.rglob("*"))
    except OSError as exc:
        raise BuiltinSkillBundleError("builtin Skill bundle root is unavailable") from exc
    for entry in entries:
        if entry.is_symlink():
            raise BuiltinSkillBundleError("builtin Skill bundle must not contain symlinks")
        if entry.is_file():
            actual_files.add(entry.relative_to(root).as_posix())
        elif entry.is_dir():
            actual_directories.add(entry.relative_to(root).as_posix())
        else:
            raise BuiltinSkillBundleError("builtin Skill bundle contains an unsupported filesystem entry")
    missing_files = expected_files - actual_files
    if missing_files:
        raise BuiltinSkillBundleError(
            f"builtin Skill file is missing: {sorted(missing_files)[0]}"
        )
    if actual_files != expected_files or actual_directories != expected_directories:
        raise BuiltinSkillBundleError("builtin Skill bundle layout does not match manifest")
    for license_path, expected_digest in licenses.values():
        _verify_file(root, license_path, expected_digest)
    for resource in resources:
        _verify_file(root, resource.path, resource.sha256)
    for definition in definitions:
        expected_files = {entry.path: entry.sha256 for entry in definition.files}
        actual_files = _collect_regular_files(root / definition.directory)
        if set(actual_files) != set(expected_files):
            raise BuiltinSkillBundleError(
                f'builtin Skill "{definition.upstream_id}" files do not match manifest'
            )
        for path, digest in expected_files.items():
            _verify_file(root / definition.directory, path, digest)


def _collect_regular_files(root: Path) -> set[str]:
    """递归列出无符号链接的普通文件，并拒绝不存在或非常规节点。"""
    if root.is_symlink() or not root.is_dir():
        raise BuiltinSkillBundleError("builtin Skill directory is unavailable")
    files: set[str] = set()
    try:
        paths = sorted(root.rglob("*"))
    except OSError as exc:
        raise BuiltinSkillBundleError("builtin Skill directory cannot be read") from exc
    for path in paths:
        if path.is_symlink():
            raise BuiltinSkillBundleError("builtin Skill bundle must not contain symlinks")
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise BuiltinSkillBundleError("builtin Skill bundle contains an unsupported filesystem entry")
    return files


def _verify_file(root: Path, relative_path: str, expected_digest: str) -> None:
    """读取一个固定相对路径并比对 SHA-256，不接受链接或路径逃逸。"""
    path = root.joinpath(*Path(relative_path).parts)
    raw = _read_regular_file(path, None)
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise BuiltinSkillBundleError(f"builtin Skill file digest mismatch: {relative_path}")


def _read_regular_file(path: Path, limit: int | None) -> bytes:
    """读取有限大小的普通文件；bundle 文件全部以 UTF-8/bytes 原样校验。"""
    if path.is_symlink() or not path.is_file():
        raise BuiltinSkillBundleError(f"builtin Skill file is missing: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BuiltinSkillBundleError(f"builtin Skill file cannot be read: {path.name}") from exc
    if limit is not None and len(raw) > limit:
        raise BuiltinSkillBundleError("builtin Skill manifest exceeds size limit")
    return raw


def _identifier(value: object, label: str) -> str:
    """校验 manifest 中的短标识符。"""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise BuiltinSkillBundleError(f"builtin Skill {label} is invalid")
    return value


def _upstream_id(value: object) -> str:
    """校验 ``publisher:skill`` 形式的上游稳定 identity。"""
    if not isinstance(value, str) or value.count(":") != 1:
        raise BuiltinSkillBundleError("builtin Skill upstream_id is invalid")
    publisher, name = value.split(":", 1)
    _identifier(publisher, "upstream publisher")
    _identifier(name, "upstream name")
    return value


def _canonical_id(value: object) -> str:
    """只接受 reserved ``builtin/<name>`` canonical identity。"""
    if not isinstance(value, str) or not value.startswith("builtin/"):
        raise BuiltinSkillBundleError("builtin Skill canonical_id is invalid")
    _identifier(value.removeprefix("builtin/"), "canonical name")
    return value


def _relative_path(value: object, label: str) -> str:
    """将 bundle 路径限制为无穿越的正向相对路径。"""
    if not isinstance(value, str) or not value:
        raise BuiltinSkillBundleError(f"builtin Skill {label} is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BuiltinSkillBundleError(f"builtin Skill {label} is unsafe")
    return path.as_posix()


def _digest(value: object, label: str) -> str:
    """校验小写 SHA-256 字符串。"""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise BuiltinSkillBundleError(f"builtin Skill {label} is invalid")
    return value


def _upstream(value: object) -> tuple[str, str, str]:
    """校验可审计的上游 URL、revision 与版本。"""
    if not isinstance(value, dict) or set(value) != {"url", "revision", "version"}:
        raise BuiltinSkillBundleError("builtin Skill upstream metadata is invalid")
    url = value.get("url")
    revision = value.get("revision")
    version = value.get("version")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BuiltinSkillBundleError("builtin Skill upstream url is invalid")
    if not isinstance(revision, str) or not revision.strip():
        raise BuiltinSkillBundleError("builtin Skill upstream revision is invalid")
    if not isinstance(version, str) or not version.strip():
        raise BuiltinSkillBundleError("builtin Skill upstream version is invalid")
    return url, revision, version


def _string_list(
    value: object,
    label: str,
    *,
    allowed: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """校验无重复的非空字符串数组，并保持 manifest 顺序。"""
    if not isinstance(value, list) or not value:
        raise BuiltinSkillBundleError(f"builtin Skill {label} must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise BuiltinSkillBundleError(f"builtin Skill {label} contains an invalid value")
    values = tuple(value)
    if len(set(values)) != len(values):
        raise BuiltinSkillBundleError(f"builtin Skill {label} contains duplicate values")
    if allowed is not None and not set(values) <= allowed:
        raise BuiltinSkillBundleError(f"builtin Skill {label} contains an unsupported value")
    return values


def _files(
    value: object,
    *,
    label: str,
    allow_empty: bool = False,
) -> tuple[BuiltinSkillFile, ...]:
    """解析一个 Skill 所有直接资源的不可变摘要表。"""
    if not isinstance(value, list) or (not value and not allow_empty):
        expectation = "a list" if allow_empty else "a non-empty list"
        raise BuiltinSkillBundleError(f"builtin Skill {label} must be {expectation}")
    files: list[BuiltinSkillFile] = []
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise BuiltinSkillBundleError(f"builtin Skill {label} entry is invalid")
        path = _relative_path(item["path"], "file path")
        if path in paths:
            raise BuiltinSkillBundleError(f"builtin Skill {label} contains duplicate paths")
        paths.add(path)
        files.append(BuiltinSkillFile(path=path, sha256=_digest(item["sha256"], "file digest")))
    return tuple(sorted(files, key=lambda entry: entry.path))
