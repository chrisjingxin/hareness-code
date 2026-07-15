import { expect, test } from "bun:test"

import {
  createTuiRuntime,
  formatDuration,
  formatUsage,
  runtimeStatusLabel,
  supportsHomeDecoration,
  workspaceLabel,
} from "../../src/tui/model"

test("从脱敏初始化结果提取可展示的运行上下文", () => {
  const runtime = createTuiRuntime({
    server_info: { name: "za38-agent", version: "0.1.0" },
    protocol_version: 1,
    capabilities: {},
    config: {
      workspace: "/work/za38-cli",
      model: { name: "deepseek-v4-flash", api_key_configured: true },
    },
    startup_error: null,
  }, "/fallback", { gitBranch: "main" })

  expect(runtime).toEqual({
    workspace: "/work/za38-cli",
    gitBranch: "main",
    cliVersion: "0.1.0",
    modelName: "deepseek-v4-flash",
    modelConfigured: true,
    startupError: undefined,
  })
  expect(workspaceLabel(runtime.workspace)).toBe("za38-cli")
})

test("首页装饰在窄终端降级，运行状态只展示安全摘要", () => {
  expect(supportsHomeDecoration(87, 40)).toBeFalse()
  expect(supportsHomeDecoration(120, 27)).toBeFalse()
  expect(supportsHomeDecoration(120, 40)).toBeTrue()
  expect(runtimeStatusLabel({
    workspace: "/work/za38-cli",
    cliVersion: "0.1.0",
    modelConfigured: true,
  })).toBe("Agent 已连接")
  expect(runtimeStatusLabel({
    workspace: "/work/za38-cli",
    cliVersion: "0.1.0",
    modelConfigured: false,
  })).toBe("模型未配置")
})

test("运行摘要以紧凑格式显示耗时和 token", () => {
  expect(formatDuration(840)).toBe("840ms")
  expect(formatDuration(1350)).toBe("1.4s")
  expect(formatUsage({ inputTokens: 1200, outputTokens: 35 })).toBe("1.2k in · 35 out")
})
