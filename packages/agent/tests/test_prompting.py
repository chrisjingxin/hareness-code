"""稳定前缀 composer 的排序、大小和指纹回归测试。"""

from __future__ import annotations

from pathlib import Path


def _write_skill(root: Path, name: str, description: str, body: str = "正文") -> None:
    """创建一个最小合法 Skill，避免测试依赖用户目录。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: \"1\"\n---\n{body}\n",
        encoding="utf-8",
    )


def test_skill_index_is_sorted_bounded_and_does_not_leak_body(tmp_path: Path):
    """稳定索引只应携带受限元数据，正文和宿主根目录均不能出现在 system 区段。"""
    from harness_agent.skills import MAX_SKILL_INDEX_CHARS, SkillRegistry

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills", "zeta", "z" * 300, "SECRET_SKILL_BODY")
    _write_skill(workspace / ".harness" / "skills", "alpha", "a" * 300)
    registry = SkillRegistry(workspace, home=tmp_path / "home")

    index = registry.system_prompt_fragment()

    assert index.index("project/alpha") < index.index("project/zeta")
    assert len(index) <= MAX_SKILL_INDEX_CHARS
    assert "SECRET_SKILL_BODY" not in index
    assert str(workspace) not in index
    assert "a" * 131 not in index


def test_prompt_epoch_reuses_environment_snapshot_and_normalizes_tool_order(tmp_path: Path):
    """相同输入必须得到字节一致前缀，工具注册顺序不能改变 schema 指纹。"""
    from harness_agent.prompting import PromptComposer, tool_schema_fingerprint

    composer = PromptComposer("core")
    first = composer.create_epoch(
        thread_id="one",
        execution_boundary="execution",
        environment={"workspace": str(tmp_path), "approval_mode": "default"},
        readonly_memory="memory",
        skill_index="<skills />",
        tool_fingerprint=tool_schema_fingerprint(
            [{"name": "z", "description": "z", "parameters": {"b": 1, "a": 2}}, {"name": "a", "description": "a", "parameters": {}}]
        ),
        now_ms=1_000,
    )
    second = composer.create_epoch(
        thread_id="two",
        execution_boundary="execution",
        environment={"approval_mode": "default", "workspace": str(tmp_path)},
        readonly_memory="memory",
        skill_index="<skills />",
        tool_fingerprint=tool_schema_fingerprint(
            [{"name": "a", "description": "a", "parameters": {}}, {"name": "z", "description": "z", "parameters": {"a": 2, "b": 1}}]
        ),
        now_ms=2_000,
    )

    assert first.system_prompt == second.system_prompt
    assert first.environment_snapshot.snapshot_id == second.environment_snapshot.snapshot_id
    assert first.tool_schema_fingerprint == second.tool_schema_fingerprint
    assert first.system_fingerprint == second.system_fingerprint
