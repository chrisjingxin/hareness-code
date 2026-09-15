"""验证内网安装使用的 DeepAgents 无 Anthropic 补丁。"""

from __future__ import annotations

import base64
import importlib.util
import hashlib
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/dependencies/apply_deepagents_patch.py"
SPEC = importlib.util.spec_from_file_location("apply_deepagents_patch", SCRIPT)
assert SPEC and SPEC.loader
PATCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PATCH
SPEC.loader.exec_module(PATCH)


GRAPH = '''"""Primary graph assembly module for Deep Agents."""\n\nfrom langchain_anthropic import ChatAnthropic\nfrom langchain_core.language_models import BaseChatModel\n\n\ndef _build_default_model() -> ChatAnthropic:\n    """Construct the default model without emitting a deprecation warning.\n\n    Internal helper used by `create_deep_agent` so the parameter-level\n    `model=None` warning isn't paired with a separate function-level warning\n    from `get_default_model`. Direct user calls go through `get_default_model`,\n    which keeps its decorator and warns once per process.\n    """\n    return ChatAnthropic(model_name="claude-sonnet-4-6")\n\n\ndef get_default_model() -> ChatAnthropic:\n    return _build_default_model()\n'''

PROMPT_CACHING = '''from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware\n\n\ndef append_prompt_caching_middleware(middleware):\n    middleware.append(AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"))\n'''

MEMORY = '''from langchain_anthropic import ChatAnthropic\nfrom langchain_core.messages import ContentBlock, SystemMessage\n\n\nclass MemoryMiddleware:\n    def modify_request(self, request):\n        new_system_message = request.system_message\n        # Runtime check uses `request.model` (not a flag captured at init) so\n        # the breakpoint correctly follows middleware-level model overrides.\n        # Runs regardless of `system_prompt` so callers who suppress the\n        # fragment still get the prompt-cache breakpoint they asked for.\n        if (\n            self._add_cache_control\n            and isinstance(request.model, ChatAnthropic)\n            and new_system_message is not None\n            and new_system_message.content_blocks\n        ):\n            blocks: list[ContentBlock] = list(new_system_message.content_blocks)\n            last = blocks[-1]\n            base = last if isinstance(last, dict) else {}\n            # Merged dict is structurally a ContentBlock with an extra\n            # provider-specific key; ty can't discriminate the union.\n            blocks[-1] = {**base, "cache_control": {"type": "ephemeral"}}  # ty: ignore[invalid-assignment]\n            new_system_message = SystemMessage(content_blocks=blocks)\n        return request\n'''


def create_installation(root: Path, version: str = "0.7.3") -> Path:
    site_packages = root / "site-packages"
    package = site_packages / "deepagents" / "middleware"
    package.mkdir(parents=True)
    (package.parent / "graph.py").write_text(GRAPH, encoding="utf-8")
    (package / "_prompt_caching.py").write_text(PROMPT_CACHING, encoding="utf-8")
    newline = chr(10)
    memory = MEMORY.replace(
        "        return request" + newline,
        "        if new_system_message is request.system_message:" + newline
        + "            return request" + newline
        + "        return request" + newline,
    )
    (package / "memory.py").write_text(memory, encoding="utf-8")
    dist = site_packages / f"deepagents-{version}.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: deepagents\nVersion: "
        f"{version}\nRequires-Dist: langchain-anthropic<2.0.0,>=1.5.3\n",
        encoding="utf-8",
    )
    record_files = [
        "deepagents/graph.py",
        "deepagents/middleware/_prompt_caching.py",
        "deepagents/middleware/memory.py",
        f"deepagents-{version}.dist-info/METADATA",
    ]
    record_lines = []
    for path in record_files:
        payload = (site_packages / path).read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")
        record_lines.append(f"{path},sha256={digest},{len(payload)}\n")
    record_lines.append(f"deepagents-{version}.dist-info/RECORD,,\n")
    (dist / "RECORD").write_text("".join(record_lines), encoding="utf-8")
    return site_packages


