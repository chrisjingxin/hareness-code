"""Agent factory tests using a fake model that supports tool binding."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import Runnable
from pydantic import Field


class ToolCallingFakeChatModel(GenericFakeChatModel):
    """Generic fake model with the minimal bind_tools contract deepagents needs."""

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self


class RecordingFakeChatModel(ToolCallingFakeChatModel):
    """记录模型实际收到的消息，用于验证共享图的动态 Context 注入。"""

    received: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
        """保存模型输入后继续使用 GenericFakeChatModel 的离线响应。"""
        self.received.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


def _make_fake_model() -> ToolCallingFakeChatModel:
    model = ToolCallingFakeChatModel(messages=iter([AIMessage(content="ok")]))
    model.profile = {"max_input_tokens": 200000}
    return model


def _create_agent():
    from harness_agent.runtime.agent import create_harness_agent

    return create_harness_agent(
        model=_make_fake_model(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )


def test_create_harness_agent_returns_compiled_graph():
    agent = _create_agent()
    assert hasattr(agent, "astream")
    assert hasattr(agent, "ainvoke")


def test_default_tool_schema_exposes_only_canonical_snapshot_file_mutations():
    """静态 schema/指纹与运行时 contract 共用同一套 Snapshot 文件参数。"""
    from harness_agent.runtime.agent import default_tool_schemas

    schemas = {str(schema["name"]): schema["parameters"] for schema in default_tool_schemas()}
    names = set(schemas)
    assert "apply_patch" not in names
    assert schemas["edit_file"] == {
        "file_path": "string",
        "snapshot_id": "string",
        "old_string": "string",
        "new_string": "string",
    }
    assert schemas["delete_file"] == {"file_path": "string", "snapshot_id": "string"}


async def test_auto_mode_preflight_uses_classifier_cache_end_to_end():
    """接线回归：auto 模式分类器缓存 allow 命中时不弹窗，工具直接执行。

    防止 create_harness_agent 组装预检时丢失 classifier 参数导致
    F4 缓存永远查不到、所有未决调用回退弹窗。
    """
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.memory import MemorySaver

    from harness_agent.policy.classifier import SafetyClassifier
    from harness_agent.runtime.agent import create_harness_agent

    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": "python --version"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )
    model = ToolCallingFakeChatModel(messages=iter([call, AIMessage(content="done")]))
    model.profile = {"max_input_tokens": 200000}
    classifier = SafetyClassifier(model=object())  # type: ignore[arg-type]
    classifier.record_decision("call-1", "allow", "回归缓存")

    agent = create_harness_agent(
        model,
        checkpointer=MemorySaver(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="auto",
        classifier=classifier,
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="看下版本")]},
        config={"configurable": {"thread_id": "auto-classifier-cache"}},
    )

    assert "__interrupt__" not in result
    assert any(isinstance(message, ToolMessage) for message in result["messages"])


async def test_default_hitl_shows_prepared_file_diff_before_edit(tmp_path: Path):
    """真实 Agent 图在审批前展示已固定的文件 diff，而不是仅展示模型参数。"""
    from langgraph.checkpoint.memory import MemorySaver

    from harness_agent.threads.snapshots import ThreadSnapshotStore
    from harness_agent.threads.text_backend import LocalTextMutationBackend
    from harness_agent.tools.snapshot_file_contract import create_snapshot_file_tool_contract
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.run_context import RunContext
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    target = tmp_path / "approval.txt"
    target.write_text("before\n", encoding="utf-8")
    backend = LocalTextMutationBackend(tmp_path)
    document = backend.read_text_document("/approval.txt")
    snapshots = ThreadSnapshotStore()
    record = snapshots.record_read(
        "prepared-diff",
        "/approval.txt",
        backend.backend_id,
        document.content,
        offset=0,
        limit=1,
        raw_bytes=b"before\n",
    )
    assert record is not None
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "edit_file",
                "args": {
                    "file_path": "/approval.txt",
                    "snapshot_id": record.snapshot_id,
                    "old_string": "before\n",
                    "new_string": "approved\n",
                },
                "id": "prepared-edit",
            }
        ],
    )
    model = ToolCallingFakeChatModel(messages=iter([call, AIMessage(content="done")]))
    model.profile = {"max_input_tokens": 200_000}
    contract = create_snapshot_file_tool_contract(
        object(),
        snapshot_store=snapshots,
        text_backend=backend,
    )
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path),
        checkpointer=MemorySaver(),
        approval_mode="default",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        file_tool_contract=contract,
        shared_engine=True,
    )
    context = RunContext(
        thread_id="prepared-diff",
        run_id="run-prepared-diff",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="prepared-diff",
            system_prompt="test",
            workspace=str(tmp_path),
            sandboxed=False,
            provider=None,
            approval_mode="default",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="default",
        snapshot_store=snapshots,
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="修改文件")]},
        config={"configurable": {"thread_id": "prepared-diff"}},
        context=context,
    )

    interrupt = result["__interrupt__"][0].value
    action = interrupt["action_requests"][0]
    assert action["args"]["file_path"] == "/approval.txt"
    assert "-before" in action["description"]
    assert "+approved" in action["description"]


async def test_production_toolnode_attaches_bounded_lsp_diagnostics_after_write(tmp_path: Path):
    """真实 ToolNode 的异步文件入口在提交后追加 LSP 摘要，而不会回显诊断正文。"""
    import json

    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import ToolMessage
    from langgraph.checkpoint.memory import MemorySaver

    from harness_agent.runtime.agent import create_harness_agent

    class FakeLspManager:
        """模拟 Host 注入的已连接 LSP，只返回一项包含不可信正文的诊断。"""

        async def query(self, *_args: Any, **_kwargs: Any) -> dict[str, object]:
            return {
                "action": "diagnostics",
                "results": {
                    "kind": "full",
                    "items": [
                        {
                            "range": {"start": {"line": 0}, "end": {"line": 0}},
                            "severity": 1,
                            "code": "TEST001",
                            "message": "diagnostic-body-must-not-appear",
                        }
                    ],
                },
            }

    call = AIMessage(
        content="",
        tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": str(tmp_path / "new.txt"), "content": "created\n"},
                "id": "write-with-lsp",
            }
        ],
    )
    model = ToolCallingFakeChatModel(messages=iter([call, AIMessage(content="done")]))
    model.profile = {"max_input_tokens": 200_000}
    plugin_runtime = SimpleNamespace(lsp=FakeLspManager(), middleware=AgentMiddleware())
    agent = create_harness_agent(
        model,
        cwd=str(tmp_path),
        checkpointer=MemorySaver(),
        approval_mode="yolo",
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        plugin_runtime=plugin_runtime,
    )

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="创建文件")]},
        config={"configurable": {"thread_id": "post-write-lsp"}},
    )

    tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    payload = json.loads(str(tool_message.content))
    assert payload["ok"] is True
    assert payload["diagnostics"] == {
        "status": "ok",
        "count": 1,
        "items": [
            {"start_line": 1, "end_line": 1, "severity": "error", "code": "TEST001"}
        ],
        "truncated": False,
        "latency_ms": payload["diagnostics"]["latency_ms"],
    }
    assert "diagnostic-body-must-not-appear" not in str(tool_message.content)


def test_execution_context_prompt_marks_local_and_remote_boundaries():
    """提示词必须如实说明本机默认模式与远端逻辑工作目录。"""
    from harness_agent.runtime.agent import _with_execution_context

    local = _with_execution_context(
        "base", workspace="/tmp/work", sandboxed=False, provider=None
    )
    remote = _with_execution_context(
        "base", workspace="/workspace", sandboxed=True, provider="corp"
    )

    assert "本机工作目录是：`/tmp/work`" in local
    assert "文件工具只允许访问" in local
    assert "不能通过审批绕过" in local
    assert "corp` 远端沙箱" in remote
    assert "`/workspace`" in remote
    # 审批模式事实不再写入稳定边界文本，改由每次 Run 动态追加。
    assert "审批模式" not in local
    assert "审批模式" not in remote


def test_embedded_context_snapshot_keeps_policy_mode_without_legacy_prompt_section():
    """直接库调用的 canonical snapshot 保留实际模式且不生成旧 epoch 文本。"""
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot

    snapshot = prepare_embedded_context_snapshot(
        thread_id="thread-mode",
        system_prompt="base",
        workspace=".",
        sandboxed=False,
        provider=None,
        approval_mode="yolo",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )

    assert "yolo" in snapshot.system_prompt
    assert "PromptEpoch" not in snapshot.system_prompt


def test_context_snapshot_middleware_appends_current_run_mode_fact():
    """共享图每轮按 RunContext 追加模式事实，并保留迁移快照清理规则。"""
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.runtime.run_context import (
        RunContext,
        RunContextSnapshotMiddleware,
        _without_legacy_approval_mode_section,
    )

    legacy = _without_legacy_approval_mode_section(
        "base\n\n## 执行环境\n\nx\n\n## 审批模式：默认确认\n\n旧事实"
    )
    assert legacy == "base\n\n## 执行环境\n\nx"

    snapshot = prepare_embedded_context_snapshot(
        thread_id="thread-mw",
        system_prompt="base",
        workspace=".",
        sandboxed=False,
        provider=None,
        approval_mode="default",
        skill_registry=None,
        enable_memory=False,
        enable_skills=False,
        enable_ask_user=False,
    )
    context = RunContext(
        thread_id="thread-mw",
        run_id="run-mw",
        context_snapshot=snapshot,
        approval_mode="yolo",
    )

    captured: dict[str, str] = {}

    async def handler(request):
        captured["system"] = str(request.system_message.content)
        return SimpleNamespace()

    import asyncio

    request = SimpleNamespace(
        runtime=SimpleNamespace(context=context, config={}),
        system_message=None,
        override=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    asyncio.run(RunContextSnapshotMiddleware().awrap_model_call(request, handler))

    assert "审批模式：YOLO" in captured["system"]
    assert captured["system"].count("## 审批模式：") == 1


def test_default_local_subagent_has_its_own_workspace_guard(tmp_path):
    """默认子 Agent 不得因独立 middleware 栈绕过本机文件边界。"""
    from harness_agent.runtime.agent import _create_default_subagents
    from harness_agent.policy.workspace_boundary import WorkspaceBoundaryMiddleware

    subagents = _create_default_subagents(workspace=tmp_path, approval_mode="default")
    assert subagents[0]["name"] == "general-purpose"
    assert isinstance(subagents[0]["middleware"][0], WorkspaceBoundaryMiddleware)


def test_plan_subagent_has_its_own_plan_guard(tmp_path):
    """子 Agent 的独立栈必须重复计划模式守卫，不能借 task 绕过。"""
    from harness_agent.runtime.agent import _create_default_subagents
    from harness_agent.policy.approval_policy import PlanModeMiddleware

    subagents = _create_default_subagents(workspace=tmp_path, approval_mode="plan")
    assert isinstance(subagents[0]["middleware"][0], PlanModeMiddleware)


async def test_agent_streams_events():
    agent = _create_agent()
    events = [
        event
        async for event in agent.astream(
            {"messages": [HumanMessage(content="hi")]},
            config={"configurable": {"thread_id": "test-1"}},
            stream_mode=["messages", "updates"],
        )
    ]
    assert events


async def test_shared_agent_injects_context_snapshot_per_run_without_thread_state_leakage():
    """同一编译图服务两个 thread 时，模型输入和 checkpoint 必须彼此隔离。"""
    from langgraph.checkpoint.memory import MemorySaver

    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.runtime.run_context import RunContext

    model = RecordingFakeChatModel(
        messages=iter([AIMessage(content="A 完成"), AIMessage(content="B 完成")])
    )
    model.profile = {"max_input_tokens": 200_000}
    agent = create_harness_agent(
        model,
        checkpointer=MemorySaver(),
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        approval_mode="yolo",
        shared_engine=True,
    )

    def run_context(thread_id: str, marker: str) -> RunContext:
        return RunContext(
            thread_id=thread_id,
            run_id=f"run-{thread_id}",
            context_snapshot=prepare_embedded_context_snapshot(
                thread_id=thread_id,
                system_prompt=marker,
                workspace=".",
                sandboxed=False,
                provider=None,
                approval_mode="yolo",
                skill_registry=None,
                enable_memory=False,
                enable_skills=False,
                enable_ask_user=False,
            ),
            approval_mode="yolo",
        )

    await asyncio.gather(
        agent.ainvoke(
            {"messages": [HumanMessage(content="thread A request")]},
            config={"configurable": {"thread_id": "thread-a"}},
            context=run_context("thread-a", "PROMPT_EPOCH_A"),
        ),
        agent.ainvoke(
            {"messages": [HumanMessage(content="thread B request")]},
            config={"configurable": {"thread_id": "thread-b"}},
            context=run_context("thread-b", "PROMPT_EPOCH_B"),
        ),
    )

    system_inputs = [
        "\n".join(str(message.content) for message in messages if message.type == "system")
        for messages in model.received
    ]
    assert any("PROMPT_EPOCH_A" in prompt and "PROMPT_EPOCH_B" not in prompt for prompt in system_inputs)
    assert any("PROMPT_EPOCH_B" in prompt and "PROMPT_EPOCH_A" not in prompt for prompt in system_inputs)

    first = await agent.aget_state({"configurable": {"thread_id": "thread-a"}})
    second = await agent.aget_state({"configurable": {"thread_id": "thread-b"}})
    assert [message.content for message in first.values["messages"] if isinstance(message, HumanMessage)] == ["thread A request"]
    assert [message.content for message in second.values["messages"] if isinstance(message, HumanMessage)] == ["thread B request"]


def test_run_context_rejects_mismatched_langgraph_thread_id():
    """共享图配置与 RunContext 指向不同 thread 时必须 fail closed。"""
    from harness_agent.threads.context_lifecycle import prepare_embedded_context_snapshot
    from harness_agent.runtime.run_context import RunContext, RunContextError, thread_id_for_runtime

    context = RunContext(
        thread_id="thread-a",
        run_id="run-a",
        context_snapshot=prepare_embedded_context_snapshot(
            thread_id="thread-a",
            system_prompt="test prompt",
            workspace=".",
            sandboxed=False,
            provider=None,
            approval_mode="yolo",
            skill_registry=None,
            enable_memory=False,
            enable_skills=False,
            enable_ask_user=False,
        ),
        approval_mode="yolo",
    )
    runtime = SimpleNamespace(
        context=context,
        config={"configurable": {"thread_id": "thread-b"}},
    )

    with pytest.raises(RunContextError, match="RUN_CONTEXT_CONFIG_THREAD_MISMATCH"):
        thread_id_for_runtime(runtime)


def test_tool_search_candidates_respect_capability_view():
    """tool_search 候选与能力视图一致：被策略隐藏的 MCP 工具不可搜索到。

    构图时 ``create_harness_agent`` 先把 ``tools`` 按 capability 过滤，再把
    过滤后的集合传给 ``create_harness_tools(mcp_tools=...)``；搜索候选与
    注入集合共用同一可见集合，不会泄露被隐藏的工具。
    """
    import json

    from langchain_core.tools import StructuredTool

    from harness_agent.policy.capability_policy import (
        BUILTIN_TOOL_NAMES,
        resolve_effective_capability_view,
    )
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.agent_catalog import (
        DelegationPolicy,
        EffectiveExecutionPolicy,
        StringRule,
    )

    def _mcp(name: str, description: str) -> StructuredTool:
        def _impl(x: str) -> str:
            return x

        return StructuredTool.from_function(
            func=_impl,
            name=name,
            description=description,
        )

    mcp = [_mcp("server_a_tool", "A 工具"), _mcp("server_b_tool", "B 工具")]
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        tools=None,
        mcp_tools=StringRule(allow=("server_a_tool",)),
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=None,
        isolation="local",
        approval_mode="yolo",
        delegation=DelegationPolicy(
            enabled=False,
            allowed_agents=(),
            max_depth=1,
            max_parallelism=1,
        ),
    )
    view = resolve_effective_capability_view(
        policy,
        available_tools=(*BUILTIN_TOOL_NAMES, *(t.name for t in mcp)),
        mcp_tool_names=(t.name for t in mcp),
    )
    agent = create_harness_agent(
        _make_fake_model(),
        tools=mcp,
        capability_view=view,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
    )
    tool_node = agent.nodes["tools"].bound
    # 注入集合与搜索候选都只含可见的 server_a_tool。
    assert "server_a_tool" in tool_node.tools_by_name
    assert "server_b_tool" not in tool_node.tools_by_name
    result = json.loads(tool_node.tools_by_name["tool_search"].func("server"))
    assert [item["name"] for item in result["results"]] == ["server_a_tool"]


async def _run_defer_graph(
    defer: bool,
    messages: list[AIMessage],
) -> tuple[list[list[str]], list[str], list[BaseMessage]]:
    """按 defer 开关构图并跑完整工具循环，返回每轮绑定、system 摘要与最终消息。"""
    import json

    from langchain_core.tools import StructuredTool

    from harness_agent.runtime.agent import create_harness_agent

    class RecordingModel(ToolCallingFakeChatModel):
        """记录每轮 bind_tools 收到的工具名与 system 内容。"""

        bindings: list[list[str]] = Field(default_factory=list)
        system_contents: list[str] = Field(default_factory=list)

        def bind_tools(
            self,
            tools: Sequence[Any],
            *,
            tool_choice: str | None = None,
            **kwargs: Any,
        ) -> Runnable:
            self.bindings.append(
                sorted(getattr(t, "name", str(t)) for t in tools)
            )
            return self

        def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
            for message in messages:
                if message.type == "system":
                    self.system_contents.append(str(message.content or ""))
            return super()._generate(messages, *args, **kwargs)

    def _mcp(name: str, description: str) -> StructuredTool:
        def _impl(x: str) -> str:
            return f"{name} handled {x}"

        return StructuredTool.from_function(
            func=_impl,
            name=name,
            description=description,
        )

    model = RecordingModel(messages=iter(messages))
    model.profile = {"max_input_tokens": 200000}
    agent = create_harness_agent(
        model,
        tools=[_mcp("server_a_tool", "A 工具"), _mcp("server_b_tool", "B 工具")],
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        defer_tools=defer,
    )
    result = await agent.ainvoke(
        {"messages": []},
        config={"configurable": {"thread_id": "defer-test"}},
    )
    return model.bindings, model.system_contents, result["messages"]


async def test_defer_tools_hides_then_reveals_on_search():
    """defer 开启：deferred/MCP 工具初始不绑定，tool_search 命中后下一轮可调用。"""
    first = AIMessage(
        content="",
        tool_calls=[{"name": "tool_search", "args": {"query": "server_a"}, "id": "t1"}],
    )
    second = AIMessage(
        content="",
        tool_calls=[{"name": "server_a_tool", "args": {"x": "hi"}, "id": "t2"}],
    )
    bindings, system_contents, messages = await _run_defer_graph(
        defer=True,
        messages=[first, second, AIMessage(content="done")],
    )

    # 第 1 轮只有常驻工具；MCP 与 deferred 内置（lsp 等）均不绑定。
    assert "server_a_tool" not in bindings[0]
    assert "server_b_tool" not in bindings[0]
    assert "lsp" not in bindings[0]
    assert "tool_search" in bindings[0]
    # 搜索命中后下一轮绑定包含该工具，未命中的仍隐藏。
    assert "server_a_tool" in bindings[1]
    assert "server_b_tool" not in bindings[1]
    # 模型直接调用成功，且 prompt 含 deferred 摘要与内置名单。
    assert any(m.type == "tool" and m.name == "server_a_tool" for m in messages)
    assert any("以下工具默认未加载" in content for content in system_contents)
    assert any(
        "lsp" in content and "monitor" in content and "web_search" in content
        for content in system_contents
    )


async def test_defer_tools_off_binds_everything():
    """defer 关闭：MCP 与 deferred 内置全量绑定，保持稳定前缀（D9）。"""
    bindings, system_contents, _ = await _run_defer_graph(
        defer=False,
        messages=[AIMessage(content="done")],
    )

    assert "server_a_tool" in bindings[0]
    assert "server_b_tool" in bindings[0]
    assert "lsp" in bindings[0]
    assert not any("以下工具默认未加载" in content for content in system_contents)


async def test_defer_hidden_tools_not_searchable_in_capability_view():
    """能力视图隐藏的内置工具不出现在 tool_search 候选与摘要中（TS-6）。"""
    import json

    from langchain_core.tools import StructuredTool

    from harness_agent.policy.capability_policy import (
        BUILTIN_TOOL_NAMES,
        resolve_effective_capability_view,
    )
    from harness_agent.runtime.agent import create_harness_agent
    from harness_agent.runtime.agent_catalog import (
        DelegationPolicy,
        EffectiveExecutionPolicy,
        NetworkPolicy,
        StringRule,
    )

    class HiddenModel(ToolCallingFakeChatModel):
        bindings: list[list[str]] = Field(default_factory=list)
        system_contents: list[str] = Field(default_factory=list)

        def bind_tools(
            self,
            tools: Sequence[Any],
            *,
            tool_choice: str | None = None,
            **kwargs: Any,
        ) -> Runnable:
            self.bindings.append(sorted(getattr(t, "name", str(t)) for t in tools))
            return self

        def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any):
            for message in messages:
                if message.type == "system":
                    self.system_contents.append(str(message.content or ""))
            return super()._generate(messages, *args, **kwargs)

    def _mcp(name: str, description: str) -> StructuredTool:
        def _impl(x: str) -> str:
            return f"{name} handled {x}"

        return StructuredTool.from_function(
            func=_impl,
            name=name,
            description=description,
        )

    model = HiddenModel(messages=iter([AIMessage(content="done")]))
    model.profile = {"max_input_tokens": 200000}
    mcp = [_mcp("server_a_tool", "A 工具")]
    all_names = {t.name for t in mcp}
    # 网络关闭 → web_search/web_fetch 被能力视图隐藏。
    policy = EffectiveExecutionPolicy(
        policy_ids=("main",),
        tools=None,
        mcp_tools=StringRule(allow=("server_a_tool",)),
        skills=None,
        filesystem_read=None,
        filesystem_write=None,
        shell=None,
        network=NetworkPolicy(enabled=False),
        isolation="local",
        approval_mode="yolo",
        delegation=DelegationPolicy(
            enabled=False,
            allowed_agents=(),
            max_depth=1,
            max_parallelism=1,
        ),
    )
    view = resolve_effective_capability_view(
        policy,
        available_tools=(*BUILTIN_TOOL_NAMES, *all_names),
        mcp_tool_names=all_names,
    )
    assert "web_search" not in view.tool_names
    agent = create_harness_agent(
        model,
        tools=mcp,
        capability_view=view,
        enable_skills=False,
        enable_memory=False,
        enable_ask_user=False,
        defer_tools=True,
    )
    tool_node = agent.nodes["tools"].bound
    # 被能力视图隐藏的 web_search 不可搜索到。
    hidden = json.loads(tool_node.tools_by_name["tool_search"].func("网络搜索"))
    assert hidden["results"] == []
    # 未被隐藏的 deferred 内置（lsp）仍可搜索。
    visible = json.loads(tool_node.tools_by_name["tool_search"].func("代码智能"))
    assert [item["name"] for item in visible["results"]] == ["lsp"]
    # 摘要不列出被隐藏工具。
    assert not any("web_search" in content for content in model.system_contents)
