"""Skill 目录发现、快照完整性、资源边界和 JSON-RPC 运行链路测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest


def _write_skill(root: Path, name: str, body: str = "执行检查。", **frontmatter: object) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    values = {"name": name, "description": f"{name} skill", **frontmatter}
    header = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value}" for key, value in values.items())
    manifest = directory / "SKILL.md"
    manifest.write_text(f"---\n{header}\n---\n{body}\n", encoding="utf-8")
    return manifest


def _marketplace_archive(
    name: str,
    version: str,
    *,
    manifest: str | None = None,
    resources: dict[str, str] | None = None,
) -> bytes:
    """构造不依赖网络的企业 Skill artifact。"""
    content = manifest or (
        f"---\nname: {name}\ndescription: {name} skill\nversion: {version}\n---\n"
        "已安装正文\n"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr(f"{name}/SKILL.md", content)
        for path, value in (resources or {}).items():
            package.writestr(f"{name}/{path}", value)
    return output.getvalue()


def _marketplace_artifact(name: str, version: str, archive: bytes) -> Any:
    """把测试归档包装为已验证摘要的 Provider 返回值。"""
    from harness_agent.extensions.skills import MarketplaceArtifact

    return MarketplaceArtifact(
        market="acme",
        name=name,
        version=version,
        archive=archive,
        sha256=hashlib.sha256(archive).hexdigest(),
    )


def test_registry_scans_canonical_sources_and_rejects_ambiguous_short_names(tmp_path: Path):
    """项目、用户和内置来源保留 canonical ID，同名 Skill 不静默覆盖。"""
    from harness_agent.extensions.skills import SkillAmbiguousError, SkillRegistry

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    _write_skill(workspace / ".harness" / "skills", "review", "项目说明")
    _write_skill(home / ".harness" / "skills" / "local", "review", "用户说明")
    _write_skill(home / ".harness" / "skills" / "local", "deploy", "部署说明")

    registry = SkillRegistry(workspace, home=home)
    assert {record.skill_id for record in registry.records} == {"project/review", "user/review", "user/deploy"}
    assert registry.resolve("project/review").source == "project"
    with pytest.raises(SkillAmbiguousError):
        registry.resolve("review")


def test_registry_skips_invalid_and_symlink_manifests(tmp_path: Path):
    """非法 front matter、目录穿越和 symlink 不得进入 catalog。"""
    from harness_agent.extensions.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    skills = workspace / ".harness" / "skills"
    _write_skill(skills, "valid")
    invalid = skills / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("---\nname: invalid\nunknown: true\n---\nbody\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    linked = skills / "linked"
    linked.mkdir()
    try:
        (linked / "SKILL.md").symlink_to(outside)
    except OSError:
        pytest.skip("当前文件系统不支持 symlink")

    registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert [record.skill_id for record in registry.records] == ["project/valid"]
    assert any("invalid" in diagnostic for diagnostic in registry.diagnostics)
    assert not any("linked" in record.skill_id for record in registry.records)


def test_skill_load_checks_snapshot_digest_and_resource_boundary(tmp_path: Path):
    """正文和资源固定在旧 snapshot，路径逃逸仍 fail closed。"""
    from harness_agent.extensions.skills import SkillError, SkillRegistry

    workspace = tmp_path / "workspace"
    manifest = _write_skill(workspace / ".harness" / "skills", "review", "读取参考资料", version="1.0.0")
    resource = manifest.parent / "reference.txt"
    resource.write_text("旧参考", encoding="utf-8")
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert registry.load("project/review", "检查变更").args == "检查变更"
    assert registry.read_resource("project/review", "reference.txt") == "旧参考"
    with pytest.raises(SkillError, match="must not contain|escapes"):
        registry.read_resource("project/review", "../outside.txt")
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("读取参考资料", "新正文"),
        encoding="utf-8",
    )
    resource.write_text("新参考", encoding="utf-8")
    assert registry.load("project/review").body == "读取参考资料"
    assert registry.read_resource("project/review", "reference.txt") == "旧参考"

    next_registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert next_registry.load("project/review").body == "新正文"
    assert next_registry.read_resource("project/review", "reference.txt") == "新参考"

    manifest.unlink()
    resource.unlink()
    assert registry.load("project/review").body == "读取参考资料"
    assert registry.read_resource("project/review", "reference.txt") == "旧参考"
    with pytest.raises(SkillError, match="was not found"):
        SkillRegistry(workspace, home=tmp_path / "home").resolve("project/review")


def test_skill_file_validation_is_fd_anchored_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """校验期间替换文件不会返回旧摘要配新正文，边界输入继续拒绝。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillError

    manifest = tmp_path / "SKILL.md"
    manifest.write_text("---\nname: review\ndescription: review\n---\n旧正文\n", encoding="utf-8")
    original_read = skills_module.os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = original_read(descriptor, size)
        if content and not replaced:
            replaced = True
            manifest.write_text(
                "---\nname: review\ndescription: review\n---\n新正文\n",
                encoding="utf-8",
            )
        return content

    monkeypatch.setattr(skills_module.os, "read", replace_after_read)
    with pytest.raises(SkillError, match="changed during read"):
        skills_module._read_limited_text(manifest, skills_module.MAX_SKILL_FILE_BYTES)

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("旧文件", encoding="utf-8")
    swapped = False
    original_stat_entry = skills_module._stat_entry_at
    monkeypatch.setattr(skills_module.os, "read", original_read)

    def stat_then_replace(parent: int, name: str):
        nonlocal swapped
        identity = original_stat_entry(parent, name)
        if name == replacement.name and not swapped:
            replacement.write_text("新文件", encoding="utf-8")
            swapped = True
        return identity

    monkeypatch.setattr(skills_module, "_stat_entry_at", stat_then_replace)
    with pytest.raises(SkillError, match="changed during read"):
        skills_module._read_file_bytes(replacement, skills_module.MAX_RESOURCE_BYTES)
    assert swapped is True

    invalid_utf8 = tmp_path / "invalid.txt"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(SkillError, match="valid UTF-8"):
        skills_module._read_limited_text(invalid_utf8, skills_module.MAX_RESOURCE_BYTES)

    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (skills_module.MAX_RESOURCE_BYTES + 1))
    with pytest.raises(SkillError, match="exceeds"):
        skills_module._read_limited_text(oversized, skills_module.MAX_RESOURCE_BYTES)

    linked = tmp_path / "linked.txt"
    try:
        linked.symlink_to(manifest)
    except OSError:
        pytest.skip("当前文件系统不支持 symlink")
    with pytest.raises(SkillError, match="regular file"):
        skills_module._read_limited_text(linked, skills_module.MAX_RESOURCE_BYTES)