def configure_fixture_hashes(site_packages: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """为精简夹具登记哈希，测试状态机而不伪装成官方 wheel。"""

    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    files = {
        "deepagents/graph.py": site_packages / "deepagents/graph.py",
        "deepagents/middleware/_prompt_caching.py": site_packages / "deepagents/middleware/_prompt_caching.py",
        "deepagents/middleware/memory.py": site_packages / "deepagents/middleware/memory.py",
        "deepagents-0.7.3.dist-info/METADATA": site_packages / "deepagents-0.7.3.dist-info/METADATA",
    }
    monkeypatch.setattr(PATCH, "ORIGINAL_FILE_SHA256", {key: digest(path.read_bytes()) for key, path in files.items()})
    monkeypatch.setattr(PATCH, "EXPECTED_WHEEL_SHA256", "fixture-wheel")

    targets = {
        files["deepagents/graph.py"]: PATCH._patch_graph(files["deepagents/graph.py"].read_text()),
        files["deepagents/middleware/_prompt_caching.py"]: PATCH._patch_prompt_caching(
            files["deepagents/middleware/_prompt_caching.py"].read_text()
        ),
        files["deepagents/middleware/memory.py"]: PATCH._patch_memory(files["deepagents/middleware/memory.py"].read_text()),
        files["deepagents-0.7.3.dist-info/METADATA"]: PATCH._metadata_without_anthropic(
            files["deepagents-0.7.3.dist-info/METADATA"].read_text()
        ),
    }
    monkeypatch.setattr(
        PATCH,
        "PATCHED_FILE_SHA256",
        {
            path.relative_to(site_packages).as_posix(): digest(content.encode())
            for path, content in targets.items()
        },
    )


def test_patch_removes_anthropic_imports_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = create_installation(tmp_path)
    configure_fixture_hashes(site_packages, monkeypatch)
    cache = site_packages / "deepagents" / "__pycache__"
    cache.mkdir()
    stale_bytecode = cache / "graph.cpython-313.pyc"
    stale_bytecode.write_bytes(b"stale")

    assert PATCH.patch_site_packages(site_packages) is True
    assert PATCH.patch_site_packages(site_packages) is False
    assert not stale_bytecode.exists()
    assert "langchain_anthropic" not in (site_packages / "deepagents/graph.py").read_text()
    assert "langchain_anthropic" not in (site_packages / "deepagents/middleware/_prompt_caching.py").read_text()
    assert "langchain_anthropic" not in (site_packages / "deepagents/middleware/memory.py").read_text()
    metadata = (site_packages / "deepagents-0.7.3.dist-info/METADATA").read_text()
    assert "Requires-Dist: langchain-anthropic" not in metadata
    assert PATCH.PATCH_ID in metadata


def test_patch_rejects_another_deepagents_version(tmp_path: Path) -> None:
    site_packages = create_installation(tmp_path, version="0.6.8")

    with pytest.raises(PATCH.PatchError, match="expected deepagents==0.7.3"):
        PATCH.patch_site_packages(site_packages)


def test_patch_rejects_source_drift_after_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = create_installation(tmp_path)
    configure_fixture_hashes(site_packages, monkeypatch)
    PATCH.patch_site_packages(site_packages)
    graph = site_packages / "deepagents/graph.py"
    graph.write_text(graph.read_text() + "\n# unexpected source drift\n")

    with pytest.raises(PATCH.PatchError, match="patched DeepAgents: deepagents/graph.py hash mismatch"):
        PATCH.patch_site_packages(site_packages)


def test_patch_rejects_partial_patch_before_record_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = create_installation(tmp_path)
    configure_fixture_hashes(site_packages, monkeypatch)
    graph = site_packages / "deepagents/graph.py"
    graph.write_text(PATCH._patch_graph(graph.read_text()))

    with pytest.raises(PATCH.PatchError, match="original DeepAgents wheel: deepagents/graph.py hash mismatch"):
        PATCH.patch_site_packages(site_packages)


def test_patch_rejects_fabricated_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_packages = create_installation(tmp_path)
    configure_fixture_hashes(site_packages, monkeypatch)
    record = site_packages / "deepagents-0.7.3.dist-info/RECORD"
    lines = record.read_text().splitlines()
    lines[0] = "deepagents/graph.py,sha256=bad,1"
    record.write_text("\n".join(lines) + "\n")

    with pytest.raises(PATCH.PatchError, match="RECORD hash/size mismatch"):
        PATCH.patch_site_packages(site_packages)


def test_patch_applies_again_after_reinstallation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = create_installation(tmp_path / "first")
    configure_fixture_hashes(first, monkeypatch)
    assert PATCH.patch_site_packages(first) is True

    second = create_installation(tmp_path / "second")
    configure_fixture_hashes(second, monkeypatch)
    assert PATCH.patch_site_packages(second) is True
