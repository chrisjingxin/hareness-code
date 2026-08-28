"""``/.harness`` 虚拟路径的只读、分页和 project/thread 隔离回归测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


class _RecordingLog:
    """只记录虚拟 Skill 读取事件。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, fields: dict[str, object]) -> None:
        self.records.append((event, dict(fields)))


def _write_skill(root: Path) -> None:
    """创建带辅助资源的合法项目 Skill。"""
    directory = root / "review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: review\ndescription: review skill\n---\n第一行\n第二行\n第三行\n",
        encoding="utf-8",
    )
    (directory / "reference.txt").write_text("参考一\n参考二\n", encoding="utf-8")


async def test_virtual_files_read_skills_and_thread_scoped_history(tmp_path: Path):
    """正文/资源按 read_file 分页返回，历史只允许当前 project 和 thread 读取。"""
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.threads.thread_persistence import CommitContextRewrite, ContextArtifactDraft, ThreadPersistence
    from harness_agent.threads.virtual_files import HarnessVirtualBackend

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills")
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    store = await ThreadPersistence.open(project=workspace, home=tmp_path / "home")
    artifact = (
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread-a",
                artifacts=(ContextArtifactDraft(kind="history", content="一\n二\n三\n"),),
            )
        )
    ).artifacts[0]
    backend = HarnessVirtualBackend(registry=registry, thread_id="thread-a", thread_persistence=store)

    skill = await backend.aread("/.harness/skills/project/review/SKILL.md", offset=1, limit=1)
    resource = await backend.aread("/.harness/skills/project/review/reference.txt", offset=0, limit=1)
    history = await backend.aread(f"/.harness/history/{artifact.artifact_id}.md", offset=1, limit=1)

    assert skill.file_data and skill.file_data["content"] == "第二行\n"
    assert resource.file_data and resource.file_data["content"] == "参考一\n"
    assert history.file_data and history.file_data["content"] == "二\n"
    assert (await backend.aread("/.harness/history/not-real.md")).error
    assert backend.write("/.harness/history/x.md", "no").error
    # glob/grep 返回空结果而非 error，避免 CompositeBackend 聚合时传播错误。
    glob_result = backend.glob("**/*")
    assert not glob_result.error
    assert glob_result.matches == []

    other = HarnessVirtualBackend(registry=registry, thread_id="thread-b", thread_persistence=store)
    assert (await other.aread(f"/.harness/history/{artifact.artifact_id}.md")).error
    await store.close()


async def test_virtual_skill_success_logs_identity_without_content(tmp_path: Path) -> None:
    """正文与资源成功分页后记 identity；失败读取不伪造成功事件。"""
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.threads.virtual_files import HarnessVirtualBackend

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills")
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    log = _RecordingLog()
    backend = HarnessVirtualBackend(
        registry=registry,
        thread_id="thread-a",
        diagnostic_log=log,
    )

    assert (await backend.aread(
        "/.harness/skills/project/review/SKILL.md", offset=1, limit=1
    )).file_data
    assert (await backend.aread(
        "/.harness/skills/project/review/reference.txt"
    )).file_data
    assert (await backend.aread(
        "/.harness/skills/project/missing/SKILL.md"
    )).error
    assert backend.read(
        "/.harness/skills/project/review/SKILL.md", offset=-1
    ).error

    assert log.records == [
        ("skill.read", {"skill_id": "project/review", "kind": "body"}),
        ("skill.read", {"skill_id": "project/review", "kind": "resource"}),
    ]
    assert "第一行" not in repr(log.records)
    assert "参考一" not in repr(log.records)


async def test_virtual_skill_reads_are_isolated_between_old_and_new_snapshots(
    tmp_path: Path,
):
    """旧 Run 的虚拟正文/资源不随磁盘修改或删除串到下一 Run。"""
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.threads.virtual_files import HarnessVirtualBackend

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills")
    manifest = workspace / ".harness" / "skills" / "review" / "SKILL.md"
    resource = manifest.parent / "reference.txt"
    old_registry = SkillRegistry(workspace, home=tmp_path / "home")
    old_backend = HarnessVirtualBackend(
        registry=old_registry,
        thread_id="thread-a",
        expected_snapshot_id=old_registry.snapshot_id,
        require_snapshot_id=True,
    )

    manifest.write_text(
        "---\nname: review\ndescription: review skill\n---\n新正文\n",
        encoding="utf-8",
    )
    resource.write_text("新资源\n", encoding="utf-8")
    new_registry = SkillRegistry(workspace, home=tmp_path / "home")
    new_backend = HarnessVirtualBackend(
        registry=new_registry,
        thread_id="thread-a",
        expected_snapshot_id=new_registry.snapshot_id,
        require_snapshot_id=True,
    )

    old_skill = await old_backend.aread("/.harness/skills/project/review/SKILL.md")
    old_resource = await old_backend.aread("/.harness/skills/project/review/reference.txt")
    new_skill = await new_backend.aread("/.harness/skills/project/review/SKILL.md")
    new_resource = await new_backend.aread("/.harness/skills/project/review/reference.txt")
    assert old_skill.file_data and "第一行" in old_skill.file_data["content"]
    assert old_resource.file_data and old_resource.file_data["content"] == "参考一\n参考二\n"
    assert new_skill.file_data and new_skill.file_data["content"] == "新正文"
    assert new_resource.file_data and new_resource.file_data["content"] == "新资源\n"

    manifest.unlink()
    resource.unlink()
    deleted_registry = SkillRegistry(workspace, home=tmp_path / "home")
    deleted_backend = HarnessVirtualBackend(
        registry=deleted_registry,
        thread_id="thread-a",
        expected_snapshot_id=deleted_registry.snapshot_id,
        require_snapshot_id=True,
    )
    old_after_delete = await old_backend.aread("/.harness/skills/project/review/SKILL.md")
    old_resource_after_delete = await old_backend.aread(
        "/.harness/skills/project/review/reference.txt"
    )
    deleted_skill = await deleted_backend.aread("/.harness/skills/project/review/SKILL.md")
    assert old_after_delete.file_data and "第一行" in old_after_delete.file_data["content"]
    assert old_resource_after_delete.file_data
    assert old_resource_after_delete.file_data["content"] == "参考一\n参考二\n"
    assert deleted_skill.error


