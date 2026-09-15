#!/usr/bin/env python3
"""给 DeepAgents 0.7.3 应用 Harness 的无 Anthropic 运行时补丁。"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PATCH_ID = "harness-deepagents-0.7.3-no-anthropic-1"
EXPECTED_VERSION = "0.7.3"
DIST_NAME = "deepagents"
EXPECTED_WHEEL_SHA256 = "fc48690259a457ddffa7da0397e575c872386f2a96e4a7a7858ee10fa842756f"

# 这些值来自官方 deepagents==0.7.3 wheel；不能用安装后自行改写的
# RECORD 反推输入制品。目标源码/元数据必须独立匹配已知状态，RECORD
# 只逐项校验这些文件的哈希和大小。
ORIGINAL_FILE_SHA256 = {
    "deepagents/graph.py": "fb08f3cae27492c39febb4a41dd7b1ae9ee98a71872c12595bf20d893dc132ef",
    "deepagents/middleware/_prompt_caching.py": "a94072281f2e8e563102273e50193af889130f9545afdf53eeaf616e0e117a17",
    "deepagents/middleware/memory.py": "21cdd92ea4aa8c09cd06d1c9e112c0a56af6b37e68b6dc8f69bf303bd6cf7a6e",
    "deepagents-0.7.3.dist-info/METADATA": "9517d7cc5583738dc5b3efbf7848edd6af90efb6a0a227451097bad3486f446d",
}
PATCHED_FILE_SHA256 = {
    "deepagents/graph.py": "0345af6b154f12a673a02085049a20c04ee8492183e99aab48a7c19ee62dd325",
    "deepagents/middleware/_prompt_caching.py": "8a77fe96c4ba5bf61913fda1f1b0779f029239ca34976877f514f8be4ef39beb",
    "deepagents/middleware/memory.py": "e19d7a48021984ae6ae0ee650136e816df2ecf4e30f096b6d96f5353d6165bb8",
    "deepagents-0.7.3.dist-info/METADATA": "1af775bc3788ac72e90695c0333503011c50031cf4d5cc4d2f3e3417d64ea50c",
}


class PatchError(RuntimeError):
    """补丁前置条件或文件内容不符合预期。"""


@dataclass(frozen=True)
class FileChange:
    """一个待写入的站点包文件。"""

    path: Path
    content: str


def _digest(content: bytes) -> str:
    """计算 RECORD 要求的无填充 base64url SHA-256。"""

    return base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")


def _sha256(content: bytes) -> str:
    """返回文件完整 SHA-256，供制品状态校验使用。"""

    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    """在目标目录中原子替换文件，避免留下半写入源码。"""

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _remove_bytecode(path: Path) -> None:
    """删除目标源码对应的旧字节码，避免解释器复用未修补版本。"""

    cache = path.parent / "__pycache__"
    for candidate in cache.glob(f"{path.stem}.*.pyc"):
        candidate.unlink()


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    """只允许替换一次，防止包内容漂移后误改相似文本。"""

    count = source.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one match, found {count}")
    return source.replace(old, new, 1)


def _validate_hashes(site_packages: Path, expected: dict[str, str], label: str) -> None:
    """验证目标文件完整哈希，拒绝漂移或部分修补状态。"""

    for relative, digest in expected.items():
        path = site_packages / relative
        if not path.is_file():
            raise PatchError(f"{label}: missing {relative}")
        actual = _sha256(path.read_bytes())
        if actual != digest:
            raise PatchError(f"{label}: {relative} hash mismatch (expected {digest}, found {actual})")


def _validate_contents(
    changes: Iterable[FileChange], site_packages: Path, expected: dict[str, str], label: str
) -> None:
    """验证待写入内容与登记的修补状态完全一致。"""

    for change in changes:
        relative = change.path.relative_to(site_packages).as_posix()
        digest = expected.get(relative)
        if digest is None:
            continue
        actual = _sha256(change.content.encode("utf-8"))
        if actual != digest:
            raise PatchError(f"{label}: generated {relative} hash mismatch (expected {digest}, found {actual})")


def _record_rows(record: str) -> dict[str, list[str]]:
    """读取 RECORD 行，并拒绝同一路径重复登记。"""

    rows = list(csv.reader(io.StringIO(record, newline="")))
    indexed: dict[str, list[str]] = {}
    for row in rows:
        if not row:
            continue
        if row[0] in indexed:
            raise PatchError(f"DeepAgents RECORD has duplicate entry: {row[0]}")
        indexed[row[0]] = row
    return indexed


def _validate_record_entries(
    site_packages: Path, record: str, expected: dict[str, str], label: str
) -> None:
    """逐项核对目标文件内容与 RECORD 登记，不依赖 RECORD 文件整体哈希。"""

    rows = _record_rows(record)
    for relative, expected_digest in expected.items():
        row = rows.get(relative)
        path = site_packages / relative
        if row is None or len(row) < 3 or not path.is_file():
            raise PatchError(f"{label}: RECORD entry is incomplete: {relative}")
        payload = path.read_bytes()
        expected_record = f"sha256={_digest(payload)}"
        if row[1] != expected_record or row[2] != str(len(payload)):
            raise PatchError(f"{label}: RECORD hash/size mismatch: {relative}")
        actual_digest = _sha256(payload)
        if actual_digest != expected_digest:
            raise PatchError(f"{label}: {relative} content hash mismatch")


def _validate_record_contents(
    site_packages: Path, record: str, payloads: dict[str, bytes], label: str
) -> None:
    """校验尚未写入磁盘的补丁 RECORD 输出。"""

    rows = _record_rows(record)
    for relative, payload in payloads.items():
        row = rows.get(relative)
        if row is None or len(row) < 3:
            raise PatchError(f"{label}: RECORD entry is incomplete: {relative}")
        if row[1] != f"sha256={_digest(payload)}" or row[2] != str(len(payload)):
            raise PatchError(f"{label}: RECORD hash/size mismatch: {relative}")


def _patch_graph(source: str) -> str:
    """删除 ChatAnthropic 导入和默认 Claude 模型。"""

    marker = "# Harness dependency patch: provider-independent import path."
    if marker in source:
        return source
    source = _replace_once(
        source,
        "from langchain_anthropic import ChatAnthropic\n",
        f"{marker}\n",
        "deepagents/graph.py Anthropic import",
    )
    source = _replace_once(
        source,
        '''def _build_default_model() -> ChatAnthropic:\n    """Construct the default model without emitting a deprecation warning.\n\n    Internal helper used by `create_deep_agent` so the parameter-level\n    `model=None` warning isn't paired with a separate function-level warning\n    from `get_default_model`. Direct user calls go through `get_default_model`,\n    which keeps its decorator and warns once per process.\n    """\n    return ChatAnthropic(model_name="claude-sonnet-4-6")\n''',
        '''def _build_default_model() -> BaseChatModel:\n    """Reject implicit provider selection in Harness Code."""\n    raise ValueError(\n        "Deep Agents requires an explicit model; Harness Code does not provide "\n        "a provider-specific default model."\n    )\n''',
        "deepagents/graph.py default model",
    )
    source = _replace_once(
        source,
        "def get_default_model() -> ChatAnthropic:",
        "def get_default_model() -> BaseChatModel:",
        "deepagents/graph.py default model annotation",
    )
    return source


