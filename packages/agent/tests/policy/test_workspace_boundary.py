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
)


class _ToolCallingFakeModel(FakeMessagesListChatModel):
    """为 deepagents 提供 bind_tools 的最小假模型实现。"""

    def bind_tools(self, *_args: Any, **_kwargs: Any) -> Runnable:
        """返回自身，使预置 AIMessage 中的工具调用可由 Agent 图消费。"""
        return self


def test_path_policy_maps_virtual_paths_to_workspace_descendants(tmp_path: Path):
    """模型使用的 `/` 虚拟路径必须稳定映射到工作区，而不是宿主根目录。"""
    policy = WorkspacePathPolicy(tmp_path)
    existing = tmp_path / "src" / "main.py"
    existing.parent.mkdir()
    existing.write_text("print('ok')", encoding="utf-8")

    assert policy.validate_direct_path("/src/main.py", tool_name="read_file") == existing
    assert policy.validate_direct_path(
        "/generated/new.md", tool_name="write_file"
    ) == tmp_path / "generated" / "new.md"


@pytest.mark.parametrize("candidate", ["relative.md", "../outside.md"])
def test_path_policy_rejects_relative_and_parent_paths(tmp_path: Path, candidate: str):
    """直接文件工具必须使用工作区内绝对路径，不能依赖相对路径语义。"""
    with pytest.raises(ValueError):
        WorkspacePathPolicy(tmp_path).validate_direct_path(candidate, tool_name="write_file")


