"""本机工作区边界中间件的路径与 Agent 工作流回归测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from harness_agent.runtime.agent import create_harness_agent
from harness_agent.runtime.execution import ExecutionContext
from harness_agent.policy.workspace_boundary import (
    WorkspaceBoundaryMiddleware,
    WorkspacePathPolicy,
    resolve_outside_workspace_write,
)


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """为 deepagents 提供 bind_tools 的最小假模型实现。"""

    def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
        """返回自身，使预置 AIMessage 中的工具调用可由 Agent 图消费。"""
        return self


def test_path_policy_allows_canonical_workspace_descendants(tmp_path: Path):
    """工作区内已有和待创建的绝对路径都应通过 containment 校验。"""
    policy = WorkspacePathPolicy(tmp_path)
    existing = tmp_path / "src" / "main.py"
    existing.parent.mkdir()
    existing.write_text("print('ok')", encoding="utf-8")

    assert policy.validate_direct_path(str(existing), tool_name="read_file") == existing
    assert policy.validate_direct_path(
        str(tmp_path / "generated" / "new.md"), tool_name="write_file"
    ) == tmp_path / "generated" / "new.md"


@pytest.mark.parametrize("candidate", ["relative.md", "../outside.md"])
def test_path_policy_rejects_relative_and_parent_paths(tmp_path: Path, candidate: str):
    """直接文件工具必须使用工作区内绝对路径，不能依赖相对路径语义。"""
    with pytest.raises(ValueError):
        WorkspacePathPolicy(tmp_path).validate_direct_path(candidate, tool_name="write_file")


def test_path_policy_allows_windows_drive_path_format(tmp_path: Path):
    """Windows 盘符绝对路径格式不应在字符串校验阶段被拒绝。"""
    policy = WorkspacePathPolicy(tmp_path)
    # 只验证字符串校验层不因 Windows 盘符格式拒绝；containment 由 resolve 保证。
    path = policy._require_path_string(
        "D:\\code\\file.py", tool_name="read_file", field="path"
    )
    assert isinstance(path, str)


@pytest.mark.parametrize("candidate", ["\\\\server\\share\\file", "//server/share/file"])
def test_path_policy_rejects_unc_paths(tmp_path: Path, candidate: str):
    """UNC 路径和网络设备命名空间必须在字符串校验阶段被拒绝。"""
    with pytest.raises(ValueError, match="UNC"):
        WorkspacePathPolicy(tmp_path)._require_path_string(
            candidate, tool_name="read_file", field="path"
        )


def test_path_policy_rejects_external_and_symlink_escape(tmp_path: Path):
    """canonical 路径在工作区外或经符号链接逃逸时必须被拒绝。"""
    policy = WorkspacePathPolicy(tmp_path)
    with TemporaryDirectory() as outside:
        outside_file = Path(outside) / "secret.txt"
        with pytest.raises(ValueError):
            policy.validate_direct_path(str(outside_file), tool_name="read_file")

        link = tmp_path / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return  # Windows 无管理员权限时跳过符号链接逃逸检查
        with pytest.raises(ValueError):
            policy.validate_direct_path(str(link / "secret.txt"), tool_name="read_file")


def test_search_policy_keeps_implicit_search_in_workspace_and_rejects_bypass(tmp_path: Path):
    """glob/grep 默认可从工作区搜索，但路径参数和文件模式不能扩大范围。"""
    policy = WorkspacePathPolicy(tmp_path)
    assert policy.validate_search_path(str(tmp_path), tool_name="grep") == tmp_path
    policy.validate_search_pattern("**/*.py", tool_name="glob", field="pattern")
    policy.validate_search_pattern("*.ts", tool_name="grep", field="glob")

    with pytest.raises(ValueError):
        policy.validate_search_path("subdir", tool_name="glob")
    with pytest.raises(ValueError):
        policy.validate_search_pattern("/etc/**/*.conf", tool_name="glob", field="pattern")
    with pytest.raises(ValueError):
        policy.validate_search_pattern("../*.env", tool_name="grep", field="glob")


def _outside_path(tmp_path: Path) -> str:
    """返回一个在工作区外的绝对路径字符串，跨平台安全。

    在 Windows 上 ``/tmp/outside`` 会被中间件归一化为工作区内的虚拟路径，
    因此必须使用真实的 OS 绝对路径来测试越界拒绝。
    """
    return str(tmp_path.parent / "outside")


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"],
)
def test_middleware_rejection_does_not_call_handler(
    tmp_path: Path, tool_name: str
):
    """每个受管工具的越界调用都必须在执行前短路。"""
    outside = _outside_path(tmp_path)
    args_map: dict[str, dict[str, str]] = {
        "ls": {"path": outside},
        "read_file": {"file_path": f"{outside}.txt"},
        "write_file": {"file_path": f"{outside}.txt", "content": "blocked"},
        "edit_file": {"file_path": f"{outside}.txt", "old_string": "a", "new_string": "b"},
        "delete": {"file_path": f"{outside}.txt"},
        "glob": {"pattern": "**/*.py", "path": outside},
        "grep": {"pattern": "secret", "path": outside},
    }
    args = args_map[tool_name]
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    request = SimpleNamespace(
        tool_call={
            "name": tool_name,
            "id": "call-outside",
            "args": args,
        }
    )
    invoked = False

    def handler(_request: object) -> object:
        nonlocal invoked
        invoked = True
        return object()

    result = middleware.wrap_tool_call(request, handler)
    assert invoked is False
    assert result.status == "error"
    assert "工作区边界拒绝" in str(result.content)


def test_middleware_preflight_matches_the_execution_boundary(tmp_path: Path):
    """HITL 预检必须复用最终执行边界，防止越界调用出现可误导的审批框。"""
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    outside = SimpleNamespace(
        tool_call={"name": "write_file", "id": "outside", "args": {"file_path": _outside_path(tmp_path) + ".md"}}
    )
    inside = SimpleNamespace(
        tool_call={"name": "write_file", "id": "inside", "args": {"file_path": str(tmp_path / "inside.md")}}
    )

    assert middleware.allows_approval(outside) is False
    assert middleware.allows_approval(inside) is True


def test_resolve_outside_workspace_write_only_matches_absolute_outside_writes(tmp_path: Path):
    """越界写入判定只覆盖真实的绝对越界路径，其余交给常规边界校验。"""
    outside = tmp_path.parent / "outside.md"
    assert resolve_outside_workspace_write(
        "write_file", {"file_path": str(outside)}, tmp_path
    ) == outside.resolve()
    # 工作区内路径、相对路径、虚拟根、穿越路径与非写入工具均不放行。
    assert resolve_outside_workspace_write(
        "write_file", {"file_path": str(tmp_path / "inside.md")}, tmp_path
    ) is None
    assert resolve_outside_workspace_write("write_file", {"file_path": "relative.md"}, tmp_path) is None
    assert resolve_outside_workspace_write(
        "write_file", {"file_path": "/.harness/thread/x"}, tmp_path
    ) is None
    assert resolve_outside_workspace_write(
        "write_file", {"file_path": str(tmp_path / ".." / "escaped.md")}, tmp_path
    ) is None
    assert resolve_outside_workspace_write(
        "read_file", {"file_path": str(outside)}, tmp_path
    ) is None


@pytest.mark.parametrize("mode", ["default", "auto-edit", "auto", "yolo"])
def test_middleware_non_plan_mode_writes_outside_workspace(tmp_path: Path, mode: str):
    """非 plan 模式的越界 write_file 必须真实写出且不经过底层 handler。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "nested" / "note.md"
        middleware = WorkspaceBoundaryMiddleware(tmp_path, approval_mode=mode)
        request = SimpleNamespace(
            tool_call={
                "name": "write_file",
                "id": "call-outside-write",
                "args": {"file_path": str(destination), "content": "hello outside"},
            }
        )
        invoked = False

        def handler(_request: object) -> object:
            nonlocal invoked
            invoked = True
            return object()

        result = middleware.wrap_tool_call(request, handler)

        assert invoked is False
        assert result.status == "success"
        assert str(result.content) == f"Updated file {destination}"
        assert destination.read_text(encoding="utf-8") == "hello outside"


