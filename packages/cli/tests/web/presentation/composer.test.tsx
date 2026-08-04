/** Composer：受控 draft、发送/取消、Skill chip、命令菜单与键盘交互。 */
/** @jsxImportSource react */

import { describe, expect, test } from "bun:test"
import { act } from "react"
import { createElement, type ReactElement } from "react"

import { Composer } from "../../../src/web/presentation/composer"
import type {
  CommandMenuItem,
  SkillMenuItem,
} from "../../../src/interactive/commands"
import type { WebAdapterSnapshot, WebIntent } from "../../../src/web/application/adapter"
import { makeInteractive, makeSnapshot, makeSkill } from "./fixtures"
import { changeValue, pressKey, render, type RenderHandle } from "./render"

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
  test("textarea 跟随 snapshot.draft；输入 dispatch draft-change", () => {
    const intents: WebIntent[] = []
    const handle = mountComposer(makeSnapshot({ draft: "初始" }), intents)
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      expect(textarea?.value).toBe("初始")
      changeValue(textarea!, "新的输入")
      expect(intents[0]).toEqual({ type: "draft-change", value: "新的输入" })
    } finally {
      handle.unmount()
    }
  })

  test("Enter 触发 submit（保留原值，不 trim）；Shift+Enter 不提交", () => {
    const intents: WebIntent[] = []
    const handle = mountComposer(makeSnapshot({ draft: "你好" }), intents)
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      act(() => {
        pressKey(textarea!, { key: "Enter" })
      })
      expect(intents[0]).toEqual({ type: "submit" })
      intents.length = 0
      act(() => {
        pressKey(textarea!, { key: "Enter", shiftKey: true })
      })
      expect(intents).toEqual([])
    } finally {
      handle.unmount()
    }
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

  test("键盘 ArrowDown 改变 hover 索引并 dispatch command-menu-hover", () => {
    const intents: WebIntent[] = []
    const snapshot = makeSnapshot({
      draft: "/",
      commandMenuOpen: true,
      commandOptions: [SAMPLE_COMMAND, DISABLED_COMMAND, SAMPLE_COMMAND],
      commandMenuIndex: 0,
    })
    const handle = mountComposer(snapshot, intents)
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      act(() => {
        pressKey(textarea!, { key: "ArrowDown" })
      })
      expect(intents).toContainEqual({ type: "command-menu-hover", selectedIndex: 1 })
    } finally {
      handle.unmount()
    }
  })

  test("菜单打开时按 Enter 在可用项上 dispatch command-menu-select", () => {
    const intents: WebIntent[] = []
    const snapshot = makeSnapshot({
      draft: "/",
      commandMenuOpen: true,
      commandOptions: [SAMPLE_COMMAND],
      commandMenuIndex: 0,
    })
    const handle = mountComposer(snapshot, intents)
    try {
      const textarea = handle.container.querySelector<HTMLTextAreaElement>(".composer-textarea")
      act(() => {
        pressKey(textarea!, { key: "Enter" })
      })
      const select = intents.find(intent => intent.type === "command-menu-select")
      expect(select).toBeDefined()
    } finally {
      handle.unmount()
    }
  })
})