async def test_run_scoped_virtual_backend_isolates_shared_graph_history(tmp_path: Path):
    """共享图每次工具调用都必须按 RunContext 重新绑定历史归档。"""
    from deepagents.backends import LocalShellBackend

    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.threads.thread_persistence import CommitContextRewrite, ContextArtifactDraft, ThreadPersistence
    from harness_agent.threads.virtual_files import run_scoped_virtual_backend_factory

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    store = await ThreadPersistence.open(project=workspace, home=tmp_path / "home")
    artifact = (
        await store.commit_context(
            CommitContextRewrite(
                thread_id="thread-a",
                artifacts=(ContextArtifactDraft(kind="history", content="only thread a"),),
            )
        )
    ).artifacts[0]

    def context_for(thread_id: str) -> RunContext:
        return RunContext(
            thread_id=thread_id,
            run_id=f"run-{thread_id}",
            context_snapshot=prepare_embedded_context_snapshot(
                thread_id=thread_id,
                system_prompt="test prompt",
                workspace=str(workspace),
                sandboxed=False,
                provider=None,
                approval_mode="yolo",
                skill_registry=registry,
                enable_memory=False,
                enable_skills=False,
                enable_ask_user=False,
            ),
            approval_mode="yolo",
            skill_registry=registry,
        )

    factory = run_scoped_virtual_backend_factory(
        LocalShellBackend(root_dir=workspace, virtual_mode=True),
        thread_persistence=store,
    )
    first = factory(SimpleNamespace(context=context_for("thread-a")))
    second = factory(SimpleNamespace(context=context_for("thread-b")))

    assert (await first.aread(f"/.harness/history/{artifact.artifact_id}.md")).file_data["content"] == "only thread a"
    assert (await second.aread(f"/.harness/history/{artifact.artifact_id}.md")).error
    await store.close()


async def test_run_scoped_virtual_backend_requires_the_run_skill_snapshot(tmp_path: Path):
    """共享图的虚拟 Skill 文件只能读取 RunContext 绑定的 catalog identity。"""
    from deepagents.backends import LocalShellBackend

    from harness_agent.threads.context_lifecycle import ContextLifecycle
    from harness_agent.runtime.run_context import RunContext, RunContextError
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.threads.virtual_files import run_scoped_virtual_backend_factory

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills")
    home = tmp_path / "home"
    registry = SkillRegistry(workspace, home=home)
    spec = SimpleNamespace(
        project_fingerprint="project-fingerprint",
        prompt="core prompt",
        effective_policy=SimpleNamespace(
            fingerprint="policy-fingerprint",
            approval_mode="yolo",
            isolation="local",
        ),
        tools=(),
        skill_registry=registry,
        execution=SimpleNamespace(
            mode="local",
            sandbox_enabled=False,
            approval_mode="yolo",
            remote=None,
        ),
        workspace=workspace,
        enable_memory=False,
        enable_skills=True,
        enable_ask_user=False,
    )
    snapshot = ContextLifecycle(workspace, home=home).prepare(
        thread_id="thread-snapshot",
        spec=spec,
    )
    context = RunContext(
        thread_id="thread-snapshot",
        run_id="run-snapshot",
        context_snapshot=snapshot,
        approval_mode="yolo",
        skill_registry=registry,
    )
    factory = run_scoped_virtual_backend_factory(
        LocalShellBackend(root_dir=workspace, virtual_mode=True),
    )
    backend = factory(SimpleNamespace(context=context))
    result = await backend.aread("/.harness/skills/project/review/SKILL.md")
    assert result.file_data and "第一行" in result.file_data["content"]

    (workspace / ".harness" / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: changed\n---\nchanged\n",
        encoding="utf-8",
    )
    changed_registry = SkillRegistry(workspace, home=home)
    with pytest.raises(RunContextError, match="RUN_CONTEXT_SKILL_SNAPSHOT_MISMATCH"):
        RunContext(
            thread_id="thread-snapshot",
            run_id="run-mismatch",
            context_snapshot=snapshot,
            approval_mode="yolo",
            skill_registry=changed_registry,
        )


def test_workspace_boundary_allows_only_virtual_read_file(tmp_path: Path):
    """逻辑根不应交给宿主路径解析，写入、列举和搜索仍在中间件处失败。"""
    from types import SimpleNamespace

    from harness_agent.policy.workspace_boundary import WorkspaceBoundaryMiddleware

    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    read = SimpleNamespace(tool_call={"name": "read_file", "id": "read", "args": {"file_path": "/.harness/skills/project/review/SKILL.md", "offset": 0, "limit": 10}})
    write = SimpleNamespace(tool_call={"name": "write_file", "id": "write", "args": {"file_path": "/.harness/history/x.md", "content": "x"}})
    shell = SimpleNamespace(tool_call={"name": "execute", "id": "shell", "args": {"command": "cat /.harness/history/x.md"}})

    assert middleware.allows_approval(read)
    assert not middleware.allows_approval(write)
    assert not middleware.allows_approval(shell)
