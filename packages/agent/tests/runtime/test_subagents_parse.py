"""子代理 Markdown 定义解析与序列化测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_agent.runtime.subagents import (
    AgentColor,
    AgentDefinition,
    AgentSource,
    parse_agent_markdown,
    serialize_agent_markdown,
)


def _write_agent(root: Path, filename: str, content: str) -> Path:
    """将 Agent 定义写入临时目录并返回路径。"""
    agents_dir = root / ".harness" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


FULL_AGENT_MD = """\
---
name: code-reviewer
description: 审查代码变更并提供反馈
tools:
  - read_file
  - grep
disallowedTools:
  - shell
model: gpt-4o
color: blue
maxTurns: 10
background: true
---

你是一个代码审查专家。请仔细审查代码变更。
"""

MINIMAL_AGENT_MD = """\
---
name: helper
description: 通用辅助代理
---

你是一个通用辅助代理。
"""


def test_parse_valid_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """正常解析一个完整的 agent markdown。"""
    # 将 Path.home() 指向不相关目录，确保 tmp_path 被判定为项目来源
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fake-home"))
    path = _write_agent(tmp_path, "code-reviewer.md", FULL_AGENT_MD)
    defn = parse_agent_markdown(path)

    assert defn.name == "code-reviewer"
    assert defn.description == "审查代码变更并提供反馈"
    assert defn.tools == ["read_file", "grep"]
    assert defn.disallowed_tools == ["shell"]
    assert defn.model == "gpt-4o"
    assert defn.color == AgentColor.BLUE
    assert defn.max_turns == 10
    assert defn.background is True
    assert defn.system_prompt == "你是一个代码审查专家。请仔细审查代码变更。"
    assert defn.source == AgentSource.PROJECT
    assert defn.file_path == path


def test_parse_minimal_agent(tmp_path: Path):
    """只有 name + description + prompt 的最小定义。"""
    path = _write_agent(tmp_path, "helper.md", MINIMAL_AGENT_MD)
    defn = parse_agent_markdown(path)

    assert defn.name == "helper"
    assert defn.description == "通用辅助代理"
    assert defn.system_prompt == "你是一个通用辅助代理。"
    assert defn.tools is None
    assert defn.disallowed_tools is None
    assert defn.model is None
    assert defn.color is None
    assert defn.max_turns is None
    assert defn.background is False


def test_parse_missing_name_raises(tmp_path: Path):
    """缺少 name 报错。"""
    content = "---\ndescription: 没有名字\n---\n正文\n"
    path = _write_agent(tmp_path, "no-name.md", content)
    with pytest.raises(ValueError, match="缺少 name"):
        parse_agent_markdown(path)


def test_parse_invalid_color_raises(tmp_path: Path):
    """颜色不在枚举内报错。"""
    content = "---\nname: bad-color\ndescription: 颜色无效\ncolor: magenta\n---\n正文\n"
    path = _write_agent(tmp_path, "bad-color.md", content)
    with pytest.raises(ValueError, match="color"):
        parse_agent_markdown(path)


def test_parse_invalid_name_raises(tmp_path: Path):
    """name 不是 kebab-case 报错。"""
    content = "---\nname: Bad_Name\ndescription: 名称无效\n---\n正文\n"
    path = _write_agent(tmp_path, "bad-name.md", content)
    with pytest.raises(ValueError, match="kebab-case"):
        parse_agent_markdown(path)


def test_serialize_roundtrip(tmp_path: Path):
    """序列化后再解析，字段一致。"""
    original = AgentDefinition(
        name="test-agent",
        description="测试代理",
        system_prompt="你是测试代理。",
        source=AgentSource.PROJECT,
        tools=["read_file", "write_file"],
        disallowed_tools=["shell"],
        model="inherit",
        color=AgentColor.GREEN,
        max_turns=5,
        background=True,
    )
    serialized = serialize_agent_markdown(original)

    # 写入文件后重新解析
    path = _write_agent(tmp_path, "test-agent.md", serialized)
    parsed = parse_agent_markdown(path)

    assert parsed.name == original.name
    assert parsed.description == original.description
    assert parsed.system_prompt == original.system_prompt
    assert parsed.tools == original.tools
    assert parsed.disallowed_tools == original.disallowed_tools
    assert parsed.model == original.model
    assert parsed.color == original.color
    assert parsed.max_turns == original.max_turns
    assert parsed.background == original.background


def test_serialize_omits_defaults(tmp_path: Path):
    """默认值不出现在 frontmatter 中。"""
    defn = AgentDefinition(
        name="minimal",
        description="最小定义",
        system_prompt="正文内容。",
        source=AgentSource.PROJECT,
    )
    serialized = serialize_agent_markdown(defn)

    # 默认值字段不应出现
    assert "tools:" not in serialized
    assert "disallowedTools:" not in serialized
    assert "model:" not in serialized
    assert "color:" not in serialized
    assert "maxTurns:" not in serialized
    assert "background:" not in serialized

    # 必填字段应存在
    assert "name: minimal" in serialized
    assert "description: 最小定义" in serialized
