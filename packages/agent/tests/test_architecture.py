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
