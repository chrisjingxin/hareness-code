/** task 派出视图：角色 + 任务描述，不把 JSON 键铺给用户。 */

import { expect, test } from "bun:test"
import {
  parseTaskDispatch,
  taskDispatchLabel,
  taskDispatchPrimaryLine,
} from "../../src/presentation-shared/task-dispatch"

test("从 subagent_type 与 description 抽出派出视图", () => {
  const view = parseTaskDispatch(JSON.stringify({
    description: "查找代码压缩相关实现",
    subagent_type: "general-purpose",
  }))
  expect(view).toEqual({
    agentId: "general-purpose",
    description: "查找代码压缩相关实现",
  })
  expect(taskDispatchLabel(view)).toBe("派出 general-purpose")
  expect(taskDispatchPrimaryLine(view)).toBe("查找代码压缩相关实现")
})

test("兼容 agent 别名和 prompt 回退，忽略空白", () => {
  expect(parseTaskDispatch(JSON.stringify({
    prompt: "  只读摸清目录  ",
    agent: "explore",
  }))).toEqual({
    agentId: "explore",
    description: "只读摸清目录",
  })
})

test("缺角色时仍给出派出标题，畸形 JSON 得到空视图", () => {
  expect(taskDispatchLabel(parseTaskDispatch("{\"description\":\"查一下\"}"))).toBe("派出子代理")
  expect(parseTaskDispatch("{")).toEqual({ agentId: null, description: null })
  expect(parseTaskDispatch(undefined)).toEqual({ agentId: null, description: null })
  expect(taskDispatchPrimaryLine({ agentId: null, description: null })).toBeNull()
})

test("主参数行截成单行，不拼接 JSON 键名", () => {
  const line = taskDispatchPrimaryLine({
    agentId: "general-purpose",
    description: "查找 harness-code\n项目中与代码上下文压缩相关的实现",
  }, 20)
  expect(line).toBe("查找 harness-code 项目中…")
  expect(line).not.toContain("subagent_type")
  expect(line).not.toContain("description")
})
