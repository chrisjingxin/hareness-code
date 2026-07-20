"""Skill 目录发现、快照完整性、资源边界和 JSON-RPC 运行链路测试。"""

from __future__ import annotations

import asyncio
import json
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


def test_registry_scans_canonical_sources_and_rejects_ambiguous_short_names(tmp_path: Path):
    """项目、用户和内置来源保留 canonical ID，同名 Skill 不静默覆盖。"""
    from harness_agent.skills import SkillAmbiguousError, SkillRegistry

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
    from harness_agent.skills import SkillRegistry

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
    """正文按需加载，启动后篡改和资源路径逃逸均 fail closed。"""
    from harness_agent.skills import SkillError, SkillRegistry

    workspace = tmp_path / "workspace"
    manifest = _write_skill(workspace / ".harness" / "skills", "review", "读取参考资料", version="1.0.0")
    (manifest.parent / "reference.txt").write_text("参考", encoding="utf-8")
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    assert registry.load("project/review", "检查变更").args == "检查变更"
    assert registry.read_resource("project/review", "reference.txt") == "参考"
    with pytest.raises(SkillError, match="must not contain|escapes"):
        registry.read_resource("project/review", "../outside.txt")
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("读取参考资料", "被篡改"), encoding="utf-8")
    with pytest.raises(SkillError, match="changed after startup"):
        registry.load("project/review")


def test_manifest_accepts_claude_style_hyphenated_optional_fields(tmp_path: Path):
    """兼容常见 Claude 风格的可选 front matter 拼写，并归一化为协议字段。"""
    from harness_agent.skills import SkillRegistry

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


@pytest.mark.asyncio
async def test_explicit_skill_run_emits_loaded_event_before_content(tmp_path: Path):
    """显式 requested_skill 在正文输出前发出独立 skill.loaded 事件。"""
    from harness_agent.server import JsonRpcServer

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills", "review", "先检查代码。")
    server = JsonRpcServer(allow_echo=True, config_home=tmp_path / "home")
    frames: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        frames.append(message)

    server.send = capture
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocol": {"major": 2, "min_minor": 1, "max_minor": 1},
                "client": {"name": "test", "version": "0.1.0"},
                "capabilities": ["skills.read"],
                "cwd": str(workspace),
            },
            "id": "init",
        }
    )
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "run.start",
            "params": {
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
    assert [event["type"] for event in events] == ["run.started", "skill.loaded", "content.delta", "run.completed"]
    assert events[1]["payload"]["skill_id"] == "project/review"
    assert "先检查代码。" in events[2]["payload"]["text"]