def test_middleware_plan_mode_rejects_outside_write_without_touching_fs(tmp_path: Path):
    """plan 模式的越界写入必须直接拒绝且不创建任何文件。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "blocked.md"
        middleware = WorkspaceBoundaryMiddleware(tmp_path, approval_mode="plan")
        request = SimpleNamespace(
            tool_call={
                "name": "write_file",
                "id": "call-plan-blocked",
                "args": {"file_path": str(destination), "content": "blocked"},
            }
        )
        result = middleware.wrap_tool_call(request, lambda _request: object())

        assert result.status == "error"
        assert "工作区边界拒绝" in str(result.content)
        assert not destination.exists()


def test_middleware_non_plan_mode_edits_outside_workspace(tmp_path: Path):
    """非 plan 模式的越界 edit_file 必须复现唯一匹配替换语义并真实写回。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "config.txt"
        destination.write_text("alpha\nbeta\n", encoding="utf-8")
        middleware = WorkspaceBoundaryMiddleware(tmp_path, approval_mode="default")

        edit = SimpleNamespace(
            tool_call={
                "name": "edit_file",
                "id": "call-outside-edit",
                "args": {
                    "file_path": str(destination),
                    "old_string": "beta",
                    "new_string": "gamma",
                },
            }
        )
        result = middleware.wrap_tool_call(edit, lambda _request: object())
        assert result.status == "success"
        assert f"Successfully replaced 1 instance(s) of the string in '{destination}'" == str(result.content)
        assert destination.read_text(encoding="utf-8") == "alpha\ngamma\n"

        missing = SimpleNamespace(
            tool_call={
                "name": "edit_file",
                "id": "call-outside-missing",
                "args": {
                    "file_path": str(Path(outside) / "missing.txt"),
                    "old_string": "x",
                    "new_string": "y",
                },
            }
        )
        error = middleware.wrap_tool_call(missing, lambda _request: object())
        assert error.status == "error"
        assert "not found" in str(error.content)


