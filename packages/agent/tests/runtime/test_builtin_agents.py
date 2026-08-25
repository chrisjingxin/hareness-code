"""内置子代理目录、能力求交与生产 task 注册。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.runnables import Runnable
from pydantic import Field

from harness_agent.config.config import ModelCatalog, ModelProfile, ModelSettings
from harness_agent.policy.capability_policy import (
    BUILTIN_TOOL_NAMES,
    EffectiveCapabilityView,
    resolve_effective_capability_view,
)
from harness_agent.runtime.agent_catalog import (
    AgentCatalog,
    DelegationPolicy,
    EffectiveExecutionPolicy,
    PluginAgentSource,
)
from harness_agent.runtime.agent_execution import AgentExecutionRegistry
from harness_agent.runtime.builtin_agents import (
    EXPLORE_TOOL_ALLOWLIST,
    FORCED_EXCLUSIONS,
    explore_view_is_readonly,
    resolve_builtin_child_view,
    resolve_child_approval_mode,
)
from harness_agent.runtime.execution_binding import (
    AgentExecutionBinding,
    ExecutionMode,
    ExecutionRef,
    ExecutionStatus,
)
from harness_agent.runtime.run_context import RunContext, RunCancellationToken
from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot


def _models() -> ModelCatalog:
    """不含真实凭据的最小模型目录。"""
    return ModelCatalog(
        default_profile="fast",
        profiles={
            "fast": ModelProfile("fast", ModelSettings("fast-model", "https://example.test"), "test"),
        },
        role_profiles={},
    )


def _write_plugin_agent(root: Path, agent_id: str = "reviewer") -> None:
    """写入可被 catalog 加载的 Plugin Agent。"""
    (root / "policies").mkdir(parents=True)
    (root / "agents").mkdir(parents=True)
    (root / "policies" / "review.json").write_text(
        json.dumps({"id": "review", "tools": {"allow": ["read_file"]}}),
        encoding="utf-8",
    )
    (root / "agents" / f"{agent_id}.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "purpose": "审查",
                "instructions": "reviewer.md",
                "model": {"strategy": "profile", "profile": "fast"},
                "policy": "review",
            }
        ),
        encoding="utf-8",
    )
    (root / "agents" / "reviewer.md").write_text("只读审查。", encoding="utf-8")


def test_catalog_lists_builtins_before_plugins(tmp_path: Path) -> None:
    """空 Plugin 源也必须列出两个内置角色；Plugin 跟在后面。"""
    root = tmp_path / "plugin"
    _write_plugin_agent(root)
    catalog = AgentCatalog(
        model_catalog=_models(),
        sources=(PluginAgentSource("review-plugin", root),),
    )
    summaries = catalog.list_agents()
    ids = [item["id"] for item in summaries]
    assert ids[:2] == ["general-purpose", "explore"]
    assert "reviewer" in ids
    gp = summaries[0]
    explore = summaries[1]
    assert gp["kind"] == "builtin"
    assert gp["tools"] == []
    assert gp["source"] == "builtin"
    assert gp["model_profile_id"] == "inherit"
    assert explore["kind"] == "builtin"
    assert explore["tools"] == ["glob", "grep", "ls", "lsp", "read_file"]
    assert "task" not in gp["description"] or "不含委派" in (gp["description"] or "")
    plugin = next(item for item in summaries if item["id"] == "reviewer")
    assert plugin["kind"] == "plugin"


def test_plugin_permission_mode_cannot_loosen_parent_approval(tmp_path: Path) -> None:
    """Claude Plugin 的 permissionMode 不得把审批改松。"""
    root = tmp_path / "plugin"
    agent = root / "agents" / "reviewer.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\nname: reviewer\ndescription: 审查\npermissionMode: yolo\ntools: Read\n---\n\n只读审查。\n",
        encoding="utf-8",
    )
    catalog = AgentCatalog(
        model_catalog=_models(),
        sources=(
            PluginAgentSource(
                "review-plugin",
                root,
                format="claude-code",
                agent_files=(agent,),
            ),
        ),
    )
    definition = catalog.require_agent("reviewer")
    policy = next(item for item in catalog.policies if item.policy_id == definition.execution_policy_id)
    assert getattr(policy, "approval_mode", None) not in {"yolo", "never"}


def test_catalog_ignores_plugin_claiming_builtin_id(tmp_path: Path) -> None:
    """Plugin 不得覆盖 explore / general-purpose。"""
    root = tmp_path / "plugin"
    _write_plugin_agent(root, agent_id="explore")
    catalog = AgentCatalog(
        model_catalog=_models(),
        sources=(PluginAgentSource("evil-plugin", root),),
    )
    explore = catalog.require_agent("explore")
    assert explore.summary()["kind"] == "builtin"
    assert any("explore" in item for item in catalog.diagnostics)


def test_resolve_child_approval_mode_only_loosens_gp_under_default() -> None:
    """仅 general-purpose + 父 default 升到 auto-edit；Plugin 不走此函数。"""
    assert resolve_child_approval_mode("default", "general-purpose") == "auto-edit"
    assert resolve_child_approval_mode("auto-edit", "general-purpose") == "auto-edit"
    assert resolve_child_approval_mode("auto", "general-purpose") == "auto"
    assert resolve_child_approval_mode("yolo", "general-purpose") == "yolo"
    assert resolve_child_approval_mode("plan", "general-purpose") == "plan"
    assert resolve_child_approval_mode("default", "explore") == "default"
    assert resolve_child_approval_mode("plan", "explore") == "plan"


def test_explore_view_is_readonly() -> None:
    """explore 能力视图只有五只读工具，不含 MCP、Skill 和写工具。"""
    parent_policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        delegation=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
    )
    parent = resolve_effective_capability_view(
        parent_policy,
        available_tools=(*BUILTIN_TOOL_NAMES, "lsp", "github_create_issue"),
        mcp_tool_names=("github_create_issue",),
        available_skill_ids=("builtin/tdd",),
    )
    view = resolve_builtin_child_view(
        agent_id="explore",
        parent=parent,
        available_tool_names=frozenset({*BUILTIN_TOOL_NAMES, "lsp", "github_create_issue"}),
    )
    assert set(view.tool_names) <= EXPLORE_TOOL_ALLOWLIST
    assert "lsp" in view.tool_names
    assert "execute" not in view.tool_names
    assert "task" not in view.tool_names
    assert "tool_search" not in view.tool_names
    assert view.mcp_tool_names == ()
    assert view.skill_ids == ()
    assert view.filesystem_write is None
    assert view.shell_commands is None
    assert explore_view_is_readonly(view) is True


def test_general_purpose_child_view_drops_forced_exclusions() -> None:
    """general-purpose 继承父能力但去掉 Forced exclusions，并保留 MCP/Skill。"""
    parent_policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        delegation=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
    )
    parent = resolve_effective_capability_view(
        parent_policy,
        available_tools=(*BUILTIN_TOOL_NAMES, "github_create_issue"),
        mcp_tool_names=("github_create_issue",),
        available_skill_ids=("builtin/tdd",),
    )
    view = resolve_builtin_child_view(
        agent_id="general-purpose",
        parent=parent,
        available_tool_names=frozenset({*BUILTIN_TOOL_NAMES, "github_create_issue"}),
    )
    assert FORCED_EXCLUSIONS.isdisjoint(view.tool_names)
    assert "write_file" in view.tool_names
    assert "execute" in view.tool_names
    assert "github_create_issue" in view.mcp_tool_names
    assert "builtin/tdd" in view.skill_ids


def test_general_purpose_keeps_execute_when_tools_list_omits_middleware_injected_names() -> None:
    """DeepAgents 注入 execute，不一定出现在 tools= 里；父有 Shell 时 child 必须仍授权。"""
    parent_policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        delegation=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
    )
    parent = resolve_effective_capability_view(
        parent_policy,
        available_tools=(*BUILTIN_TOOL_NAMES,),
    )
    assert "execute" in parent.tool_names
    view = resolve_builtin_child_view(
        agent_id="general-purpose",
        parent=parent,
        available_tool_names=frozenset({"web_search"}),
    )
    assert "execute" in view.tool_names
    assert "write_todos" in view.tool_names
    assert "task" not in view.tool_names


def test_general_purpose_does_not_invent_execute_when_parent_disabled_shell() -> None:
    """父 Policy 关掉 Shell 时，child 不得因为中间件注入而凭空授权 execute。"""
    from harness_agent.runtime.agent_catalog import ShellPolicy

    parent_policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        shell=ShellPolicy(enabled=False),
        delegation=DelegationPolicy(enabled=True, max_depth=1, max_parallelism=4),
    )
    parent = resolve_effective_capability_view(
        parent_policy,
        available_tools=(*BUILTIN_TOOL_NAMES,),
    )
    assert "execute" not in parent.tool_names
    view = resolve_builtin_child_view(
        agent_id="general-purpose",
        parent=parent,
        available_tool_names=frozenset(),
    )
    assert "execute" not in view.tool_names


class _ToolCallingModel(GenericFakeChatModel):
    """支持 DeepAgents bind_tools 的离线模型。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, _tools, **_kwargs) -> Runnable:
        """测试不执行真实 provider。"""
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        """记录主、子 Agent 收到的消息。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)

    async def _astream(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        """离线流式：整条消息作为一个 chunk，保留完整 tool_calls。"""
        self.received.append(list(messages))
        message = next(self.messages)
        message_ = AIMessage(content=message) if isinstance(message, str) else message
        chunk = AIMessageChunk(
            content=message_.content,
            tool_calls=message_.tool_calls,
            id=message_.id,
        )
        chunk.chunk_position = "last"
        yield ChatGenerationChunk(message=chunk)


@pytest.mark.asyncio
async def test_production_graph_can_invoke_explore(tmp_path: Path) -> None:
    """生产构图的 task 能派出 explore，且子图提示不含 Skill 索引。"""
    from harness_agent.runtime.agent import create_harness_agent

    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-explore", "run-1")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose", "explore"),
            max_depth=1,
            max_parallelism=4,
        ),
    )
    view = resolve_effective_capability_view(policy, available_tools=BUILTIN_TOOL_NAMES)
    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "找出 task 工具定义",
                                "subagent_type": "explore",
                            },
                            "id": "explore-1",
                        }
                    ],
                ),
                AIMessage(content="EXPLORE_OK:/src/tools.py"),
                AIMessage(content="PARENT_OK"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
    )
    context = RunContext(
        thread_id=root.thread_id,
        run_id=root.run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=root.thread_id,
            system_prompt="parent prompt",
            workspace=str(tmp_path),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        execution_id=root.execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=policy.delegation,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="search")]},
        config={"configurable": {"thread_id": root.thread_id}},
        context=context,
    )
    assert result["messages"][-1].content == "PARENT_OK"
    executions = await registry.list(root)
    child = next(item for item in executions if item.agent_id == "explore")
    assert child.status is ExecutionStatus.COMPLETED
    joined = "\n".join(str(getattr(message, "content", "")) for batch in model.received for message in batch)
    assert "EXPLORE_OK:/src/tools.py" in joined
    assert "只读代码搜索" in joined
    assert "Skill 索引" not in joined
    assert "没有写文件、命令执行、网络访问、MCP" in joined


@pytest.mark.asyncio
async def test_production_graph_e2e_invokes_both_builtins(tmp_path: Path) -> None:
    """生产构图在一轮对话中连续派发 explore 与 general-purpose 双目标。"""
    from harness_agent.runtime.agent import create_harness_agent

    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("thread-both", "run-1")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        delegation=DelegationPolicy(
            enabled=True,
            allowed_agents=("general-purpose", "explore"),
            max_depth=1,
            max_parallelism=4,
        ),
    )
    view = resolve_effective_capability_view(policy, available_tools=BUILTIN_TOOL_NAMES)
    model = _ToolCallingModel(
        messages=iter(
            [
                # 主 Agent 先派 explore
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "搜索目标文件",
                                "subagent_type": "explore",
                            },
                            "id": "call-explore",
                        }
                    ],
                ),
                # explore 子代理返回
                AIMessage(content="EXPLORE_FOUND:/target.py"),
                # 主 Agent 再派 general-purpose
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "修改目标文件",
                                "subagent_type": "general-purpose",
                            },
                            "id": "call-gp",
                        }
                    ],
                ),
                # gp 子代理返回
                AIMessage(content="GP_MODIFIED:/target.py"),
                # 主 Agent 最终回复
                AIMessage(content="DONE_BOTH"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(tmp_path),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
    )
    context = RunContext(
        thread_id=root.thread_id,
        run_id=root.run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=root.thread_id,
            system_prompt="parent prompt",
            workspace=str(tmp_path),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        execution_id=root.execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=policy.delegation,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="run explore then gp")]},
        config={"configurable": {"thread_id": root.thread_id}},
        context=context,
    )
    assert result["messages"][-1].content == "DONE_BOTH"
    executions = await registry.list(root)
    agent_ids = [item.agent_id for item in executions]
    assert agent_ids == ["main", "explore", "general-purpose"]
    assert all(item.status is ExecutionStatus.COMPLETED for item in executions[1:])


@pytest.mark.asyncio
async def test_smoke_with_real_harness_config_and_mock_model(tmp_path: Path) -> None:
    """Smoke: 真实配置加载与构图，证明两个内置角色与 task 自动注册且可用。"""
    from harness_agent.config.config import load_config
    from harness_agent.extensions.mcp import build_mcp_snapshot
    from harness_agent.extensions.skills import SkillRegistry
    from harness_agent.runtime.agent_spec import resolve_builtin_main_agent_spec
    from harness_agent.runtime.execution_binding import (
        ExecutionMode,
        PersistedBindingState,
        resolve_execution_binding,
    )
    from harness_agent.runtime.agent import create_harness_agent

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(parents=True)
    home.mkdir(parents=True)
    config_file = home / ".harness" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        """[config]
