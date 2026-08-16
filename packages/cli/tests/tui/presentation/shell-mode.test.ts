import { expect, test } from "bun:test"
import { createTuiAdapter } from "../../../src/tui/application/adapter"
import { createInteractiveController } from "../../../src/interactive/controller"
import { tuiTheme } from "../../../src/tui/presentation/theme"

test("TUI Adapter inputMode 状态机与 Shell 模式切换", async () => {
  const controller = createInteractiveController({
    gateway: {
      initialize: async () => ({
        protocol_version: "3.0",
        agent_version: "0.1.0",
        auth_status: "authenticated",
        workspace: "/workspace/test",
        capabilities: ["runs.direct_shell"],
        approval_mode: "default",
        model: {
          configured: true,
          profile_id: "test-profile",
          model_name: "test-model",
        },
      }),
      listThreads: async () => ({ threads: [] }),
      listSkills: async () => ({ skills: [] }),
      listModelProfiles: async () => ({ default_profile_id: "test", profiles: [] }),
    } as any,
    promptHistoryStore: {
      load: async () => [],
      append: async () => {},
    },
  })

  const adapter = createTuiAdapter({
    controller,
    gateway: {} as any,
    onRequestExit: () => {},
  })

  try {
    expect(adapter.getSnapshot().inputMode).toBe("chat")

    // 派发 input-mode-change 切换到 shell
    await adapter.dispatch({ type: "input-mode-change", mode: "shell" })
    expect(adapter.getSnapshot().inputMode).toBe("shell")

    // 派发 input-mode-change 切回 chat
    await adapter.dispatch({ type: "input-mode-change", mode: "chat" })
    expect(adapter.getSnapshot().inputMode).toBe("chat")

    // 通过 shortcut exit-shell-mode 退出
    await adapter.dispatch({ type: "input-mode-change", mode: "shell" })
    await adapter.dispatch({ type: "shortcut", action: "exit-shell-mode" })
    expect(adapter.getSnapshot().inputMode).toBe("chat")
  } finally {
    await adapter.close()
    await controller.close()
  }
})

test("resolveShortcut 在 inputMode 为 shell 时按下 Escape 返回 exit-shell-mode", async () => {
  const { resolveShortcut } = await import("../../../src/tui/application/shortcuts")
  const action = resolveShortcut({ name: "escape", ctrl: false }, {
    commandMenuVisible: false,
    commandOptionCount: 0,
    activeRun: false,
    hasDraft: true,
    inputMode: "shell",
  })
  expect(action).toBe("exit-shell-mode")
})

test("Shell 模式下 submit 以 direct_shell 模式向 Gateway 启动 Run", async () => {
  let startedRunParams: any = null
  const controller = createInteractiveController({
    gateway: {
      initialize: async () => ({
        protocol_version: "3.0",
        agent_version: "0.1.0",
        auth_status: "authenticated",
        workspace: "/workspace/test",
        capabilities: ["runs.direct_shell"],
        approval_mode: "default",
        model: {
          configured: true,
          profile_id: "test-profile",
          model_name: "test-model",
        },
      }),
      listThreads: async () => ({ threads: [] }),
      listSkills: async () => ({ skills: [] }),
      listModelProfiles: async () => ({ default_profile_id: "test", profiles: [] }),
      startRun: async (params: any) => {
        startedRunParams = params
        return {
          accepted: true,
          thread_id: params.thread_id,
          run_id: params.run_id,
          events: (async function* () {
            yield {
              type: "tool.started",
              payload: { tool_call_id: "call_1", name: "shell" },
            }
            yield {
              type: "tool.completed",
              payload: {
                tool_call_id: "call_1",
                result: { content: "On branch master", is_error: false, truncated: false, original_bytes: 16 },
              },
            }
            yield {
              type: "run.completed",
              payload: { status: "completed" },
            }
          })(),
        }
      },
    } as any,
    promptHistoryStore: {
      load: async () => [],
      append: async () => {},
    },
  })

  const adapter = createTuiAdapter({
    controller,
    gateway: {} as any,
    onRequestExit: () => {},
  })

  try {
    // 切换到 Shell 模式
    await adapter.dispatch({ type: "input-mode-change", mode: "shell" })
    expect(adapter.getSnapshot().inputMode).toBe("shell")

    // 提交命令
    await adapter.dispatch({ type: "submit", value: "git status" })

    // 验证以 direct_shell 模式下发给 gateway
    expect(startedRunParams).not.toBeNull()
    expect(startedRunParams.mode).toBe("direct_shell")
    expect(startedRunParams.message).toBe("git status")

    // 验证提交后模式重置回 chat
    expect(adapter.getSnapshot().inputMode).toBe("chat")
  } finally {
    await adapter.close()
    await controller.close()
  }
})

test("tuiTheme 具有独立的 modeShell 专色且区别于 modeBuild 与 modeCompose", () => {
  expect(tuiTheme.modeShell).toBeDefined()
  expect(typeof tuiTheme.modeShell).toBe("string")
  expect(tuiTheme.modeShell).not.toBe(tuiTheme.modeBuild)
  expect(tuiTheme.modeShell).not.toBe(tuiTheme.modeCompose)
})