def test_skill_manifest_parent_swap_to_symlink_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """manifest 父目录在校验和 openat 之间换成外部 symlink 时不得越界。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillError, SkillRegistry

    workspace = tmp_path / "workspace"
    skills_root = workspace / ".harness" / "skills"
    manifest = _write_skill(skills_root, "review", "内部正文")
    saved_skill = manifest.parent.with_name("review.saved")
    outside_skill = _write_skill(tmp_path / "outside", "review", "外部正文")

    original_open_path = skills_module._open_directory_path
    original_open_at = skills_module._open_directory_at
    project_fd: int | None = None
    swapped = False

    def capture_root(path: Path):
        nonlocal project_fd
        result = original_open_path(path)
        if path == skills_root.resolve():
            project_fd = result[0]
        return result

    def swap_before_open(
        parent: int,
        name: str,
        *,
        expected: Any = None,
    ):
        nonlocal swapped
        if parent == project_fd and name == "review" and not swapped:
            manifest.parent.rename(saved_skill)
            try:
                manifest.parent.symlink_to(outside_skill.parent, target_is_directory=True)
            except OSError:
                pytest.skip("当前文件系统不支持 symlink")
            swapped = True
        return original_open_at(parent, name, expected=expected)

    monkeypatch.setattr(skills_module, "_open_directory_path", capture_root)
    monkeypatch.setattr(skills_module, "_open_directory_at", swap_before_open)

    registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert swapped is True
    assert not any(record.skill_id == "project/review" for record in registry.records)
    with pytest.raises(SkillError, match="was not found"):
        registry.resolve("project/review")
    assert outside_skill.read_text(encoding="utf-8").endswith("外部正文\n")


def test_skill_nested_directory_swap_to_symlink_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """资源子目录在 stat 和 openat 之间换成外部 symlink 时不读取外部文件。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillError, SkillRegistry

    workspace = tmp_path / "workspace"
    skills_root = workspace / ".harness" / "skills"
    manifest = _write_skill(skills_root, "review", "内部正文")
    nested = manifest.parent / "docs"
    nested.mkdir()
    (nested / "reference.txt").write_text("内部参考", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reference.txt").write_text("外部参考", encoding="utf-8")

    original_open_path = skills_module._open_directory_path
    original_open_at = skills_module._open_directory_at
    project_fd: int | None = None
    skill_fd: int | None = None
    swapped = False

    def capture_root(path: Path):
        nonlocal project_fd
        result = original_open_path(path)
        if path == skills_root.resolve():
            project_fd = result[0]
        return result

    def swap_before_open(
        parent: int,
        name: str,
        *,
        expected: Any = None,
    ):
        nonlocal skill_fd, swapped
        if parent == project_fd and name == "review":
            result = original_open_at(parent, name, expected=expected)
            skill_fd = result[0]
            return result
        if parent == skill_fd and name == "docs" and not swapped:
            nested.rename(nested.with_name("docs.saved"))
            try:
                nested.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("当前文件系统不支持 symlink")
            swapped = True
        return original_open_at(parent, name, expected=expected)

    monkeypatch.setattr(skills_module, "_open_directory_path", capture_root)
    monkeypatch.setattr(skills_module, "_open_directory_at", swap_before_open)

    registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert swapped is True
    with pytest.raises(SkillError, match="was not found|symlink|not captured|changed"):
        registry.read_resource("project/review", "docs/reference.txt")
    assert (outside / "reference.txt").read_text(encoding="utf-8") == "外部参考"


def test_manifest_accepts_claude_style_hyphenated_optional_fields(tmp_path: Path):
    """兼容常见 Claude 风格的可选 front matter 拼写，并归一化为协议字段。"""
    from harness_agent.extensions.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    _write_skill(
        workspace / ".harness" / "skills",
        "review",
        "检查",
        **{"user-invocable": False, "argument-hint": "路径"},
    )
    record = SkillRegistry(workspace, home=tmp_path / "home").inspect("project/review")
    assert record["user_invocable"] is False
    assert record["argument_hint"] == "路径"


def test_skill_catalog_manager_reuses_unchanged_snapshot_and_applies_mutations_on_refresh(
    tmp_path: Path,
):
    """catalog 无变化复用对象，内容和启停变化只在下一次 refresh 发布。"""
    from harness_agent.extensions.skills import SkillCatalogManager, SkillError

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    manifest = _write_skill(workspace / ".harness" / "skills", "review", "旧正文")
    manager = SkillCatalogManager(workspace, home=home)

    first = manager.refresh()
    assert manager.refresh() is first
    old_loaded = first.load("project/review")
    assert f'snapshot_id="{first.snapshot_id}"' in first.system_prompt_fragment()

    manifest.write_text(manifest.read_text(encoding="utf-8").replace("旧正文", "新正文"), encoding="utf-8")
    second = manager.refresh()
    assert second is not first
    assert old_loaded.body == "旧正文"
    assert second.load("project/review").body == "新正文"

    extra_manifest = _write_skill(workspace / ".harness" / "skills", "extra", "新增正文")
    third = manager.refresh()
    assert third is not second
    assert third.resolve("project/extra").name == "extra"
    extra_manifest.unlink()
    extra_manifest.parent.rmdir()
    fourth = manager.refresh()
    assert fourth is not third
    with pytest.raises(SkillError, match="was not found"):
        fourth.resolve("project/extra")

    disabled = manager.set_enabled("project/review", False)
    assert disabled["effective_on"] == "next_run"
    fifth = manager.refresh()
    assert fifth.snapshot_id != fourth.snapshot_id
    with pytest.raises(SkillError, match="was not found"):
        fifth.resolve("project/review")

    enabled = manager.set_enabled("project/review", True)
    assert enabled["effective_on"] == "next_run"
    sixth = manager.refresh()
    assert sixth.resolve("project/review").enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        "name: review\ndescription: review skill\nversion: 1.0.0\n---\n正文\n",
        "---\nname: other\ndescription: review skill\nversion: 1.0.0\n---\n正文\n",
        "---\nname: review\ndescription: review skill\nversion: 2.0.0\n---\n正文\n",
        "---\nname: review\ndescription: review skill\nversion: 1.0.0\n---\n\n",
    ],
)
async def test_marketplace_install_validates_before_replacing_existing_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: str,
):
    """非法 artifact 不推进 manager，且保留旧的有效安装。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager, SkillError

    class Provider:
        name = "acme"

        def __init__(self, artifact: Any) -> None:
            self.artifact = artifact

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    valid_archive = _marketplace_archive(
        "review",
        "1.0.0",
        resources={"reference.txt": "旧安装资源"},
    )
    provider = Provider(_marketplace_artifact("review", "1.0.0", valid_archive))
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(workspace, home=home)

    result = await manager.install("acme", "review", "1.0.0")
    assert result["effective_on"] == "next_run"
    old_registry = manager.refresh()
    assert old_registry.load("acme/review").body == "已安装正文"
    assert old_registry.read_resource("acme/review", "reference.txt") == "旧安装资源"
    assert manager._dirty is False

    invalid_archive = _marketplace_archive("review", "1.0.0", manifest=manifest)
    provider.artifact = _marketplace_artifact("review", "1.0.0", invalid_archive)
    with pytest.raises(SkillError):
        await manager.install("acme", "review", "1.0.0")

    assert manager.current is old_registry
    assert manager._dirty is False
    assert manager.refresh() is old_registry
    assert old_registry.load("acme/review").body == "已安装正文"
    assert old_registry.read_resource("acme/review", "reference.txt") == "旧安装资源"


@pytest.mark.asyncio
async def test_marketplace_install_rename_failure_rolls_back_old_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """正式目录切换失败时恢复旧目录，不发布坏 snapshot。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager, SkillError

    class Provider:
        name = "acme"
        artifact: Any

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    archive = _marketplace_archive("review", "1.0.0")
    provider = Provider()
    provider.artifact = _marketplace_artifact("review", "1.0.0", archive)
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(tmp_path / "workspace", home=tmp_path / "home")
    await manager.install("acme", "review", "1.0.0")
    old_registry = manager.refresh()
    destination = tmp_path / "home" / ".harness" / "skills" / "market" / "acme" / "review" / "1.0.0"

    replacement_archive = _marketplace_archive(
        "review",
        "1.0.0",
        resources={"reference.txt": "替换资源"},
    )
    provider.artifact = _marketplace_artifact("review", "1.0.0", replacement_archive)
    original_replace = skills_module._replace_path
    failed = False

    def fail_destination_switch(source: Path, target: Path) -> None:
        nonlocal failed
        if Path(target) == destination and not failed:
            failed = True
            raise OSError("injected rename failure")
        original_replace(source, target)

    monkeypatch.setattr(skills_module, "_replace_path", fail_destination_switch)
    with pytest.raises(SkillError, match="replacement"):
        await manager.install("acme", "review", "1.0.0")

    assert failed is True
    assert manager.current is old_registry
    assert manager._dirty is False
    assert manager.refresh() is old_registry
    assert old_registry.load("acme/review").body == "已安装正文"
    assert (destination / "SKILL.md").read_text(encoding="utf-8").endswith("已安装正文\n")

    monkeypatch.setattr(skills_module, "_replace_path", original_replace)
    result = await manager.install("acme", "review", "1.0.0")
    assert result["effective_on"] == "next_run"
    assert manager._dirty is True
    new_registry = manager.refresh()
    assert new_registry is not old_registry
    with pytest.raises(SkillError, match="not captured"):
        old_registry.read_resource("acme/review", "reference.txt")
    assert new_registry.read_resource("acme/review", "reference.txt") == "替换资源"


