"""角色级能力视图、schema 裁剪与执行守卫测试。"""

from __future__ import annotations

from types import SimpleNamespace

from harness_agent.runtime.agent_catalog import (
    DelegationPolicy,
    EffectiveExecutionPolicy,
    NetworkPolicy,
    ShellPolicy,
    StringRule,
)
from harness_agent.policy.capability_policy import (
    CapabilityPolicyMiddleware,
    resolve_effective_capability_view,
)


def _readonly_policy() -> EffectiveExecutionPolicy:
    """构造一个只能读取源码和调用单个 MCP/Skill 的角色策略。"""
    return EffectiveExecutionPolicy(
        policy_ids=("readonly",),
        tools=None,
        mcp_tools=StringRule(allow=("mcp_read",)),
        skills=StringRule(allow=("plugin/acme/review",)),
        filesystem_read=("src/**",),
        filesystem_write=(),
        shell=ShellPolicy(enabled=False),
        network=NetworkPolicy(enabled=False),
        isolation="local",
        approval_mode="always",
        delegation=DelegationPolicy(enabled=False),
    )


def test_readonly_capability_view_filters_files_shell_mcp_skills_and_delegation() -> None:
    """能力求交必须使用实际资源集合，Policy 文字不能凭空创建授权。"""
    view = resolve_effective_capability_view(
        _readonly_policy(),
        available_tools=(
            "read_file",
            "grep",
            "write_file",
            "execute",
            "web_fetch",
            "task",
            "mcp_read",
            "mcp_write",
        ),
        mcp_tool_names=("mcp_read", "mcp_write"),
        available_skill_ids=("plugin/acme/review", "plugin/acme/deploy"),
    )

    assert view.tool_names == ("grep", "mcp_read", "read_file")
    assert view.mcp_tool_names == ("mcp_read",)
    assert view.skill_ids == ("plugin/acme/review",)
    assert not view.allows_tool("write_file")
    assert not view.allows_tool("execute")
    assert not view.allows_tool("task")


def test_capability_middleware_hides_schema_and_rejects_forged_tool_call(tmp_path) -> None:
    """模型看不到写工具，伪造同名 tool call 也不能到达 handler。"""
    view = resolve_effective_capability_view(
        _readonly_policy(),
        available_tools=("read_file", "write_file"),
    )
    middleware = CapabilityPolicyMiddleware(view, workspace=tmp_path)
    request = SimpleNamespace(
        tools=[
            SimpleNamespace(name="write_file"),
            SimpleNamespace(name="read_file"),
        ]
    )

    def override(**changes):
        return SimpleNamespace(**{**request.__dict__, **changes})

    request.override = override
    visible: list[str] = []

    def model_handler(updated):
        visible.extend(tool.name for tool in updated.tools)
        return "ok"

    assert middleware.wrap_model_call(request, model_handler) == "ok"
    assert visible == ["read_file"]

    invoked = False

    def tool_handler(_request):
        nonlocal invoked
        invoked = True
        return "unexpected"

    result = middleware.wrap_tool_call(
        SimpleNamespace(
            tool_call={
                "name": "write_file",
                "id": "forged-write",
                "args": {"file_path": str(tmp_path / "blocked.txt")},
            }
        ),
        tool_handler,
    )
    assert invoked is False
    assert result.status == "error"
    assert "角色能力策略拒绝" in str(result.content)


def test_capability_middleware_enforces_role_path_subset(tmp_path) -> None:
    """即使工具名获准，角色也只能读取 Policy 声明的工作区子树。"""
    view = resolve_effective_capability_view(
        _readonly_policy(),
        available_tools=("read_file",),
    )
    middleware = CapabilityPolicyMiddleware(view, workspace=tmp_path)
    invoked = False

    def handler(_request):
        nonlocal invoked
        invoked = True
        return "ok"

    denied = middleware.wrap_tool_call(
        SimpleNamespace(
            tool_call={
                "name": "read_file",
                "id": "outside-role-subset",
                "args": {"file_path": str(tmp_path / "secrets.txt")},
            }
        ),
        handler,
    )
    assert denied.status == "error"
    assert invoked is False

    allowed = middleware.wrap_tool_call(
        SimpleNamespace(
            tool_call={
                "name": "read_file",
                "id": "inside-role-subset",
                "args": {"file_path": str(tmp_path / "src" / "main.py")},
            }
        ),
        handler,
    )
    assert allowed == "ok"
    assert invoked is True
