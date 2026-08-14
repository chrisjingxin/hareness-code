"""ZC-141 Agent Plugins 1.0 离线 fixture 的目录、ZIP 和规范边界测试。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from harness_agent.plugins.manager import PluginManager
from harness_agent.plugins.mcp_schema import validate_http_headers, validate_http_url
from harness_agent.plugins.model import PluginError
from harness_agent.plugins.store import PluginStore


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "agent_plugins"
GOOGLE_FIXTURES = (
    ("google-spanner-0.3.4", "spanner", "spanner-query"),
    ("google-alloydb-0.2.0", "alloydb", "alloydb-query"),
)
PORTABLE_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


def _zip_fixture(source: Path, destination: Path) -> Path:
    """以单一顶层目录打包 fixture，模拟本地 ZIP 来源。"""
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(source.name) / path.relative_to(source)).as_posix())
    return destination


def _assert_regular_shape(root: Path, skill_name: str) -> None:
    """确认 fixture 只有静态普通文件，且客户端 manifest 共存于 portable root。"""
    expected = {
        "plugin.json",
        "mcp.json",
        f"skills/{skill_name}/SKILL.md",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "gemini-extension.json",
        "com.google.gemini-cli/extension.json",
        "SOURCE.md",
    }
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert expected <= actual
    assert not any(path.is_symlink() for path in root.rglob("*"))
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == PORTABLE_SCHEMA
    assert (root / "mcp.json").read_text(encoding="utf-8")
    assert json.loads((root / "mcp.json").read_text(encoding="utf-8"))["mcpServers"] == {}


@pytest.mark.parametrize("fixture_name,plugin_name,skill_name", GOOGLE_FIXTURES)
def test_google_static_fixtures_validate_from_directory_and_zip_without_runtime(
    tmp_path: Path,
    fixture_name: str,
    plugin_name: str,
    skill_name: str,
) -> None:
    """固定提交的脱敏形状可从目录/ZIP 导入，且测试不会启动 MCP。"""
    source = FIXTURE_ROOT / fixture_name
    _assert_regular_shape(source, skill_name)
    manager = PluginManager(home=tmp_path / "home")
    archive = _zip_fixture(source, tmp_path / f"{fixture_name}.zip")

    for package in (source, archive):
        result = manager.validate(package)["plugin"]
        assert isinstance(result, dict)
        assert result["name"] == plugin_name
        assert result["format"] == "hybrid"
        assert result["manifest"] == "plugin.json + .claude-plugin/plugin.json"
        components = {item["kind"]: item for item in result["components"]}
        assert components["skills"]["count"] == 1
        assert components["skills"]["effective"] is True
        assert components["mcp"]["count"] == 0
        assert components["mcp"]["effective"] is False
        assert result["can_enable"] is True


def test_edge_case_fixtures_cover_nonfatal_partial_empty_and_malicious_inputs(
    tmp_path: Path,
) -> None:
    """离线边界 fixture 显式覆盖规范要求的继续加载与失败关闭形状。"""
    nonfatal = FIXTURE_ROOT / "nonfatal-manifest"
    partial = FIXTURE_ROOT / "partial-components"
    empty = FIXTURE_ROOT / "empty-components"
    malicious = FIXTURE_ROOT / "malicious-paths"

    for source in (nonfatal, partial, empty, malicious):
        assert (source / "plugin.json").is_file()
        assert json.loads((source / "plugin.json").read_text(encoding="utf-8"))["$schema"] == PORTABLE_SCHEMA

    manager = PluginManager(home=tmp_path / "home")
    nonfatal_result = manager.validate(nonfatal)["plugin"]
    assert isinstance(nonfatal_result, dict)
    assert nonfatal_result["can_enable"] is True
    assert any("x-fixture-unknown" in item for item in nonfatal_result["diagnostics"])
    assert any("extensions 不是 object" in item for item in nonfatal_result["diagnostics"])

    partial_result = manager.validate(partial)["plugin"]
    assert isinstance(partial_result, dict)
    assert partial_result["compatibility"] == "partial"
    partial_components = {item["kind"]: item for item in partial_result["components"]}
    assert partial_components["skills"]["count"] == 1
    assert partial_components["mcp"]["count"] == 1
    assert partial_components["skills"]["diagnostics"]
    assert partial_components["mcp"]["diagnostics"]

    empty_result = manager.validate(empty)["plugin"]
    assert isinstance(empty_result, dict)
    assert empty_result["compatibility"] == "recognized"
    assert empty_result["can_enable"] is False

    malicious_mcp = json.loads((malicious / "mcp.json").read_text(encoding="utf-8"))
    assert malicious_mcp["mcpServers"]["path-escape"]["command"] == "../bin/server"
    assert malicious_mcp["mcpServers"]["reserved-env"]["env"]["PLUGIN_ROOT"] == "forged-root"
    assert malicious_mcp["mcpServers"]["unknown-placeholder"]["args"] == ["${HOST_ENV}"]


def test_portable_manifest_accepts_period_in_name(tmp_path: Path) -> None:
    """规范允许句点且仍遵守 portable Plugin name 的边界约束。"""
    source = tmp_path / "period-name"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "acme.tools"}),
        encoding="utf-8",
    )
    manager = PluginManager(home=tmp_path / "home")
    result = manager.validate(source)["plugin"]
    assert isinstance(result, dict)
    assert result["name"] == "acme.tools"
    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    assert installed["id"].endswith("/acme.tools")


def test_portable_manifest_accepts_non_semver_version_string(tmp_path: Path) -> None:
    """规范只要求 version 为 string，不以 SemVer 作为拒绝门禁。"""
    source = tmp_path / "version-string"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "version-string", "version": "build snapshot 1"}),
        encoding="utf-8",
    )
    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    assert result["version"] == "build snapshot 1"


@pytest.mark.parametrize(
    "author",
    (
        {"name": 7},
        {"name": "valid", "unexpected": "field"},
    ),
)
def test_portable_manifest_rejects_invalid_author_shape(
    tmp_path: Path,
    author: object,
) -> None:
    """author 的嵌套字段类型和 closed-object 约束会拒绝整个 portable 包。"""
    source = tmp_path / "author-shape"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "author-shape", "author": author}),
        encoding="utf-8",
    )
    with pytest.raises(PluginError):
        PluginManager(home=tmp_path / "home").validate(source)


@pytest.mark.parametrize(
    "manifest_update",
    (
        {"$schema": "https://agent-plugins.org/schemas/9.9.9/plugin.schema.json"},
        {"name": "bad--name"},
        {"name": "a..b"},
        {"name": "-leading"},
        {"name": "trailing-"},
        {"name": "a" * 65},
    ),
)
def test_portable_manifest_schema_and_name_errors_block_component_discovery(
    tmp_path: Path,
    manifest_update: dict[str, str],
) -> None:
    """schema/name 核心错误在固定组件发现前拒绝 portable 包。"""
    source = tmp_path / "fatal-identity"
    source.mkdir()
    manifest = {"$schema": PORTABLE_SCHEMA, "name": "fatal-identity"}
    manifest.update(manifest_update)
    (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    skill = source / "skills" / "should-not-load" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: should-not-load\ndescription: valid\n---\n\nbody\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code in {"PLUGIN_SCHEMA_UNSUPPORTED", "PLUGIN_NAME_INVALID"}


@pytest.mark.parametrize(
    "field_value",
    (
        ("version", 1),
        ("description", []),
        ("homepage", None),
        ("repository", False),
        ("license", {"id": "MIT"}),
        ("keywords", ["portable", 1]),
    ),
)
def test_portable_manifest_core_type_errors_block_component_discovery(
    tmp_path: Path,
    field_value: tuple[str, object],
) -> None:
    """核心 manifest 类型错误在发现 Skill 前 fatal，避免执行不可信组件。"""
    field, value = field_value
    source = tmp_path / f"fatal-{field}"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "fatal-plugin", field: value}),
        encoding="utf-8",
    )
    skill = source / "skills" / "should-not-load" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: should-not-load\ndescription: valid\n---\n\nbody\n",
        encoding="utf-8",
    )

    with pytest.raises(PluginError) as rejected:
        PluginManager(home=tmp_path / "home").validate(source)
    assert rejected.value.code == "PLUGIN_MANIFEST_FIELD_INVALID"


def test_portable_unknown_extension_value_is_ignored_without_validation(tmp_path: Path) -> None:
    """未实现 namespace 的值不读取、不校验，其他组件照常发现。"""
    source = tmp_path / "unknown-extension"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": PORTABLE_SCHEMA,
                "name": "unknown-extension",
                "extensions": {"com.example.client": ["not", "an", "object"]},
                "unknown-top-level": {"ignored": True},
            }
        ),
        encoding="utf-8",
    )
    skill = source / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: valid\n---\n\nbody\n",
        encoding="utf-8",
    )

    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    assert result["name"] == "unknown-extension"
    assert result["can_enable"] is True
    assert any("unknown-top-level" in item for item in result["diagnostics"])
    assert not any("com.example.client" in item for item in result["diagnostics"])


def test_portable_skills_use_direct_children_and_isolate_invalid_entries(
    tmp_path: Path,
) -> None:
    """Skill 只发现直接子目录，坏的直接 Skill 不影响其他有效 Skill。"""
    source = tmp_path / "direct-skills"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "direct-skills"}),
        encoding="utf-8",
    )
    valid = source / "skills" / "valid"
    valid.mkdir(parents=True)
    (valid / "SKILL.md").write_text(
        "---\nname: valid\ndescription: valid\n---\n\nbody\n",
        encoding="utf-8",
    )
    broken = source / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text("not a skill manifest\n", encoding="utf-8")
    nested = source / "skills" / "nested" / "inner"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: inner\ndescription: nested\n---\n\nbody\n",
        encoding="utf-8",
    )

    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    skills = {item["kind"]: item for item in result["components"]}["skills"]
    assert skills["count"] == 1
    assert skills["sources"] == ["skills/valid/SKILL.md"]
    assert any("skills/broken/SKILL.md" in item for item in skills["diagnostics"])
    assert all("skills/nested/inner/SKILL.md" not in item for item in skills["sources"])


def test_portable_wrong_kind_skills_component_does_not_block_mcp(tmp_path: Path) -> None:
    """固定位置 skills 为普通文件时只隔离 Skill，其他组件继续加载。"""
    source = tmp_path / "wrong-kind-skills"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "wrong-kind-skills"}),
        encoding="utf-8",
    )
    (source / "skills").write_text("not a directory\n", encoding="utf-8")
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA,
                "mcpServers": {
                    "valid-server": {"type": "stdio", "command": "server"}
                },
            }
        ),
        encoding="utf-8",
    )

    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    components = {item["kind"]: item for item in result["components"]}
    assert components["skills"]["status"] == "invalid"
    assert components["skills"]["count"] == 0
    assert components["mcp"]["status"] == "supported"
    assert components["mcp"]["count"] == 1
    assert result["compatibility"] == "partial"
    assert result["can_enable"] is True


def test_malicious_mcp_paths_are_isolated_before_runtime(tmp_path: Path) -> None:
    """恶意路径/保留变量逐条隔离，未知 placeholder 保持可运行字面值。"""
    manager = PluginManager(home=tmp_path / "home")
    source = FIXTURE_ROOT / "malicious-paths"
    result = manager.validate(source)["plugin"]
    assert isinstance(result, dict)
    components = {item["kind"]: item for item in result["components"]}
    assert components["mcp"]["status"] == "supported"
    assert components["mcp"]["count"] == 1
    assert components["mcp"]["effective"] is True
    assert result["compatibility"] == "partial"
    assert result["can_enable"] is True
    diagnostics = "\n".join(components["mcp"]["diagnostics"])
    assert "path-escape" in diagnostics
    assert "reserved-env" in diagnostics
    assert "unknown-placeholder" not in diagnostics

    installed = manager.install(source)["plugin"]
    assert isinstance(installed, dict)
    manager.set_enabled(
        str(installed["id"]),
        enabled=True,
        capability_fingerprint=str(installed["capability_fingerprint"]),
    )
    loaded = manager.mcp_servers(manager.catalog(), workspace=tmp_path / "workspace")
    assert len(loaded.servers) == 1
    server = loaded.servers[0]
    assert server.args == ("${HOST_ENV}",)
    assert server.env["VISIBLE"] == "${HOST_ENV}"
    assert any("path-escape" in item for item in loaded.diagnostics)
    assert any("reserved-env" in item for item in loaded.diagnostics)


def test_mcp_closed_schema_isolates_invalid_and_unsupported_servers(tmp_path: Path) -> None:
    """closed union 中的坏条目和不支持 transport 不吞掉有效 server。"""
    source = tmp_path / "mcp-partial"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "mcp-partial"}),
        encoding="utf-8",
    )
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA,
                "mcpServers": {
                    "valid": {"type": "stdio", "command": "fixture-server"},
                    "invalid": {
                        "type": "stdio",
                        "command": "fixture-server",
                        "url": "https://example.test/mcp",
                    },
                    "unsupported": {"type": "websocket", "url": "wss://example.test"},
                },
            }
        ),
        encoding="utf-8",
    )

    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    component = {item["kind"]: item for item in result["components"]}["mcp"]
    assert component["status"] == "supported"
    assert component["count"] == 1
    assert result["compatibility"] == "partial"
    assert any(item.startswith("PLUGIN_COMPONENT_INVALID:") for item in component["diagnostics"])
    assert any(item.startswith("PLUGIN_COMPONENT_UNSUPPORTED:") for item in component["diagnostics"])


def test_mcp_top_level_schema_is_closed(tmp_path: Path) -> None:
    """mcp.json 顶层未知字段使整个 MCP component invalid。"""
    source = tmp_path / "mcp-closed-root"
    source.mkdir()
    (source / "plugin.json").write_text(
        json.dumps({"$schema": PORTABLE_SCHEMA, "name": "mcp-closed-root"}),
        encoding="utf-8",
    )
    (source / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA,
                "mcpServers": {},
                "unexpected": True,
            }
        ),
        encoding="utf-8",
    )

    result = PluginManager(home=tmp_path / "home").validate(source)["plugin"]
    assert isinstance(result, dict)
    component = {item["kind"]: item for item in result["components"]}["mcp"]
    assert component["status"] == "invalid"
    assert component["count"] == 0
    assert result["compatibility"] == "invalid"


def test_mcp_http_url_and_header_validation_is_strict() -> None:
    """HTTP/SSE endpoint 和 header 校验不依赖网络。"""
    assert validate_http_url("http://127.0.0.1:8080/mcp")
    assert validate_http_url("https://example.test/mcp")
    for url in (
        "relative/path",
        "http://example.test/mcp",
        "https://user:pass@example.test/mcp",
        "https://example.test/mcp#fragment",
        "https://example.test/mcp?token=${HOST_ENV}",
    ):
        with pytest.raises(PluginError):
            validate_http_url(url)
    assert validate_http_headers({"Authorization": "Bearer token"}) == {
        "Authorization": "Bearer token"
    }
    with pytest.raises(PluginError):
        validate_http_headers({"X-Test": "one", "x-test": "two"})
    with pytest.raises(PluginError):
        validate_http_headers({"X-Test": "bad\r\nInjected: yes"})
    with pytest.raises(PluginError):
        validate_http_headers({"Authorization": "Bearer ${PLUGIN_ROOT}"})


def test_google_fixture_can_be_staged_as_directory_or_zip(tmp_path: Path) -> None:
    """Store staging 只读取静态文件形状，目录和 ZIP 都不触发 Adapter runtime。"""
    source = FIXTURE_ROOT / "google-spanner-0.3.4"
    archive = _zip_fixture(source, tmp_path / "spanner.zip")
    store = PluginStore(home=tmp_path / "home")

    for package in (source, archive):
        with store.stage(package) as staged:
            assert (staged.root / "plugin.json").is_file()
            assert (staged.root / "skills" / "spanner-query" / "SKILL.md").is_file()
            assert (staged.root / ".claude-plugin" / "plugin.json").is_file()
            assert (staged.root / ".codex-plugin" / "plugin.json").is_file()
            assert (staged.root / "gemini-extension.json").is_file()
            assert (staged.root / "mcp.json").is_file()