@pytest.mark.asyncio
async def test_marketplace_install_recovers_old_version_after_switch_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """旧目录移入 backup 后崩溃时，下一 manager 恢复完整旧版本。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager

    class Provider:
        name = "acme"
        artifact: Any

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    class SimulatedCrash(RuntimeError):
        pass

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    provider = Provider()
    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive("review", "1.0.0"),
    )
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(workspace, home=home)
    await manager.install("acme", "review", "1.0.0")
    old_registry = manager.refresh()

    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive(
            "review",
            "1.0.0",
            manifest="---\nname: review\ndescription: review skill\nversion: 1.0.0\n---\n新版本正文\n",
        ),
    )

    def failpoint(name: str) -> None:
        if name == "after_old_to_backup":
            raise SimulatedCrash("crash after old_to_backup")

    monkeypatch.setattr(skills_module, "_install_failpoint", failpoint)
    with pytest.raises(SimulatedCrash, match="old_to_backup"):
        await manager.install("acme", "review", "1.0.0")

    skills_root = home / ".harness" / "skills"
    assert (skills_root / ".skill-install-journal.json").exists()
    assert manager.current is old_registry
    assert manager._dirty is False

    recovered = SkillCatalogManager(workspace, home=home)
    registry = recovered.refresh()
    assert registry.load("acme/review").body == "已安装正文"
    assert not (skills_root / ".skill-install-journal.json").exists()
    backup_root = skills_root / ".skill-install-backups"
    assert not backup_root.exists() or not any(backup_root.iterdir())


@pytest.mark.asyncio
async def test_marketplace_install_recovers_new_version_after_target_switch_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """新目录已成为 target、清理 backup 前崩溃时，下一 manager 保留完整新版本。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager

    class Provider:
        name = "acme"
        artifact: Any

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    class SimulatedCrash(RuntimeError):
        pass

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    provider = Provider()
    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive("review", "1.0.0"),
    )
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(workspace, home=home)
    await manager.install("acme", "review", "1.0.0")
    manager.refresh()
    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive(
            "review",
            "1.0.0",
            manifest="---\nname: review\ndescription: review skill\nversion: 1.0.0\n---\n新版本正文\n",
        ),
    )

    def failpoint(name: str) -> None:
        if name == "after_new_to_target":
            raise SimulatedCrash("crash after new_to_target")

    monkeypatch.setattr(skills_module, "_install_failpoint", failpoint)
    with pytest.raises(SimulatedCrash, match="new_to_target"):
        await manager.install("acme", "review", "1.0.0")

    skills_root = home / ".harness" / "skills"
    assert (skills_root / ".skill-install-journal.json").exists()
    recovered = SkillCatalogManager(workspace, home=home)
    registry = recovered.refresh()
    assert registry.load("acme/review").body == "新版本正文"
    assert not (skills_root / ".skill-install-journal.json").exists()
    backup_root = skills_root / ".skill-install-backups"
    assert not backup_root.exists() or not any(backup_root.iterdir())


