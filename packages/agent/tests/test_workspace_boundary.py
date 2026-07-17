"""本机工作区边界中间件的路径与 Agent 工作流回归测试。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from harness_agent.agent import create_harness_agent
from harness_agent.execution import ExecutionContext
from harness_agent.workspace_boundary import WorkspaceBoundaryMiddleware, WorkspacePathPolicy


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


def test_path_policy_rejects_external_and_symlink_escape(tmp_path: Path):
    """canonical 路径在工作区外或经符号链接逃逸时必须被拒绝。"""
    policy = WorkspacePathPolicy(tmp_path)
    with TemporaryDirectory() as outside:
        outside_file = Path(outside) / "secret.txt"
        with pytest.raises(ValueError):
            policy.validate_direct_path(str(outside_file), tool_name="read_file")

        link = tmp_path / "outside-link"
        link.symlink_to(outside, target_is_directory=True)
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


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("ls", {"path": "/tmp/outside"}),
        ("read_file", {"file_path": "/tmp/outside.txt"}),
        ("write_file", {"file_path": "/tmp/outside.txt", "content": "blocked"}),
        ("edit_file", {"file_path": "/tmp/outside.txt", "old_string": "a", "new_string": "b"}),
        ("delete", {"file_path": "/tmp/outside.txt"}),
        ("glob", {"pattern": "**/*.py", "path": "/tmp/outside"}),
        ("grep", {"pattern": "secret", "path": "/tmp/outside"}),
    ],
)
def test_middleware_rejection_does_not_call_handler(
    tmp_path: Path, tool_name: str, args: dict[str, str]
):
    """每个受管工具的越界调用都必须在执行前短路。"""
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
        tool_call={"name": "write_file", "id": "outside", "args": {"file_path": "/tmp/outside.md"}}
    )
    inside = SimpleNamespace(
        tool_call={"name": "write_file", "id": "inside", "args": {"file_path": str(tmp_path / "inside.md")}}
    )

    assert middleware.allows_approval(outside) is False
    assert middleware.allows_approval(inside) is True


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


async def test_real_agent_workflow_cannot_write_outside_workspace(tmp_path: Path):
    """真实 Agent 图收到越界写入工具调用时不得在工作区外创建文件。"""
    with TemporaryDirectory() as outside:
        destination = Path(outside) / "must-not-exist.md"
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
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=False),
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
