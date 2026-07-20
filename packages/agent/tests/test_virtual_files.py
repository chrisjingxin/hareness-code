"""``/.harness`` 虚拟路径的只读、分页和 project/thread 隔离回归测试。"""

from __future__ import annotations

from pathlib import Path


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
    from harness_agent.skills import SkillRegistry
    from harness_agent.thread_store import ThreadStore
    from harness_agent.virtual_files import HarnessVirtualBackend

    workspace = tmp_path / "workspace"
    _write_skill(workspace / ".harness" / "skills")
    registry = SkillRegistry(workspace, home=tmp_path / "home")
    store = await ThreadStore.open(project=workspace, home=tmp_path / "home")
    artifact = await store.archive_context("thread-a", kind="history", content="一\n二\n三\n")
    backend = HarnessVirtualBackend(registry=registry, thread_id="thread-a", thread_store=store)

    skill = await backend.aread("/.harness/skills/project/review/SKILL.md", offset=1, limit=1)
    resource = await backend.aread("/.harness/skills/project/review/reference.txt", offset=0, limit=1)
    history = await backend.aread(f"/.harness/history/{artifact.artifact_id}.md", offset=1, limit=1)

    assert skill.file_data and skill.file_data["content"] == "第二行\n"
    assert resource.file_data and resource.file_data["content"] == "参考一\n"
    assert history.file_data and history.file_data["content"] == "二\n"
    assert (await backend.aread("/.harness/history/not-real.md")).error
    assert backend.write("/.harness/history/x.md", "no").error
    assert backend.glob("**/*").error

    other = HarnessVirtualBackend(registry=registry, thread_id="thread-b", thread_store=store)
    assert (await other.aread(f"/.harness/history/{artifact.artifact_id}.md")).error
    await store.close()


def test_workspace_boundary_allows_only_virtual_read_file(tmp_path: Path):
    """逻辑根不应交给宿主路径解析，写入、列举和搜索仍在中间件处失败。"""
    from types import SimpleNamespace

    from harness_agent.workspace_boundary import WorkspaceBoundaryMiddleware

    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    read = SimpleNamespace(tool_call={"name": "read_file", "id": "read", "args": {"file_path": "/.harness/skills/project/review/SKILL.md", "offset": 0, "limit": 10}})
    write = SimpleNamespace(tool_call={"name": "write_file", "id": "write", "args": {"file_path": "/.harness/history/x.md", "content": "x"}})
    shell = SimpleNamespace(tool_call={"name": "execute", "id": "shell", "args": {"command": "cat /.harness/history/x.md"}})

    assert middleware.allows_approval(read)
    assert not middleware.allows_approval(write)
    assert not middleware.allows_approval(shell)
