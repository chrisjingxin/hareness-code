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
    from harness_agent.extensions.skills import MAX_SKILL_INDEX_CHARS, SkillRegistry

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


def test_embedded_context_snapshot_is_stable_and_normalizes_tool_order(tmp_path: Path):
    """直接库调用也走 ContextLifecycle，工具注册顺序不能改变快照正文。"""
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    tools = [
        {"name": "z", "description": "z", "parameters": {"b": 1, "a": 2}},
        {"name": "a", "description": "a", "parameters": {}},
    ]
    first = prepare_embedded_context_snapshot(
        thread_id="embedded",
        system_prompt="core",
        workspace=tmp_path,
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
        tools=tools,
        now_ms=1_000,
    )
    second = prepare_embedded_context_snapshot(
        thread_id="embedded",
        system_prompt="core",
        workspace=tmp_path,
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
        tools=reversed(tools),
        now_ms=2_000,
    )

    assert first.system_prompt == second.system_prompt
    assert first.system_fingerprint == second.system_fingerprint
