"""HC-140 内置原版 Skill bundle 的完整性与解析边界测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def _write_bundle(root: Path, *, resource: str = "参考资料") -> None:
    """写入最小 bundle fixture，供 manifest 完整性失败路径复用。"""
    skill_root = root / "grill-me"
    skill_root.mkdir(parents=True)
    manifest = skill_root / "SKILL.md"
    manifest.write_text(
        "---\nname: grill-me\ndescription: 需求澄清\n---\n执行访谈。\n",
        encoding="utf-8",
    )
    reference = skill_root / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text(resource, encoding="utf-8")
    license_file = root / "licenses" / "upstream-MIT.txt"
    license_file.parent.mkdir()
    license_file.write_text("MIT License\n", encoding="utf-8")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "licenses": [
                    {
                        "id": "upstream-mit",
                        "path": "licenses/upstream-MIT.txt",
                        "sha256": digest(license_file),
                    }
                ],
                "resources": [],
                "skills": [
                    {
                        "upstream_id": "mattpocock:grill-me",
                        "canonical_id": "builtin/grill-me",
                        "directory": "grill-me",
                        "license_id": "upstream-mit",
                        "upstream": {
                            "url": "https://example.test/skills",
                            "revision": "test-revision",
                            "version": "1.0.0",
                        },
                        "work_modes": ["compose"],
                        "activities": ["task"],
                        "files": [
                            {"path": "SKILL.md", "sha256": digest(manifest)},
                            {
                                "path": "references/guide.md",
                                "sha256": digest(reference),
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_builtin_bundle_exposes_reserved_compose_skill() -> None:
    """原版 bundle 以固定 identity 发布，并记录来源、许可和完整文件摘要。"""
    from harness_agent.skills.builtin_catalog import BuiltinSkillBundle

    bundle = BuiltinSkillBundle()
    definition = bundle.resolve("mattpocock:grill-me")

    assert {item.upstream_id for item in bundle.definitions} == {
        "mattpocock:grill-me",
        "agent-skills:spec-driven-development",
        "mattpocock:codebase-design",
        "agent-skills:planning-and-task-breakdown",
        "agent-skills:test-driven-development",
        "mattpocock:diagnosing-bugs",
        "agent-skills:code-review-and-quality",
    }
    assert definition.canonical_id == "builtin/grill-me"
    assert definition.work_modes == ("compose",)
    assert definition.activities == ("task",)
    assert definition.upstream_url.startswith("https://")
    assert definition.upstream_revision
    assert definition.license_id
    assert {entry.path for entry in definition.files} >= {"SKILL.md"}


def test_builtin_bundle_rejects_tampered_direct_resource(tmp_path: Path) -> None:
    """manifest 声明的任意直接资源缺失或被改写都不能发布为可用 Skill。"""
    from harness_agent.skills.builtin_catalog import BuiltinSkillBundle, BuiltinSkillBundleError

    _write_bundle(tmp_path)
    bundle = BuiltinSkillBundle(root=tmp_path)
    assert bundle.resolve("mattpocock:grill-me").directory == "grill-me"

    (tmp_path / "grill-me" / "references" / "guide.md").write_text(
        "已被篡改",
        encoding="utf-8",
    )
    with pytest.raises(BuiltinSkillBundleError, match="digest"):
        BuiltinSkillBundle(root=tmp_path)


def test_builtin_bundle_rejects_missing_declared_skill_file(tmp_path: Path) -> None:
    """任一 manifest 声明文件缺失时，bundle 不能发布部分可用的 Skill。"""
    from harness_agent.skills.builtin_catalog import BuiltinSkillBundle, BuiltinSkillBundleError

    _write_bundle(tmp_path)
    (tmp_path / "grill-me" / "SKILL.md").unlink()

    with pytest.raises(BuiltinSkillBundleError, match="missing"):
        BuiltinSkillBundle(root=tmp_path)


def test_registry_uses_reserved_builtin_identity_and_manifest_mode_visibility(
    tmp_path: Path,
) -> None:
    """项目同名 Skill 不得遮蔽 Compose required Skill，Build 也不能绕过 manifest visibility。"""
    from harness_agent.extensions.plugin_skills import SkillError, SkillRegistry

    workspace = tmp_path / "workspace"
    project_skill = workspace / ".harness" / "skills" / "grill-me"
    project_skill.mkdir(parents=True)
    (project_skill / "SKILL.md").write_text(
        "---\nname: grill-me\ndescription: 项目同名 Skill\n---\n项目正文。\n",
        encoding="utf-8",
    )

    registry = SkillRegistry(workspace, home=tmp_path / "home")
    required = registry.resolve_builtin_required(
        "mattpocock:grill-me",
        work_mode="compose",
        activity="task",
    )

    assert required.skill_id == "builtin/grill-me"
    assert registry.load(required.skill_id).body == "Run a `/grilling` session."
    assert registry.resolve("project/grill-me").skill_id == "project/grill-me"
    with pytest.raises(SkillError, match="not available"):
        registry.resolve_builtin_required(
            "mattpocock:grill-me",
            work_mode="build",
            activity="task",
        )


def test_registry_reads_manifest_declared_shared_resource_for_reserved_builtin(
    tmp_path: Path,
) -> None:
    """原版 Skill 的相对引用仅可读取 manifest 固定的共享资源快照。"""
    from harness_agent.extensions.plugin_skills import SkillRegistry

    registry = SkillRegistry(tmp_path / "workspace", home=tmp_path / "home")
    required = registry.resolve_builtin_required(
        "agent-skills:test-driven-development",
        work_mode="compose",
        activity="implement",
    )

    assert "Jest" in registry.read_resource(
        required.skill_id,
        "../../references/testing-patterns.md",
    )
