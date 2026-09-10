"""Python package 的目录职责与 import 方向回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "harness_agent"


def _internal_imports(path: Path) -> set[str]:
    """返回文件直接引用的 Harness module，忽略 stdlib 与第三方依赖。"""
    imports: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("harness_agent."):
                imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(
                name.name
                for name in node.names
                if name.name.startswith("harness_agent.")
            )
    return imports


def test_package_root_contains_only_entrypoints() -> None:
    """生产模块必须进入职责目录，package 根只保留两个入口。"""
    assert {path.name for path in PACKAGE_ROOT.glob("*.py")} == {
        "__init__.py",
        "__main__.py",
    }


def test_protocol_does_not_depend_on_business_modules() -> None:
    """Protocol 运行时只能依赖同目录生成物，不能反向耦合业务实现。"""
    for path in (PACKAGE_ROOT / "protocol").glob("*.py"):
        assert all(
            module.startswith("harness_agent.protocol.")
            for module in _internal_imports(path)
        ), path


def test_non_host_modules_do_not_import_host() -> None:
    """Host 是组合入口，其他生产 module 不得反向依赖它。"""
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == PACKAGE_ROOT / "__main__.py" or (PACKAGE_ROOT / "host") in path.parents:
            continue
        assert all(
            not module.startswith("harness_agent.host.")
            for module in _internal_imports(path)
        ), path


_HC138_DEAD_NAMES = (
    "ComposeWorkflow",
    "ComposeStateMachine",
    "ComposeArtifactStore",
)


def test_hc138_workflow_stack_absent_from_production() -> None:
    """HC-138 五阶段工作流已离开生产包；不得再以类名或模块形式出现。"""
    hits: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        for name in _HC138_DEAD_NAMES:
            if name in text:
                hits.append(f"{relative}:{name}")
    assert hits == []


def test_run_execution_has_no_stream_test_shims() -> None:
    """Host stream 翻译只走 execution_stream；run_execution 不再包一层测试兼容函数。"""
    text = (PACKAGE_ROOT / "host" / "run_execution.py").read_text(encoding="utf-8")
    for name in (
        "def _translate_stream_event(",
        "def _extract_interaction(",
        "def _capture_transcript_message(",
        "def _truncate_text(",
        "def _resume_value(",
    ):
        assert name not in text, name


def test_always_safe_commands_union_is_gone() -> None:
    """运行时只用 safe_commands_for_platform；不再导出跨平台并集别名。"""
    text = (PACKAGE_ROOT / "policy" / "safe_commands.py").read_text(encoding="utf-8")
    assert "ALWAYS_SAFE_COMMANDS" not in text


def test_agent_engine_profile_has_no_profile_only_wrapper() -> None:
    """调用方直接使用 ResolvedAgentSpec；不得再包一层只取 runtime_profile 的方法。"""
    text = (PACKAGE_ROOT / "host" / "agent_host.py").read_text(encoding="utf-8")
    assert "def _resolve_agent_engine_profile(" not in text


def _class_definition_sites(name: str) -> list[str]:
    """返回生产包中 `class Name` 定义点。"""
    needle = f"class {name}"
    hits: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(needle) and (
                stripped == needle
                or stripped[len(needle) : len(needle) + 1] in "(: "
            ):
                hits.append(f"{path.relative_to(PACKAGE_ROOT).as_posix()}:{index}")
    return hits


def test_skill_registry_has_one_definition() -> None:
    """Skill catalog 只有一个 SkillRegistry 类型。"""
    hits = _class_definition_sites("SkillRegistry")
    assert len(hits) == 1, hits


def test_resource_scope_has_one_definition() -> None:
    """共享资源 scope 枚举只有一个定义。"""
    hits = _class_definition_sites("ResourceScope")
    assert len(hits) == 1, hits