def test_middleware_non_plan_mode_deletes_outside_workspace(tmp_path: Path):
    """非 plan 模式的越界 delete_file 必须真实删除并返回工具 JSON 契约。"""
    import json

    with TemporaryDirectory() as outside:
        destination = Path(outside) / "obsolete.log"
        destination.write_text("stale", encoding="utf-8")
        middleware = WorkspaceBoundaryMiddleware(tmp_path, approval_mode="yolo")
        request = SimpleNamespace(
            tool_call={
                "name": "delete_file",
                "id": "call-outside-delete",
                "args": {"file_path": str(destination)},
            }
        )
        result = middleware.wrap_tool_call(request, lambda _request: object())

        assert result.status == "success"
        assert json.loads(str(result.content)) == {"success": True, "deleted": str(destination)}
        assert not destination.exists()


@pytest.mark.parametrize("tool_name", ["read_file", "ls", "glob", "grep"])
def test_middleware_non_plan_mode_still_rejects_outside_reads(tmp_path: Path, tool_name: str):
    """越界读取与搜索在任何审批模式下都保持硬拒绝。"""
    outside = _outside_path(tmp_path)
    args_map: dict[str, dict[str, str]] = {
        "read_file": {"file_path": f"{outside}.txt"},
        "ls": {"path": outside},
        "glob": {"pattern": "**/*.py", "path": outside},
        "grep": {"pattern": "secret", "path": outside},
    }
    middleware = WorkspaceBoundaryMiddleware(tmp_path, approval_mode="yolo")
    request = SimpleNamespace(
        tool_call={"name": tool_name, "id": "call-outside-read", "args": args_map[tool_name]}
    )
    invoked = False

    def handler(_request: object) -> object:
        nonlocal invoked
        invoked = True
        return object()

    result = middleware.wrap_tool_call(request, handler)
    assert invoked is False
    assert result.status == "error"