def test_path_policy_rejects_windows_drive_path_until_trusted(tmp_path: Path):
    """未信任的盘符路径不能直接 validate，须先走信任流程。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows")
    from harness_agent.policy.workspace_roots import ExternalPathNotTrusted

    outside = tmp_path.parent / f"zc142-pol-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "file.py"
    target.write_text("x", encoding="utf-8")
    try:
        with pytest.raises(ExternalPathNotTrusted):
            WorkspacePathPolicy(tmp_path).validate_direct_path(
                str(target), tool_name="read_file"
            )
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()


@pytest.mark.parametrize("candidate", ["\\\\server\\share\\file", "//server/share/file"])
def test_path_policy_rejects_unc_paths(tmp_path: Path, candidate: str):
    """UNC 路径和网络设备命名空间必须在字符串校验阶段被拒绝。"""
    with pytest.raises(ValueError, match="UNC"):
        WorkspacePathPolicy(tmp_path)._require_path_string(
            candidate, tool_name="read_file", field="path"
        )


def test_path_policy_rejects_symlink_escape(tmp_path: Path):
    """虚拟路径经符号链接指向工作区外时必须被拒绝。"""
    policy = WorkspacePathPolicy(tmp_path)
    with TemporaryDirectory() as outside:
        link = tmp_path / "outside-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            return  # Windows 无管理员权限时跳过符号链接逃逸检查
        with pytest.raises(ValueError):
            policy.validate_direct_path("/outside-link/secret.txt", tool_name="read_file")


def test_path_policy_preserves_internal_symlink_for_backend_rejection(tmp_path: Path):
    """边界不能提前解引用工作区内 symlink，否则会绕过底层的禁止跟随策略。"""
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        return

    assert WorkspacePathPolicy(tmp_path).validate_direct_path(
        "/link.txt", tool_name="read_file"
    ) == link


def test_search_policy_keeps_implicit_search_in_workspace_and_rejects_bypass(tmp_path: Path):
    """glob/grep 默认可从工作区搜索，但路径参数和文件模式不能扩大范围。"""
    policy = WorkspacePathPolicy(tmp_path)
    assert policy.validate_search_path("/", tool_name="grep") == tmp_path
    policy.validate_search_pattern("**/*.py", tool_name="glob", field="pattern")
    policy.validate_search_pattern("*.ts", tool_name="grep", field="glob")

    with pytest.raises(ValueError):
        policy.validate_search_path("subdir", tool_name="glob")
    with pytest.raises(ValueError):
        policy.validate_search_pattern("/etc/**/*.conf", tool_name="glob", field="pattern")
    with pytest.raises(ValueError):
        policy.validate_search_pattern("../*.env", tool_name="grep", field="glob")


@pytest.mark.parametrize(
    "tool_name",
    ["ls", "read_file", "write_file", "edit_file", "delete_file", "glob", "grep"],
)
def test_middleware_rejects_non_virtual_paths_without_calling_handler(
    tmp_path: Path, tool_name: str
):
    """每个受管文件工具都必须在执行前拒绝非虚拟路径。"""
    args_map: dict[str, dict[str, str]] = {
        "ls": {"path": "relative"},
        "read_file": {"file_path": "relative.txt"},
        "write_file": {"file_path": "relative.txt", "content": "blocked"},
        "edit_file": {"file_path": "relative.txt", "old_string": "a", "new_string": "b"},
        "delete_file": {"file_path": "relative.txt"},
        "glob": {"pattern": "**/*.py", "path": "relative"},
        "grep": {"pattern": "secret", "path": "relative"},
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
    """HITL 预检与执行边界必须共同拒绝非虚拟路径。"""
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    outside = SimpleNamespace(
        tool_call={"name": "write_file", "id": "outside", "args": {"file_path": "../outside.md"}}
    )
    inside = SimpleNamespace(
        tool_call={"name": "write_file", "id": "inside", "args": {"file_path": "/inside.md"}}
    )

    assert middleware.allows_approval(outside) is False
    assert middleware.allows_approval(inside) is True
    assert inside.tool_call["args"]["file_path"] == "/inside.md"

    canonical = middleware.canonical_approval_request(inside)
    assert canonical is not None
    assert canonical.tool_call["args"]["file_path"] == "/inside.md"
    assert inside.tool_call["args"]["file_path"] == "/inside.md"


@pytest.mark.parametrize("tool_name", ["read_file", "ls", "glob", "grep"])
def test_middleware_still_rejects_relative_reads(tmp_path: Path, tool_name: str):
    """相对路径读取与搜索保持硬拒绝。"""
    args_map: dict[str, dict[str, str]] = {
        "read_file": {"file_path": "relative.txt"},
        "ls": {"path": "relative"},
        "glob": {"pattern": "**/*.py", "path": "relative"},
        "grep": {"pattern": "secret", "path": "relative"},
    }
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
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


def test_middleware_allows_approval_for_trustable_external_path(tmp_path: Path):
    """可信任的外部绝对路径应让 allows_approval 返回 True，并暴露 trust candidate。"""
    outside = tmp_path.parent / f"zc142-mid-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    target = outside / "app.toml"
    target.write_text("x", encoding="utf-8")
    try:
        middleware = WorkspaceBoundaryMiddleware(tmp_path)
        request = SimpleNamespace(
            tool_call={
                "name": "read_file",
                "id": "ext",
                "args": {"file_path": str(target)},
            }
        )
        assert middleware.allows_approval(request) is True
        candidate = middleware.needs_directory_trust(request)
        assert candidate is not None
        assert candidate.directory == outside.resolve() or str(candidate.directory).lower() == str(outside.resolve()).lower()

        # 未信任时执行层仍拒绝（非 auto_trust）
        result = middleware.wrap_tool_call(request, lambda _r: object())
        assert result.status == "error"
        assert "信任" in str(result.content) or "工作区" in str(result.content)

        # yolo / auto_trust 自动授予后可执行
        trusted = WorkspaceBoundaryMiddleware(tmp_path, auto_trust_session=True)
        invoked = False

        def handler(req: object) -> object:
            nonlocal invoked
            invoked = True
            assert req.tool_call["args"]["file_path"].startswith("/@ext/")
            return SimpleNamespace(status="ok")

        result = trusted.wrap_tool_call(request, handler)
        assert invoked is True
        assert result.status == "ok"
    finally:
        target.unlink(missing_ok=True)
        outside.rmdir()


def test_middleware_rejects_untrustable_system_path(tmp_path: Path):
    """不可注册的系统目录即使是绝对路径也硬拒绝，不进入审批。"""
    middleware = WorkspaceBoundaryMiddleware(tmp_path)
    # 使用文件系统根下的路径；Windows 用盘符根会被 DirectoryNotTrustable
    system_path = str(Path(tmp_path.anchor))
    request = SimpleNamespace(
        tool_call={"name": "read_file", "id": "sys", "args": {"file_path": system_path}}
    )
    assert middleware.allows_approval(request) is False
    assert middleware.needs_directory_trust(request) is None


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


async def test_real_agent_workflow_rejects_outside_workspace_in_yolo(tmp_path: Path):
    """YOLO 不扩大路径权限，真实 Agent 图仍拒绝父级路径写入。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "yolo-outside.md"
        model = _ToolCallingFakeModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "../yolo-outside.md", "content": "written outside"},
                            "id": "call-outside",
                        }
                    ],
                ),
                AIMessage(content="越界写入已拒绝"),
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
        assert not destination.exists()


async def test_real_agent_workflow_rejects_outside_write_without_approval(tmp_path: Path):
    """default 模式的父级路径写入在审批前被边界拒绝。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "needs-approval.md"
        model = _ToolCallingFakeModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"file_path": "../needs-approval.md", "content": "blocked"},
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
    """真实 Agent 按 prompt 使用虚拟路径时应在 context 工作区创建文件。"""
    from deepagents.backends import LocalShellBackend

    destination = tmp_path / "allowed-by-context.md"
    model = _ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": "/allowed-by-context.md", "content": "ok"},
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
