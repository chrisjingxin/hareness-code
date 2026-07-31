"""权限规则匹配、评估与持久化行为回归测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness_agent.policy.permission_rules import (
    PermissionRule,
    evaluate_rules,
    load_rules,
    matches_pattern,
    save_rule,
)


# ---------------------------------------------------------------------------
# matches_pattern
# ---------------------------------------------------------------------------


def test_matches_pattern_exact_match():
    """精确字符串完全匹配。"""
    assert matches_pattern("execute", "execute")
    assert matches_pattern("src/main.py", "src/main.py")


def test_matches_pattern_star_wildcard():
    """星号通配符匹配任意字符序列（含空序列）。"""
    assert matches_pattern("*", "anything")
    assert matches_pattern("*.py", "main.py")
    assert matches_pattern("src/*", "src/agent.py")
    assert matches_pattern("src/*", "src/")


def test_matches_pattern_question_wildcard():
    """问号通配符匹配恰好一个字符。"""
    assert matches_pattern("file?.txt", "file1.txt")
    assert matches_pattern("file?.txt", "fileA.txt")
    assert not matches_pattern("file?.txt", "file10.txt")


def test_matches_pattern_no_match():
    """模式与值不一致时返回 False。"""
    assert not matches_pattern("execute", "read")
    assert not matches_pattern("*.py", "main.js")
    assert not matches_pattern("src/*", "lib/agent.py")


# ---------------------------------------------------------------------------
# evaluate_rules
# ---------------------------------------------------------------------------


def test_evaluate_rules_last_match_wins():
    """后定义的规则覆盖先定义的同范围规则（最后匹配优先）。"""
    rules = [
        PermissionRule(tool="execute", resource="*", effect="deny"),
        PermissionRule(tool="execute", resource="*", effect="allow"),
    ]
    assert evaluate_rules("execute", "rm -rf /", rules) == "allow"


def test_evaluate_rules_no_match_returns_none():
    """无任何规则匹配时返回 None。"""
    rules = [
        PermissionRule(tool="execute", resource="*.sh", effect="allow"),
    ]
    assert evaluate_rules("read", "file.txt", rules) is None


def test_evaluate_rules_deny_takes_precedence_when_last():
    """deny 规则位于列表末尾时优先生效。"""
    rules = [
        PermissionRule(tool="*", resource="*", effect="allow"),
        PermissionRule(tool="execute", resource="*", effect="deny"),
    ]
    assert evaluate_rules("execute", "dangerous", rules) == "deny"
    # 非 execute 工具仍命中通配 allow
    assert evaluate_rules("read", "file.txt", rules) == "allow"


# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------


def test_load_rules_missing_file_returns_empty(tmp_path: Path):
    """配置文件不存在时所有层级返回空列表。"""
    result = load_rules(project_dir=tmp_path / "nonexistent")
    assert result["project"] == []
    assert result["session"] == []


def test_load_rules_reads_project_permissions(tmp_path: Path):
    """正常加载 project 层级的 permissions 数组。"""
    settings_dir = tmp_path / ".harness"
    settings_dir.mkdir(parents=True)
    settings_file = settings_dir / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "permissions": [
                    {"tool": "execute", "resource": "*", "effect": "ask"},
                    {"tool": "read", "resource": "*.py", "effect": "allow"},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = load_rules(project_dir=tmp_path)
    assert len(result["project"]) == 2
    assert result["project"][0] == PermissionRule(tool="execute", resource="*", effect="ask")
    assert result["project"][1] == PermissionRule(tool="read", resource="*.py", effect="allow")


def test_load_rules_malformed_json_returns_empty(tmp_path: Path):
    """JSON 格式错误时静默返回空列表，不抛异常。"""
    settings_dir = tmp_path / ".harness"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text("{invalid json!!", encoding="utf-8")

    result = load_rules(project_dir=tmp_path)
    assert result["project"] == []


def test_load_rules_skips_invalid_entries(tmp_path: Path):
    """permissions 数组中字段缺失或类型错误的条目被跳过。"""
    settings_dir = tmp_path / ".harness"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "permissions": [
                    {"tool": "execute", "resource": "*", "effect": "allow"},
                    {"tool": 123, "resource": "*", "effect": "allow"},
                    {"tool": "read", "resource": "*", "effect": "invalid_effect"},
                    "not_a_dict",
                ]
            }
        ),
        encoding="utf-8",
    )

    result = load_rules(project_dir=tmp_path)
    assert len(result["project"]) == 1
    assert result["project"][0].tool == "execute"


# ---------------------------------------------------------------------------
# save_rule
# ---------------------------------------------------------------------------


def test_save_rule_writes_to_project_scope(tmp_path: Path):
    """save_rule 将规则追加到 project 层级的 settings.json。"""
    rule = PermissionRule(tool="execute", resource="*.sh", effect="ask")
    save_rule(rule, scope="project", project_dir=tmp_path)

    settings_file = tmp_path / ".harness" / "settings.json"
    assert settings_file.exists()

    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["permissions"] == [
        {"tool": "execute", "resource": "*.sh", "effect": "ask"}
    ]


def test_save_rule_appends_to_existing_permissions(tmp_path: Path):
    """多次 save_rule 追加而非覆盖已有规则。"""
    save_rule(
        PermissionRule(tool="read", resource="*", effect="allow"),
        scope="project",
        project_dir=tmp_path,
    )
    save_rule(
        PermissionRule(tool="execute", resource="*", effect="deny"),
        scope="project",
        project_dir=tmp_path,
    )

    data = json.loads(
        (tmp_path / ".harness" / "settings.json").read_text(encoding="utf-8")
    )
    assert len(data["permissions"]) == 2
    assert data["permissions"][0]["tool"] == "read"
    assert data["permissions"][1]["tool"] == "execute"


def test_save_rule_user_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """save_rule 将规则写入 user 层级的 ~/.harness/settings.json。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    rule = PermissionRule(tool="*", resource="*", effect="ask")
    save_rule(rule, scope="user")

    settings_file = tmp_path / ".harness" / "settings.json"
    assert settings_file.exists()

    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data["permissions"] == [{"tool": "*", "resource": "*", "effect": "ask"}]


def test_save_rule_session_scope_does_not_write(tmp_path: Path):
    """scope=session 时不产生任何文件写入。"""
    rule = PermissionRule(tool="execute", resource="*", effect="allow")
    save_rule(rule, scope="session", project_dir=tmp_path)

    assert not (tmp_path / ".harness").exists()
