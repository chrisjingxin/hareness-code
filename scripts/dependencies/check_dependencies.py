#!/usr/bin/env python3
"""检查内网安装结果是否满足零 Anthropic 依赖约束。"""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path


FORBIDDEN = {"anthropic", "langchain-anthropic", "claude-agent-sdk"}
PATCH_ID = "harness-deepagents-0.7.3-no-anthropic-1"
EXPECTED_WHEEL_SHA256 = "fc48690259a457ddffa7da0397e575c872386f2a96e4a7a7858ee10fa842756f"
PATCHED_FILE_SHA256 = {
    "deepagents/graph.py": "0345af6b154f12a673a02085049a20c04ee8492183e99aab48a7c19ee62dd325",
    "deepagents/middleware/_prompt_caching.py": "8a77fe96c4ba5bf61913fda1f1b0779f029239ca34976877f514f8be4ef39beb",
    "deepagents/middleware/memory.py": "e19d7a48021984ae6ae0ee650136e816df2ecf4e30f096b6d96f5353d6165bb8",
    "deepagents-0.7.3.dist-info/METADATA": "1af775bc3788ac72e90695c0333503011c50031cf4d5cc4d2f3e3417d64ea50c",
}
PATCHED_FILES = (
    "graph.py",
    "middleware/_prompt_caching.py",
    "middleware/memory.py",
)


def normalized(name: str) -> str:
    """按 Python distribution 名称规则归一化。"""

    return re.sub(r"[-_.]+", "-", name).lower()


def is_forbidden(name: str) -> bool:
    """识别禁止的 Python 包及所有名称含 anthropic 的包。"""

    candidate = normalized(name)
    return candidate in FORBIDDEN or "anthropic" in candidate