@pytest.mark.asyncio
async def test_marketplace_install_rollback_failure_is_recovered_on_next_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """正式切换和即时 rollback 都失败时，journal 仍能在下次启动恢复旧版本。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager, SkillError

    class Provider:
        name = "acme"
        artifact: Any

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    provider = Provider()
    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive("review", "1.0.0"),
    )
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(workspace, home=home)
    await manager.install("acme", "review", "1.0.0")
    old_registry = manager.refresh()
    destination = home / ".harness" / "skills" / "market" / "acme" / "review" / "1.0.0"
    provider.artifact = _marketplace_artifact(
        "review",
        "1.0.0",
        _marketplace_archive(
            "review",
            "1.0.0",
            manifest="---\nname: review\ndescription: review skill\nversion: 1.0.0\n---\n新版本正文\n",
        ),
    )
    original_replace = skills_module._replace_path
    failed_targets = 0

    def fail_switch(source: Path, target: Path) -> None:
        nonlocal failed_targets
        if Path(target) == destination:
            failed_targets += 1
            raise OSError("injected switch/rollback failure")
        original_replace(source, target)

    monkeypatch.setattr(skills_module, "_replace_path", fail_switch)
    with pytest.raises(SkillError, match="rollback"):
        await manager.install("acme", "review", "1.0.0")
    assert failed_targets == 2
    assert manager.current is old_registry
    assert manager._dirty is False

    skills_root = home / ".harness" / "skills"
    assert (skills_root / ".skill-install-journal.json").exists()
    monkeypatch.setattr(skills_module, "_replace_path", original_replace)
    recovered = SkillCatalogManager(workspace, home=home)
    registry = recovered.refresh()
    assert registry.load("acme/review").body == "已安装正文"
    assert not (skills_root / ".skill-install-journal.json").exists()
    backup_root = skills_root / ".skill-install-backups"
    assert not backup_root.exists() or not any(backup_root.iterdir())


@pytest.mark.asyncio
async def test_marketplace_install_extraction_failure_keeps_existing_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """暂存复制失败发生在正式切换前，旧版本仍然可读。"""
    import harness_agent.extensions.skills as skills_module
    from harness_agent.extensions.skills import SkillCatalogManager, SkillError

    class Provider:
        name = "acme"
        artifact: Any

        async def fetch(self, _skill: str, _version: str | None = None) -> Any:
            return self.artifact

    initial_archive = _marketplace_archive("review", "1.0.0")
    provider = Provider()
    provider.artifact = _marketplace_artifact("review", "1.0.0", initial_archive)
    monkeypatch.setattr(skills_module, "_marketplace_providers", lambda: {"acme": provider})
    manager = SkillCatalogManager(tmp_path / "workspace", home=tmp_path / "home")
    await manager.install("acme", "review", "1.0.0")
    old_registry = manager.refresh()

    replacement_archive = _marketplace_archive("review", "1.0.0")
    provider.artifact = _marketplace_artifact("review", "1.0.0", replacement_archive)
    original_write_bytes = Path.write_bytes
    failed = False

    def fail_staging_write(path: Path, data: bytes) -> int:
        nonlocal failed
        if "skill-install-" in str(path) and not failed:
            failed = True
            raise OSError("injected staging copy failure")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_staging_write)
    with pytest.raises(SkillError, match="extraction"):
        await manager.install("acme", "review", "1.0.0")

    assert failed is True
    assert manager.current is old_registry
    assert manager._dirty is False
    assert manager.refresh() is old_registry
    assert old_registry.load("acme/review").body == "已安装正文"


@pytest.mark.asyncio
async def test_explicit_skill_run_emits_loaded_event_before_content(tmp_path: Path):
    """显式 requested_skill 在正文输出前发出独立 skill.loaded 事件。"""
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills", "review", "先检查代码。")
    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocol": {"major": 3, "min_minor": 0, "max_minor": 0},
                "client": {"name": "test", "version": "0.1.0", "kind": "test"},
                "capabilities": {"requests": ["skills.read"], "handles": []},
            },
            "id": "init",
        }
    )
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "run.start",
            "params": {
                "mode": "build",
                "message": "检查变更",
                "thread_id": "thread",
                "run_id": "run",
                "requested_skill": {"id": "project/review", "args": "检查变更"},
            },
            "id": "start",
        }
    )
    for _ in range(100):
        if any(frame.get("params", {}).get("type") == "run.completed" for frame in frames):
            break
        await asyncio.sleep(0.01)
    events = [frame["params"] for frame in frames if frame.get("method") == "event"]
    assert [event["type"] for event in events] == [
        "run.started",
        "run.progress",
        "skill.loaded",
        "content.delta",
        "run.completed",
    ]
    assert events[2]["payload"]["skill_id"] == "project/review"
    assert "/.harness/skills/project/review/SKILL.md" in events[3]["payload"]["text"]


@pytest.mark.asyncio
async def test_next_run_preparation_uses_new_skill_snapshot_without_crossing_active_run(
    tmp_path: Path,
):
    """同一 Thread 的活动准备保留旧正文，后续顶层 Run 取得新 catalog。"""
    from harness_agent.host.run_coordinator import RequestedSkill, StartRun
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    manifest = _write_skill(workspace / ".harness" / "skills", "review", "第一版正文")
    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    first = await server._prepare_run(
        StartRun(mode="build", 
            thread_id="same-thread",
            run_id="run-old",
            message="检查",
            requested_skill=RequestedSkill("project/review", "旧参数"),
        ),
        None,
    )
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("第一版正文", "第二版正文"), encoding="utf-8")
    second = await server._prepare_run(
        StartRun(mode="build", 
            thread_id="same-thread",
            run_id="run-new",
            message="检查",
            requested_skill=RequestedSkill("project/review", "新参数"),
        ),
        None,
    )

    assert first.skill_registry is not None
    assert second.skill_registry is not None
    assert first.skill_registry is not second.skill_registry
    assert first.skill_snapshot_id != second.skill_snapshot_id
    assert first.requested_skill is not None and second.requested_skill is not None
    assert first.requested_skill.snapshot_id == first.skill_snapshot_id
    assert second.requested_skill.snapshot_id == second.skill_snapshot_id
    assert first.requested_skill.body == "第一版正文"
    assert second.requested_skill.body == "第二版正文"
    assert first.requested_skill.args == "旧参数"
    assert second.requested_skill.args == "新参数"
    await server.close()


@pytest.mark.asyncio
async def test_active_run_keeps_old_skill_preparation_until_next_same_thread_run(
    tmp_path: Path,
):
    """真实 RunCoordinator 生命周期中，活动 Run 不会串入下一 snapshot。"""
    from harness_agent.host.run_coordinator import (
        ConnectionRef,
        RequestedSkill,
        RunRuntime,
        StartRun,
    )
    from harness_agent.host.agent_host import AgentHost

    workspace = tmp_path / "workspace"
    manifest = _write_skill(workspace / ".harness" / "skills", "review", "活动 Run 旧正文")
    server = AgentHost(
        allow_echo=True,
        config_home=tmp_path / "home",
        workspace=workspace,
    )
    preparations: list[Any] = []
    original_prepare = server._run_coordinator._preparation_provider
    runtime_entered = asyncio.Event()
    release_runtime = asyncio.Event()

    async def capture_preparation(command: Any, persistence: Any) -> Any:
        preparation = await original_prepare(command, persistence)
        preparations.append(preparation)
        return preparation

    async def blocked_runtime(_run: Any) -> RunRuntime:
        runtime_entered.set()
        if len(preparations) == 1:
            await release_runtime.wait()

        async def release() -> None:
            return None

        return RunRuntime(
            agent=None,
            run_context=None,
            graph_config=lambda thread_id: {"configurable": {"thread_id": thread_id}},
            release=release,
        )

    server._run_coordinator._preparation_provider = capture_preparation
    server._run_coordinator._runtime_provider = blocked_runtime

    first_execution = await server._run_coordinator.start(
        StartRun(mode="build", 
            thread_id="active-thread",
            run_id="run-old",
            message="旧请求",
            requested_skill=RequestedSkill("project/review"),
        ),
        ConnectionRef("test-owner"),
    )
    first_events = asyncio.create_task(
        _collect_events(first_execution),
    )
    await runtime_entered.wait()
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("活动 Run 旧正文", "下一 Run 新正文"),
        encoding="utf-8",
    )
    release_runtime.set()
    await first_events

    second_execution = await server._run_coordinator.start(
        StartRun(mode="build", 
            thread_id="active-thread",
            run_id="run-new",
            message="新请求",
            requested_skill=RequestedSkill("project/review"),
        ),
        ConnectionRef("test-owner"),
    )
    await _collect_events(second_execution)

    assert len(preparations) == 2
    old_loaded = preparations[0].requested_skill
    new_loaded = preparations[1].requested_skill
    assert old_loaded is not None and new_loaded is not None
    assert old_loaded.body == "活动 Run 旧正文"
    assert new_loaded.body == "下一 Run 新正文"
    assert old_loaded.snapshot_id != new_loaded.snapshot_id
    await server.close()


async def _collect_events(execution: Any) -> list[Any]:
    """收集一次 Run 的终态事件，便于并发快照测试等待真实完成。"""
    return [event async for event in execution.events]