def _patch_prompt_caching(source: str) -> str:
    """保留可选供应商缓存，删除 Anthropic 缓存中间件。"""

    marker = "# Harness dependency patch: provider-independent prompt caching."
    if marker in source:
        return source
    source = _replace_once(
        source,
        "from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware\n",
        f"{marker}\n",
        "deepagents/middleware/_prompt_caching.py Anthropic import",
    )
    source = _replace_once(
        source,
        '    middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))\n',
        "",
        "deepagents/middleware/_prompt_caching.py Anthropic middleware",
    )
    return source


def _patch_memory(source: str) -> str:
    """保留记忆提示词注入，删除 Anthropic 专用 cache_control 分支。"""

    marker = "# Harness dependency patch: provider-independent memory middleware."
    if marker in source:
        return source
    source = _replace_once(
        source,
        "from langchain_anthropic import ChatAnthropic\n",
        f"{marker}\n",
        "deepagents/middleware/memory.py Anthropic import",
    )
    source = _replace_once(
        source,
        "from langchain_core.messages import ContentBlock, SystemMessage",
        "from langchain_core.messages import SystemMessage",
        "deepagents/middleware/memory.py ContentBlock import",
    )
    start_marker = "        # Runtime check uses `request.model`"
    end_marker = "        if new_system_message is request.system_message:"
    start = source.find(start_marker)
    end = source.find(end_marker, start + len(start_marker)) if start >= 0 else -1
    if start < 0 or end < 0:
        raise PatchError("deepagents/middleware/memory.py cache control block is missing")
    source = source[:start] + source[end:]
    return source


def _metadata_without_anthropic(source: str) -> str:
    """删除 DeepAgents 的硬 Anthropic Requires-Dist 并写入补丁标识。"""

    marker = f"X-Harness-Dependency-Patch: {PATCH_ID}"
    lines = source.splitlines(keepends=True)
    filtered = [
        line
        for line in lines
        if not re.match(r"^Requires-Dist:\s*(?:langchain-anthropic|anthropic)(?:[<>=!~; ]|$)", line, re.IGNORECASE)
    ]
    if len(filtered) == len(lines) and marker not in source:
        raise PatchError("DeepAgents METADATA has no expected Anthropic Requires-Dist")
    if marker not in "".join(filtered):
        insert_at = next((i + 1 for i, line in enumerate(filtered) if line.lower().startswith("version:")), len(filtered))
        filtered.insert(insert_at, f"{marker}\n")
    return "".join(filtered)