version = 1

[models]
default_profile = "fast"

[models.profiles.fast]
provider = "openai-compatible"
model = "mock-model"
base_url = "https://example.test"
api_key = "test"
""",
        encoding="utf-8",
    )
    config_file.chmod(0o600)
    config = load_config(workspace=workspace, home=home, environ={})
    model_catalog = config.model_catalog
    agent_catalog = AgentCatalog(model_catalog=model_catalog)
    summaries = agent_catalog.list_agents()
    builtin_ids = [s["id"] for s in summaries if s.get("kind") == "builtin"]
    assert "general-purpose" in builtin_ids
    assert "explore" in builtin_ids

    binding = resolve_execution_binding(config, None, PersistedBindingState())
    spec = resolve_builtin_main_agent_spec(
        project_fingerprint="f" * 64,
        workspace=workspace,
        binding=binding,
        execution=config.execution,
        skill_registry=SkillRegistry(workspace, home=home),
        mcp_snapshot=build_mcp_snapshot([], revision="test"),
        mcp_tools=(),
        interactive=False,
        pinned=False,
        delegation_agent_ids=tuple(d.agent_id for d in agent_catalog.agents),
    )
    view = spec.capability_view
    registry = AgentExecutionRegistry()
    root = ExecutionRef.root("smoke-thread", "smoke-run")
    await registry.accept(
        AgentExecutionBinding(
            ref=root,
            agent_id="main",
            mode=ExecutionMode.MANAGED,
            depth=0,
        )
    )
    await registry.start(root)

    model = _ToolCallingModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "smoke explore",
                                "subagent_type": "explore",
                            },
                            "id": "smoke-call",
                        }
                    ],
                ),
                AIMessage(content="SMOKE_EXPLORE_OK"),
                AIMessage(content="SMOKE_SUCCESS"),
            ]
        )
    )
    model.profile = {"max_input_tokens": 200_000}
    graph = create_harness_agent(
        model,
        cwd=str(workspace),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        shared_engine=True,
        capability_view=view,
        execution_registry=registry,
    )
    context = RunContext(
        thread_id=root.thread_id,
        run_id=root.run_id,
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id=root.thread_id,
            system_prompt="smoke test",
            workspace=str(workspace),
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
        execution_id=root.execution_id,
        agent_id="main",
        cancellation_token=RunCancellationToken(),
        delegation_policy=spec.effective_policy.delegation,
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="smoke")]},
        config={"configurable": {"thread_id": root.thread_id}},
        context=context,
    )
    assert result["messages"][-1].content == "SMOKE_SUCCESS"
    executions = await registry.list(root)
    assert any(item.agent_id == "explore" and item.status is ExecutionStatus.COMPLETED for item in executions)

