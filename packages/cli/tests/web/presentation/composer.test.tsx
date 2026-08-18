/** Composer：受控 draft、发送/取消、Skill chip、命令菜单与键盘交互。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"
import { act } from "react"
import { createElement, type ReactElement } from "react"

import { Composer, resolveComposerKeyboardIntent } from "../../../src/web/presentation/composer"
import type {
  CommandMenuItem,
  SkillMenuItem,
} from "../../../src/interactive/commands"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import type { ModelProfile } from "../../../src/interactive/types"
import { makeCatalog, makeInteractive, makeSnapshot, makeSkill } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())


function mountComposer(snapshot: WebAdapterSnapshot, intents: WebIntent[]): RenderHandle {
  return render(
    <Composer
      snapshot={snapshot}
      dispatch={intent => {
        intents.push(intent)
      }}
    />,
  )
}

const SAMPLE_SKILL: SkillMenuItem = {
  id: "skill-a",
  name: "Skill A",
  description: "demo",
  source: "builtin",
  enabled: true,
  userInvocable: true,
}

const SAMPLE_COMMAND: CommandMenuItem = {
  kind: "command",
  command: {
    id: "system.status",
    name: "status",
    description: "显示运行状态",
    source: { type: "builtin" },
    presentation: "viewer",
  },
  availability: { state: "available" },
}

const DISABLED_COMMAND: CommandMenuItem = {
  kind: "command",
  command: {
    id: "thread.resume",
    name: "resume",
    description: "恢复 thread",
    source: { type: "builtin" },
    presentation: "picker",
  },
  availability: { state: "disabled", reason: "当前任务结束或交互完成后可用" },
}

describe("Composer", () => {
  test("不显示没有独立交互的指令 Tab；Slash 命令仍由输入菜单提供", () => {
    const handle = mountComposer(makeSnapshot(), [])
    try {
      expect(handle.container.querySelector(".composer-tab")?.textContent).toBe("聊天")
      expect(handle.container.textContent).not.toContain("指令")
    } finally {
      handle.unmount()
    }
  })

  test("textarea 跟随 snapshot.draft；受控输入由 Adapter 的 draft-change 路径承载", () => {
    const handle = mountComposer(makeSnapshot({ draft: "初始" }), [])
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(textarea?.value).toBe("初始")
    } finally {
      handle.unmount()
    }
  })

  test("工作模式 chip 是 rail 中唯一模式展示：未锁定提示 Tab 切换，锁定显示锁图标", () => {
    const unlocked = mountComposer(makeSnapshot({ interactive: makeInteractive({ workMode: "build", threadMode: null }) }), [])
    try {
      const chip = unlocked.container.querySelector<HTMLElement>(".mode-chip")
      expect(chip?.textContent).toBe("Build")
      expect(chip?.title).toContain("Tab")
      expect(chip?.querySelector("svg")).toBeNull()
    } finally {
      unlocked.unmount()
    }
    const locked = mountComposer(makeSnapshot({ interactive: makeInteractive({ workMode: "compose", threadMode: "compose" }) }), [])
    try {
      const chip = locked.container.querySelector<HTMLElement>(".mode-chip")
      expect(chip?.textContent).toContain("Compose")
      expect(chip?.title).toContain("锁定")
      expect(chip?.querySelector("svg")).not.toBeNull()
    } finally {
      locked.unmount()
    }
  })

  test("rail 布局：左=审批下拉+工作模式 chip，右=模型下拉+发送；装饰图标与键盘提示已移除", () => {
    const handle = mountComposer(makeSnapshot(), [])
    try {
      const left = handle.container.querySelector(".composer-rail-left")
      const right = handle.container.querySelector(".composer-rail-right")
      // 左栏第一个是审批下拉，工作模式 chip 跟在旁边。
      expect(left?.querySelector(".composer-approval") !== null).toBe(true)
      expect(left?.querySelector(".mode-chip")?.textContent).toBe("Build")
      expect(left?.firstElementChild?.classList.contains("composer-rail-control")).toBe(true)
      // 右栏模型下拉在发送键旁。
      expect(right?.querySelector(".composer-model") !== null).toBe(true)
      expect(right?.querySelector(".send-button") !== null).toBe(true)
      // 装饰图标、键盘提示与只读审批 mode-chip 已移除。
      expect(handle.container.querySelector(".composer-decoration-icons") === null).toBe(true)
      expect(handle.container.querySelector(".composer-hint") === null).toBe(true)
      expect(handle.container.querySelectorAll(".mode-chip").length).toBe(1)
    } finally {
      handle.unmount()
    }
  })

  test("审批模式下拉：列出全部模式、选择派发 approval-mode-select，忙碌时禁用", () => {
    const intents: WebIntent[] = []
    const handle = mountComposer(makeSnapshot(), intents)
    try {
      const control = handle.container.querySelector<HTMLButtonElement>(".composer-approval")
      expect(control).not.toBeNull()
      expect(control?.getAttribute("aria-label")).toBe("选择审批模式，当前：default")
      expect(control?.getAttribute("aria-haspopup")).toBe("menu")
      expect(control?.getAttribute("aria-expanded")).toBe("false")
      expect(control?.disabled).toBe(false)

      act(() => { control?.click() })
      expect(control?.getAttribute("aria-expanded")).toBe("true")
      const options = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".composer-menu-option"))
      expect(options.map(option => option.textContent?.replace("✓", "").trim())).toEqual(["plan", "default", "auto-edit", "auto", "yolo"])

      const autoOption = options.find(option => option.textContent?.includes("auto") && !option.textContent?.includes("auto-edit"))
      act(() => { autoOption?.click() })
      expect(intents).toContainEqual({ type: "approval-mode-select", mode: "auto" })
      expect(handle.container.querySelector(".composer-menu") === null).toBe(true)
    } finally {
      handle.unmount()
    }

    const busy = makeInteractive({ activeRun: { threadId: "t1", runId: "r1" } })
    const busyHandle = mountComposer(makeSnapshot({ interactive: busy }), [])
    try {
      expect(busyHandle.container.querySelector<HTMLButtonElement>(".composer-approval")?.disabled).toBe(true)
    } finally {
      busyHandle.unmount()
    }
  })

  test("模型下拉：打开刷新目录、列出模型、选择派发 model-select，管理入口开 Dock", () => {
    const profiles: ModelProfile[] = [
      { id: "fast", model: "k3-fast", provider_label: "DeepSeek", context_window_tokens: 128000, capabilities: [], is_default: true, available: true, source: "config" },
      { id: "pro", model: "k3-pro", provider_label: "DeepSeek", context_window_tokens: 128000, capabilities: [], is_default: false, available: false, unavailable_reason: "缺少 API Key", source: "config" },
    ]
    const interactive = makeInteractive({
      catalogs: { ...makeInteractive().catalogs, models: makeCatalog<ModelProfile>(profiles) },
      selection: { requestedModelProfileId: "fast", actualModel: null, armedSkill: null },
    })
    const intents: WebIntent[] = []
    const handle = mountComposer(makeSnapshot({ interactive }), intents)
    try {
      const control = handle.container.querySelector<HTMLButtonElement>(".composer-model")
      expect(control).not.toBeNull()
      expect(control?.getAttribute("aria-haspopup")).toBe("menu")
      expect(control?.getAttribute("aria-expanded")).toBe("false")

      // 打开菜单：请求刷新 models 目录，但不打开 Dock。
      act(() => { control?.click() })
      expect(control?.getAttribute("aria-expanded")).toBe("true")
      expect(intents).toContainEqual({ type: "models-catalog-refresh" })
      expect(intents.some(intent => intent.type === "dock-open")).toBe(false)

      const options = Array.from(handle.container.querySelectorAll<HTMLButtonElement>(".model-option"))
      expect(options.length).toBe(2)
      // 当前选中项带 aria-checked；不可用项禁用并提示原因。
      expect(options[0]?.getAttribute("aria-checked")).toBe("true")
      expect(options[0]?.textContent).toContain("fast")
      expect(options[0]?.textContent).toContain("DeepSeek · k3-fast")
      expect(options[1]?.getAttribute("aria-checked")).toBe("false")
      expect(options[1]?.disabled).toBe(true)
      expect(options[1]?.title).toBe("缺少 API Key")

      // 选择可用模型：派发 model-select 并关闭菜单。
      act(() => { options[0]?.click() })
      expect(intents).toContainEqual({ type: "model-select", profileId: "fast" })
      expect(handle.container.querySelector(".composer-menu") === null).toBe(true)

      // 管理入口：打开 Dock models 面板并关闭菜单。
      act(() => { control?.click() })
      const manage = handle.container.querySelector<HTMLButtonElement>(".composer-menu-manage")
      expect(manage?.textContent).toContain("管理模型")
      act(() => { manage?.click() })
      expect(intents).toContainEqual({ type: "dock-open", panel: "models" })
      expect(handle.container.querySelector(".composer-menu") === null).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("模型目录为空时下拉显示空态，管理入口仍可用", () => {
    const handle = mountComposer(makeSnapshot(), [])
    try {
      const control = handle.container.querySelector<HTMLButtonElement>(".composer-model")
      act(() => { control?.click() })
      expect(handle.container.querySelector(".composer-menu .composer-menu-status")?.textContent ?? "").toContain("暂无可用模型")
      expect(handle.container.querySelector(".composer-menu-manage") !== null).toBe(true)
    } finally {
      handle.unmount()
    }
  })

  test("IME、Enter、Shift+Enter 与命令菜单走同一键盘状态机", () => {
    const base = {
      shiftKey: false,
      ctrlKey: false,
      metaKey: false,
      isComposing: false,
      menuVisible: false,
      items: [] as readonly CommandMenuItem[],
      selectedIndex: 0,
      draft: "你好",
      composedDisabled: false,
      activeRun: false,
    }
    expect(resolveComposerKeyboardIntent({ ...base, key: "Enter", isComposing: true }))
      .toEqual({ preventDefault: false, intent: null })
    expect(resolveComposerKeyboardIntent({ ...base, key: "Enter" }))
      .toEqual({ preventDefault: true, intent: { type: "submit" } })
    expect(resolveComposerKeyboardIntent({ ...base, key: "Enter", shiftKey: true }))
      .toEqual({ preventDefault: false, intent: null })
    expect(resolveComposerKeyboardIntent({ ...base, key: "ArrowDown", menuVisible: true, items: [SAMPLE_COMMAND] }))
      .toEqual({ preventDefault: true, intent: { type: "command-menu-hover", selectedIndex: 0 } })
    expect(resolveComposerKeyboardIntent({ ...base, key: "Enter", menuVisible: true, items: [SAMPLE_COMMAND] }))
      .toEqual({ preventDefault: true, intent: { type: "command-menu-select", item: SAMPLE_COMMAND } })
    expect(resolveComposerKeyboardIntent({ ...base, key: "Enter", menuVisible: true, items: [DISABLED_COMMAND] }))
      .toEqual({ preventDefault: true, intent: null })
  })

  test("activeRun 时显示取消按钮且 dispatch cancel-run；idle 时显示发送按钮", () => {
    const intents: WebIntent[] = []
    const interactive = makeInteractive({
      activeRun: { threadId: "t1", runId: "r1" },
    })
    const handle = mountComposer(makeSnapshot({ draft: "x", interactive }), intents)
    try {
      const cancel = handle.container.querySelector<HTMLButtonElement>(".cancel-button")
      expect(cancel).not.toBeNull()
      const send = handle.container.querySelector<HTMLButtonElement>(".send-button")
      expect(send).toBeNull()
      act(() => { cancel?.click() })
      expect(intents).toContainEqual({ type: "cancel-run" })
    } finally {
      handle.unmount()
    }

    intents.length = 0
    const handle2 = mountComposer(makeSnapshot({ draft: "x" }), intents)
    try {
      const send = handle2.container.querySelector<HTMLButtonElement>(".send-button")
      expect(send).not.toBeNull()
      expect(send?.disabled).toBe(false)
    } finally {
      handle2.unmount()
    }
  })

  test("上下文压缩期间禁用输入和发送并显示等待原因", () => {
    const interactive = makeInteractive({ activity: { kind: "compacting" } })
    const handle = mountComposer(makeSnapshot({ draft: "保留的草稿", interactive }), [])
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      const send = handle.container.querySelector<HTMLButtonElement>(".send-button")
      expect(textarea?.disabled).toBe(true)
      expect(textarea?.placeholder).toBe("正在压缩上下文…")
      expect(send?.disabled).toBe(true)
      expect(handle.container.textContent).toContain("上下文压缩中，请稍候")
    } finally {
      handle.unmount()
    }
  })

  test("armed Skill 显示 chip 名称；点击清除 dispatch skill-clear", () => {
    const interactive = makeInteractive({
      selection: {
        requestedModelProfileId: null,
        actualModel: null,
        armedSkill: { ...SAMPLE_SKILL, name: "已选 Skill" },
      },
    })
    const intents: WebIntent[] = []
    const handle = mountComposer(makeSnapshot({ interactive }), intents)
    try {
      const chip = handle.container.querySelector(".skill-chip")
      expect(chip?.textContent).toContain("已选 Skill")
      const clear = chip?.querySelector<HTMLButtonElement>(".skill-chip-clear")
      expect(clear).not.toBeNull()
      act(() => { clear?.click() })
      expect(intents).toContainEqual({ type: "skill-clear" })
    } finally {
      handle.unmount()
    }
  })

  test("draft 以 / 开头时打开命令菜单并显示选项；// 不打开", () => {
    const intents: WebIntent[] = []
    const snapshot = makeSnapshot({
      draft: "/st",
      commandMenuOpen: true,
      commandOptions: [SAMPLE_COMMAND, DISABLED_COMMAND],
      commandMenuIndex: 0,
    })
    const handle = mountComposer(snapshot, intents)
    try {
      const menu = handle.container.querySelector<HTMLElement>(".command-menu")
      expect(menu).not.toBeNull()
      const items = menu?.querySelectorAll(".command-item")
      expect(items?.length).toBe(2)
      const disabled = menu?.querySelector<HTMLElement>('[data-disabled="true"]')
      expect(disabled?.textContent).toContain("当前任务结束或交互完成后可用")
    } finally {
      handle.unmount()
    }

    const handle2 = mountComposer(makeSnapshot({ draft: "//x", commandMenuOpen: false }), intents)
    try {
      expect(handle2.container.querySelector(".command-menu")).toBeNull()
    } finally {
      handle2.unmount()
    }
  })

  test("disabled 命令点击不 select；可用命令点击 dispatch command-menu-select", () => {
    const intents: WebIntent[] = []
    const snapshot = makeSnapshot({
      draft: "/",
      commandMenuOpen: true,
      commandOptions: [SAMPLE_COMMAND, DISABLED_COMMAND],
      commandMenuIndex: 0,
    })
    const handle = mountComposer(snapshot, intents)
    try {
      const items = handle.container.querySelectorAll<HTMLButtonElement>(".command-item")
      act(() => { items[1]?.click() })
      expect(intents.find(intent => intent.type === "command-menu-select")).toBeUndefined()
      act(() => { items[0]?.click() })
      const select = intents.find(intent => intent.type === "command-menu-select")
      expect(select).toBeDefined()
      if (select && select.type === "command-menu-select") {
        expect(select.item.kind).toBe("command")
      }
    } finally {
      handle.unmount()
    }
  })

})

test("Tab 空闲且无草稿时切换 Work Mode；输入中/运行中不劫持", () => {
  const base = {
    key: "",
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    isComposing: false,
    menuVisible: false,
    items: [] as readonly CommandMenuItem[],
    selectedIndex: 0,
    draft: "",
    composedDisabled: false,
    activeRun: false,
  }
  expect(resolveComposerKeyboardIntent({ ...base, key: "Tab" }))
    .toEqual({ preventDefault: true, intent: { type: "work-mode-cycle" } })
  // 输入中保留默认行为（焦点移动）
  expect(resolveComposerKeyboardIntent({ ...base, key: "Tab", draft: "你好" }))
    .toEqual({ preventDefault: false, intent: null })
  // 运行中不劫持
  expect(resolveComposerKeyboardIntent({ ...base, key: "Tab", activeRun: true }))
    .toEqual({ preventDefault: false, intent: null })
  // 菜单打开时 Tab 保留选择语义
  expect(resolveComposerKeyboardIntent({ ...base, key: "Tab", menuVisible: true, items: [SAMPLE_COMMAND] }))
    .toEqual({ preventDefault: true, intent: { type: "command-menu-select", item: SAMPLE_COMMAND } })
})