def _update_record(record: str, changes: Iterable[FileChange], dist_info: Path, site_packages: Path) -> str:
    """按修改后的 bytes 更新 wheel RECORD，而不改变其他记录。"""

    changed: dict[str, bytes] = {
        path.relative_to(site_packages).as_posix(): content.encode("utf-8")
        for path, content in ((change.path, change.content) for change in changes)
    }
    rows = list(csv.reader(io.StringIO(record, newline="")))
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for row in rows:
        if not row:
            continue
        relative = row[0]
        if relative in changed:
            payload = changed[relative]
            row = [relative, f"sha256={_digest(payload)}", str(len(payload))]
        writer.writerow(row)
    return output.getvalue()


def _site_packages_for_python(python: str) -> Path:
    """通过目标解释器获取 site-packages，避免使用执行脚本的解释器。"""

    result = subprocess.run(
        [python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def patch_site_packages(site_packages: Path) -> bool:
    """给目标 site-packages 打补丁，返回是否发生了变更。"""

    dist_infos = sorted(site_packages.glob("deepagents-*.dist-info"))
    if len(dist_infos) != 1:
        raise PatchError(f"expected one DeepAgents dist-info, found {len(dist_infos)}")
    dist_info = dist_infos[0]
    metadata_path = dist_info / "METADATA"
    record_path = dist_info / "RECORD"
    package_root = site_packages / "deepagents"
    if not metadata_path.is_file() or not record_path.is_file() or not package_root.is_dir():
        raise PatchError("DeepAgents installation is incomplete")
    metadata_text = metadata_path.read_text(encoding="utf-8")
    name_match = re.search(r"^Name:\s*(.+)$", metadata_text, re.MULTILINE | re.IGNORECASE)
    version_match = re.search(r"^Version:\s*(.+)$", metadata_text, re.MULTILINE | re.IGNORECASE)
    if not name_match or name_match.group(1).strip().lower() != DIST_NAME:
        raise PatchError("target dist-info is not deepagents")
    if not version_match or version_match.group(1).strip() != EXPECTED_VERSION:
        raise PatchError(f"expected deepagents=={EXPECTED_VERSION}, found {version_match.group(1).strip() if version_match else 'unknown'}")

    record_text = record_path.read_text(encoding="utf-8")
    if f"X-Harness-Dependency-Patch: {PATCH_ID}" in metadata_text:
        _validate_hashes(site_packages, PATCHED_FILE_SHA256, "patched DeepAgents")
        _validate_record_entries(site_packages, record_text, PATCHED_FILE_SHA256, "patched DeepAgents")
        return False
    _validate_hashes(site_packages, ORIGINAL_FILE_SHA256, "original DeepAgents wheel")
    _validate_record_entries(site_packages, record_text, ORIGINAL_FILE_SHA256, "original DeepAgents wheel")

    targets = {
        package_root / "graph.py": _patch_graph((package_root / "graph.py").read_text(encoding="utf-8")),
        package_root / "middleware" / "_prompt_caching.py": _patch_prompt_caching(
            (package_root / "middleware" / "_prompt_caching.py").read_text(encoding="utf-8")
        ),
        package_root / "middleware" / "memory.py": _patch_memory(
            (package_root / "middleware" / "memory.py").read_text(encoding="utf-8")
        ),
        metadata_path: _metadata_without_anthropic(metadata_text),
    }
    changes = [FileChange(path, content) for path, content in targets.items()]
    _validate_contents(changes, site_packages, PATCHED_FILE_SHA256, "DeepAgents patch output")
    patched_payloads = {
        change.path.relative_to(site_packages).as_posix(): change.content.encode("utf-8")
        for change in changes
    }
    updated_record = _update_record(record_text, changes, dist_info, site_packages)
    _validate_record_contents(site_packages, updated_record, patched_payloads, "DeepAgents patch output")
    changes.append(FileChange(record_path, updated_record))
    for change in changes:
        _atomic_write(change.path, change.content.encode("utf-8"))
    for path in targets:
        if path.suffix == ".py":
            _remove_bytecode(path)
    return True


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="目标虚拟环境的 Python 可执行文件")
    args = parser.parse_args(argv)
    try:
        changed = patch_site_packages(_site_packages_for_python(args.python))
    except (OSError, subprocess.CalledProcessError, PatchError) as exc:
        print(f"DeepAgents patch failed: {exc}", file=sys.stderr)
        return 1
    print("DeepAgents patch applied." if changed else "DeepAgents patch already applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