async def test_async_middleware_rejection_does_not_call_handler(tmp_path: Path):
    """异步工具链同样必须在进入底层后端前拒绝越界路径。"""
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    request = SimpleNamespace(
        tool_call={
            "name": "glob",
            "id": "call-absolute-pattern",
            "args": {"pattern": "/outside/**/*.py"},
        }
    )
    invoked = False

    async def handler(_request: object) -> object:
        nonlocal invoked
        invoked = True
        return object()

    result = await middleware.awrap_tool_call(request, handler)
    assert invoked is False
    assert result.status == "error"


async def test_real_agent_workflow_writes_outside_workspace_in_yolo(tmp_path: Path):
    """yolo 模式的真实 Agent 图收到越界写入时必须真实写出工作区外文件。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "yolo-outside.md"
        model = _ToolCallingFakeModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": str(destination), "content": "written outside"},
                            "id": "call-outside",
                        }
                    ],
                ),
                AIMessage(content="越界写入已完成"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=str(tmp_path),
            approval_mode="yolo",
            enable_ask_user=False,
            enable_memory=False,
            enable_skills=False,
        )

        events = [
            event
            async for event in agent.astream(
                {"messages": [HumanMessage(content="在工作区外写文件")]},
                config={"configurable": {"thread_id": "workspace-boundary"}},
                stream_mode=["messages", "updates"],
            )
        ]

        assert events
        assert destination.read_text(encoding="utf-8") == "written outside"


async def test_real_agent_workflow_pauses_outside_write_before_approval(tmp_path: Path):
    """default 模式的越界写入进入审批中断，批准前不得写出工作区外文件。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "needs-approval.md"
        model = _ToolCallingFakeModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": str(destination), "content": "blocked"},
                            "id": "call-outside",
                        }
                    ],
                ),
                AIMessage(content="越界写入已被拒绝"),
            ]
        )
        model.profile = {"max_input_tokens": 200_000}
        agent = create_harness_agent(
            model,
            cwd=str(tmp_path),
            approval_mode="default",
            enable_ask_user=False,
            enable_memory=False,
            enable_skills=False,
        )

        events = [
            event
            async for event in agent.astream(
                {"messages": [HumanMessage(content="在工作区外写文件")]},
                config={"configurable": {"thread_id": "workspace-boundary-approval"}},
                stream_mode=["messages", "updates"],
            )
        ]

        assert events
        assert not destination.exists()


async def test_execution_context_workspace_is_used_when_cwd_is_omitted(tmp_path: Path):
    """库调用方只注入本机 context 时，守卫仍以 context 工作区为准。"""
    from deepagents.backends import LocalShellBackend

    destination = tmp_path / "allowed-by-context.md"
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": str(destination), "content": "ok"},
                        "id": "call-context-workspace",
                    }
                ],
            ),
            AIMessage(content="写入完成"),
        ]
    )
    model.profile = {"max_input_tokens": 200_000}
    context = ExecutionContext(
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        mode="local",
        workspace_path=str(tmp_path),
        provider=None,
    )
    agent = create_harness_agent(
        model,
        execution_context=context,
        approval_mode="yolo",
        enable_ask_user=False,
        enable_memory=False,
        enable_skills=False,
    )

    async for _ in agent.astream(
        {"messages": [HumanMessage(content="在上下文工作区创建文件")]},
        config={"configurable": {"thread_id": "context-workspace"}},
        stream_mode=["messages", "updates"],
    ):
        pass

    assert destination.read_text(encoding="utf-8") == "ok"
