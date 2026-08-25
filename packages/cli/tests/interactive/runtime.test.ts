import { expect, test } from "bun:test"

import {
  createInteractiveRuntime,
  approvalModeLabel,
  executionStatusLabel,
  runtimeStatusSummary,
  supportsHomeDecoration,
  workspaceLabel,
} from "../../src/interactive/runtime"

test("从脱敏初始化结果提取可展示的运行上下文", () => {
  const runtime = createInteractiveRuntime({
    protocol: { major: 3, minor: 0 },
    server: { name: "za38-agent", version: "0.1.0" },
    connection: { id: "test", role: "owner", project: { id: "project", label: "za38-cli" } },
    capabilities: { available: [], enabled: [], handles: [] },
    agent_commands: [],
    skills_snapshot: { id: "snapshot", count: 0 },
    skill_diagnostics: [],
    limits: { max_frame_bytes: 8388608, max_tool_payload_bytes: 1048576 },
    config_summary: {
      workspace: "/work/za38-cli",
      model: { name: "deepseek-v4-flash", api_key_configured: true },
      security: { mode: "remote-sandbox", provider: "corp", approval_mode: "default" },
    },
    startup_error: null,
  }, "/fallback", { gitWorkspace: { kind: "branch", branch: "main", root: "/work/za38-cli" } })

  expect(runtime).toEqual({
    workspace: "/work/za38-cli",
    gitWorkspace: { kind: "branch", branch: "main", root: "/work/za38-cli" },
    cliVersion: "0.1.0",
    modelName: "deepseek-v4-flash",
    modelConfigured: true,
    startupError: undefined,
    executionMode: "remote-sandbox",
    sandboxProvider: "corp",
    approvalMode: "default",
    approvalModeWarning: undefined,
    capabilities: [],
    agentCommands: [],
    mcpSummary: undefined,
    showCacheHitRate: false,
  })
  expect(workspaceLabel(runtime.workspace)).toBe("za38-cli")
  expect(executionStatusLabel(runtime)).toBe("远端沙箱 · corp")
})

test("首页装饰在窄终端降级，并保留执行安全摘要", () => {
  expect(supportsHomeDecoration(87, 40)).toBeFalse()
  expect(supportsHomeDecoration(120, 27)).toBeFalse()
  expect(supportsHomeDecoration(120, 40)).toBeTrue()
  expect(executionStatusLabel({
    workspace: "/work/za38-cli",
    cliVersion: "0.1.0",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  })).toBe("本机执行 · 未隔离")
})

test("审批模式与配置降级提示使用稳定英文展示", () => {
  const runtime = createInteractiveRuntime({
    protocol: { major: 3, minor: 0 },
    server: { name: "za38-agent", version: "0.1.0" },
    connection: { id: "test", role: "owner", project: { id: "project", label: "fallback" } },
    capabilities: { available: [], enabled: [], handles: [] },
    agent_commands: [],
    skills_snapshot: { id: "snapshot", count: 0 },
    skill_diagnostics: [],
    limits: { max_frame_bytes: 8388608, max_tool_payload_bytes: 1048576 },
    config_summary: {
      security: {
        approval_mode: "default",
        approval_mode_warning: "审批模式无效，已安全降级为默认确认模式。",
      },
    },
    startup_error: null,
  }, "/fallback")

  expect(approvalModeLabel({ ...runtime, approvalMode: "plan" })).toBe("plan")
  expect(approvalModeLabel({ ...runtime, approvalMode: "default" })).toBe("default")
  expect(approvalModeLabel({ ...runtime, approvalMode: "auto-edit" })).toBe("auto-edit")
  expect(approvalModeLabel({ ...runtime, approvalMode: "yolo" })).toBe("yolo")
  expect(runtime.approvalModeWarning).toContain("安全降级")
})

test("/status 汇总真实的本机后端和英文审批模式", () => {
  const summary = runtimeStatusSummary({
    workspace: "/work/za38-cli",
    cliVersion: "0.1.0",
    modelName: "deepseek-v4-flash",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  })

  expect(summary).toBe([
    "工作区  /work/za38-cli",
    "模型    deepseek-v4-flash",
    "执行    本机执行 · 未隔离",
    "审批    default",
    "MCP     未配置",
  ].join("\n"))
})

test("/status 仅显示当前 Thread 的模型选择", () => {
  const summary = runtimeStatusSummary({
    workspace: "/work/za38-cli",
    cliVersion: "0.1.0",
    modelName: "pro-model",
    modelProfileId: "pro",
    modelConfigured: true,
    executionMode: "local",
    approvalMode: "default",
  })

  expect(summary).toContain("模型    pro · pro-model")
  expect(summary).not.toContain("当前 Thread")
  expect(summary).not.toContain("后续新 Thread")
})

test("从 config_summary.ui 正确提取 showCacheHitRate 配置", () => {
  const runtimeWithUi = createInteractiveRuntime({
    protocol: { major: 3, minor: 0 },
    server: { name: "za38-agent", version: "0.1.0" },
    connection: { id: "test", role: "owner", project: { id: "project", label: "za38-cli" } },
    capabilities: { available: [], enabled: [], handles: [] },
    agent_commands: [],
    skills_snapshot: { id: "snapshot", count: 0 },
    skill_diagnostics: [],
    limits: { max_frame_bytes: 8388608, max_tool_payload_bytes: 1048576 },
    config_summary: {
      workspace: "/work/za38-cli",
      ui: { show_cache_hit_rate: true },
    },
    startup_error: null,
  }, "/fallback")

  expect(runtimeWithUi.showCacheHitRate).toBeTrue()
})