def record_digest(payload: bytes) -> str:
    """返回 wheel RECORD 使用的无填充 base64url SHA-256。"""

    digest = hashlib.sha256(payload).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def sha256(payload: bytes) -> str:
    """返回制品完整 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


def check_installed() -> list[str]:
    """检查目标解释器已安装的 distribution 和 DeepAgents 源码。"""

    errors: list[str] = []
    distributions = {
        normalized(dist.metadata.get("Name", "")): dist
        for dist in importlib.metadata.distributions()
    }
    for name, distribution in sorted(distributions.items()):
        if is_forbidden(name):
            errors.append(f"forbidden installed distribution: {name}=={distribution.version}")
    deepagents = distributions.get("deepagents")
    if deepagents is None:
        errors.append("deepagents is not installed")
        return errors
    if deepagents.version != "0.7.3":
        errors.append(f"expected deepagents==0.7.3, found {deepagents.version}")
    if deepagents.metadata.get("X-Harness-Dependency-Patch") != PATCH_ID:
        errors.append("DeepAgents patch marker is missing from METADATA")
    for requirement in deepagents.metadata.get_all("Requires-Dist") or []:
        match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
        if match and is_forbidden(match.group(1)):
            errors.append(f"forbidden DeepAgents metadata dependency: {requirement}")
    package_root = Path(deepagents.locate_file("deepagents"))
    for relative in PATCHED_FILES:
        path = package_root / relative
        if not path.is_file():
            errors.append(f"missing DeepAgents source: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            errors.append(f"cannot parse DeepAgents source {relative}: {exc}")
            continue
        imported = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        if any(
            name == "anthropic"
            or name.startswith(("anthropic.", "langchain_anthropic", "langchain.anthropic"))
            for name in imported
        ):
            errors.append(f"Anthropic import remains in {relative}")
    dist_info = Path(deepagents.locate_file("deepagents-0.7.3.dist-info"))
    record_path = dist_info / "RECORD"
    if record_path.is_file():
        record_bytes = record_path.read_bytes()
        rows = csv.reader(io.StringIO(record_bytes.decode("utf-8"), newline=""))
        records = {row[0]: row[1:] for row in rows if row}
        for relative in (*[f"deepagents/{path}" for path in PATCHED_FILES], "deepagents-0.7.3.dist-info/METADATA"):
            record = records.get(relative)
            path = Path(deepagents.locate_file(relative))
            if record is None or len(record) < 2 or not record[0].startswith("sha256=") or not path.is_file():
                errors.append(f"DeepAgents RECORD entry is incomplete: {relative}")
                continue
            expected_hash = record[0].removeprefix("sha256=")
            expected_size = record[1]
            payload = path.read_bytes()
            if expected_hash != record_digest(payload) or expected_size != str(len(payload)):
                errors.append(f"DeepAgents RECORD hash/size mismatch: {relative}")
            expected_file_hash = PATCHED_FILE_SHA256.get(relative)
            if expected_file_hash is not None and sha256(payload) != expected_file_hash:
                errors.append(f"DeepAgents patched file hash mismatch: {relative}")
    else:
        errors.append("DeepAgents RECORD is missing")
    return errors


def check_lock(lock_path: Path) -> list[str]:
    """检查 uv.lock 的 package name 节点，而非任意说明文本。"""

    if not lock_path.is_file():
        return [f"missing lockfile: {lock_path}"]
    text = lock_path.read_text(encoding="utf-8")
    names = {
        normalized(match.group(1))
        for match in re.finditer(r'^name\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    }
    errors = [f"forbidden package in lockfile: {name}" for name in sorted(name for name in names if is_forbidden(name))]
    deepagents_block = re.search(
        r'^\[\[package\]\]\s+name\s*=\s*"deepagents"\n.*?(?=^\[\[package\]\]|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    if deepagents_block is None:
        errors.append("deepagents==0.7.3 package node is missing from uv.lock")
    else:
        if 'version = "0.7.3"' not in deepagents_block.group(0):
            errors.append("uv.lock deepagents package node is not version 0.7.3")
        wheel_hashes = re.findall(r'hash\s*=\s*"sha256:([0-9a-f]+)"', deepagents_block.group(0))
        if EXPECTED_WHEEL_SHA256 not in wheel_hashes:
            errors.append(
                "uv.lock deepagents wheel hash does not match the official 0.7.3 wheel "
                f"{EXPECTED_WHEEL_SHA256}"
            )
    return errors


def check_bun_lock(lock_path: Path) -> list[str]:
    """检查 Bun 锁文件中解析后的包记录，不扫描普通文本。"""

    if not lock_path.is_file():
        return [f"missing Bun lockfile: {lock_path}"]
    try:
        # Bun lock v1 is JSON-shaped but permits trailing commas.
        lock_text = re.sub(
            r",(\s*[}\]])",
            r"\1",
            lock_path.read_text(encoding="utf-8"),
        )
        document = json.loads(lock_text)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot parse Bun lockfile {lock_path}: {exc}"]
    packages = document.get("packages", {})
    names: set[str] = set()
    for record in packages.values():
        if not isinstance(record, list) or not record or not isinstance(record[0], str):
            continue
        locator = record[0]
        separator = locator.rfind("@")
        if separator > 0:
            names.add(locator[:separator])
    return [
        f"forbidden package in Bun lockfile: {name}"
        for name in sorted(name for name in names if is_forbidden(name))
    ]


def _iter_bun_package_manifests(node_modules: Path) -> Iterator[Path]:
    """遍历实际安装链接，跳过未链接的 Bun 缓存并防止符号链接循环。"""

    visited_node_modules: set[Path] = set()

    def resolved_directory(path: Path) -> Path | None:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        return resolved if resolved.is_dir() else None

    def package_manifests(package_path: Path) -> Iterator[Path]:
        if not package_path.is_dir() and not package_path.is_symlink():
            return
        try:
            manifest = package_path / "package.json"
            if manifest.is_file():
                yield manifest
            nested = package_path / "node_modules"
            if nested.is_dir() or nested.is_symlink():
                yield from node_modules_manifests(nested)
        except OSError:
            return

    def scope_manifests(scope_path: Path) -> Iterator[Path]:
        if not scope_path.is_dir() and not scope_path.is_symlink():
            return
        try:
            direct_manifest = scope_path / "package.json"
            if direct_manifest.is_file():
                yield direct_manifest
            for package_path in scope_path.iterdir():
                yield from package_manifests(package_path)
        except OSError:
            return

    def node_modules_manifests(directory: Path) -> Iterator[Path]:
        resolved = resolved_directory(directory)
        if resolved is None or resolved in visited_node_modules:
            return
        visited_node_modules.add(resolved)
        try:
            for entry in directory.iterdir():
                # .bun/.old_modules 是缓存；只有从普通 node_modules 名称
                # 链接进去的包才算实际安装结果。失效链接自然跳过。
                if entry.name in {".bun", ".old_modules", ".bin"}:
                    continue
                if entry.name.startswith("@"):
                    yield from scope_manifests(entry)
                else:
                    yield from package_manifests(entry)
        except OSError:
            return

    yield from node_modules_manifests(node_modules)


def check_bun_installed(root: Path) -> list[str]:
    """检查各 workspace 实际可加载的 npm 包清单。"""

    errors: list[str] = []
    seen_manifests: set[Path] = set()
    for node_modules in (root / "node_modules", root / "packages/cli/node_modules"):
        for manifest in _iter_bun_package_manifests(node_modules):
            try:
                resolved_manifest = manifest.resolve(strict=True)
            except OSError:
                continue
            if resolved_manifest in seen_manifests:
                continue
            seen_manifests.add(resolved_manifest)
            try:
                package = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = package.get("name")
            if isinstance(name, str) and is_forbidden(name):
                version = package.get("version", "unknown")
                errors.append(f"forbidden installed npm package: {name}@{version} ({manifest})")
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path(__file__).parents[2] / "packages/agent/uv.lock")
    parser.add_argument("--bun-lock", type=Path, default=Path(__file__).parents[2] / "bun.lock")
    args = parser.parse_args(argv)
    root = Path(__file__).parents[2]
    errors = [
        *check_installed(),
        *check_lock(args.lock),
        *check_bun_lock(args.bun_lock),
        *check_bun_installed(root),
    ]
    if errors:
        for error in errors:
            print(f"dependency check failed: {error}", file=sys.stderr)
        return 1
    print("Dependency check passed: no forbidden Anthropic distributions or lock nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
