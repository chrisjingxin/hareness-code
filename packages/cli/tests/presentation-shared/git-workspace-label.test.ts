/** gitWorkspaceLabel：四种探测结果与未探测状态渲染为稳定短文案。 */

import { expect, test } from "bun:test"

import { gitWorkspaceLabel } from "../../src/presentation-shared"

test("Git 状态标签覆盖四种状态与未探测", () => {
  expect(gitWorkspaceLabel(undefined)).toBeUndefined()
  expect(gitWorkspaceLabel({ kind: "branch", branch: "main", root: "/r" })).toBe("main")
  expect(gitWorkspaceLabel({ kind: "detached", shortSha: "abc1234", root: "/r" })).toBe("detached@abc1234")
  expect(gitWorkspaceLabel({ kind: "not-repository" })).toBe("非 Git 工作区")
  expect(gitWorkspaceLabel({ kind: "unavailable", message: "Git 状态不可用" })).toBe("Git 状态不可用")
})
