/** 跨端共享模型展示策略：选择优先、回退运行时；TUI 与 Web 必须一致。 */

import { expect, test } from "bun:test"

import { modelSelectionLabel } from "../../src/presentation-shared"

function view(overrides: Partial<Parameters<typeof modelSelectionLabel>[0]> = {}): Parameters<typeof modelSelectionLabel>[0] {
  return {
    selection: { requestedModelProfileId: null, actualModel: null },
    catalogs: {
      models: {
        items: [
          { id: "fast", model: "fast-model", provider_label: "fast", context_window_tokens: 128000, capabilities: [], is_default: true, available: true, source: "user" },
          { id: "pro", model: "pro-model", provider_label: "pro", context_window_tokens: 256000, capabilities: [], is_default: false, available: true, source: "user" },
        ],
      },
    },
    runtime: { modelConfigured: true, modelName: "pro-model", modelProfileId: undefined },
    ...overrides,
  }
}

test("/model 显式选择优先于陈旧的握手 runtime.modelName", () => {
  expect(modelSelectionLabel(view({ selection: { requestedModelProfileId: "fast", actualModel: null } }))).toBe("fast · fast-model")
})

test("实际绑定优先于显式选择", () => {
  expect(modelSelectionLabel(view({
    selection: {
      requestedModelProfileId: "fast",
      actualModel: { id: "pro", model: "pro-model", provider_label: "pro" },
    },
  }))).toBe("pro · pro-model")
})

test("未选择时回退到握手运行时配置", () => {
  expect(modelSelectionLabel(view())).toBe("pro-model")
  expect(modelSelectionLabel(view({ runtime: { modelConfigured: true, modelName: undefined, modelProfileId: "fast" } }))).toBe("fast · 已配置模型")
})

test("未配置模型返回明确文案", () => {
  expect(modelSelectionLabel(view({ runtime: { modelConfigured: false, modelName: undefined, modelProfileId: undefined } }))).toBe("未配置模型")
})
