/** 仓库协作脚本的回归测试：所有文件系统操作均限定在临时项目目录。 */

import { expect, test } from "bun:test"
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import {
  checkDocs,
  checkRelease,
  checkTasks,
  claimTask,
  compareSemVer,
  completeTask,
  loadTasks,
  parseSemVer,
  renderChangelogSection,
  renderTaskBoard,
  setVersion,
  syncTasks,
} from "../../../scripts/project-management"

const taskMetadata = {
  id: "ZC-001",
  title: "测试任务",
  priority: "P0",
  status: "待认领",
  owner: "未认领",
  branch: "-",
  scope: "验证协作脚本。",
  acceptance: "命令可执行。",
  user_docs: "不涉及",
  developer_docs: "docs/developer/任务看板说明.md",
  test_evidence: "-",
  references: "-",
  completed_at: "-",
}

/** 创建最小仓库夹具，便于验证项目级脚本而不影响真实工作区。 */
async function createFixture(): Promise<string> {
  const projectRoot = await mkdtemp(join(tmpdir(), "za38-project-management-"))
  await Promise.all([
    mkdir(join(projectRoot, "docs/user"), { recursive: true }),
    mkdir(join(projectRoot, "docs/developer/adr"), { recursive: true }),
    mkdir(join(projectRoot, "docs/developer/tasks"), { recursive: true }),
    mkdir(join(projectRoot, "packages/cli/src/tui"), { recursive: true }),
    mkdir(join(projectRoot, "packages/protocol"), { recursive: true }),
    mkdir(join(projectRoot, "packages/agent/harness_agent"), { recursive: true }),
  ])

  await Promise.all([
    writeFile(join(projectRoot, "README.md"), "# 入口\n\n[快速开始](docs/user/快速开始.md)\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/快速开始.md"), "# 快速开始\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/模型配置.md"), "# 模型配置\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/交互使用.md"), "# 交互使用\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/故障排查.md"), "# 故障排查\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/架构总览.md"), "# 架构总览\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/开发工作流.md"), "# 开发工作流\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/变更检查清单.md"), "# 变更检查清单\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/任务看板说明.md"), "# 任务看板说明\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/adr/README.md"), "# ADR\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/tasks/README.md"), "# 任务\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/tasks/ZC-001.md"), renderTask(taskMetadata), "utf8"),
    writeFile(join(projectRoot, "packages/cli/package.json"), '{"name":"cli","version":"0.0.0"}\n', "utf8"),
    writeFile(join(projectRoot, "packages/protocol/package.json"), '{"name":"protocol","version":"0.0.0"}\n', "utf8"),
    writeFile(join(projectRoot, "packages/agent/pyproject.toml"), '[project]\nversion = "0.0.0"\n', "utf8"),
    writeFile(join(projectRoot, "packages/agent/harness_agent/__init__.py"), '__version__ = "0.0.0"\n', "utf8"),
    writeFile(join(projectRoot, "packages/cli/src/tui/model.ts"), 'export const CLI_VERSION = "0.0.0"\n', "utf8"),
  ])
  await syncTasks(projectRoot)
  return projectRoot
}

function renderTask(metadata: Record<string, string>): string {
  return `---\n${Object.entries(metadata).map(([key, value]) => `${key}: ${value}`).join("\n")}\n---\n\n任务正文。\n`
}

test("任务状态校验拒绝缺少认领信息和完成证据的事项", async () => {
  const projectRoot = await createFixture()
  try {
    const taskPath = join(projectRoot, "docs/developer/tasks/ZC-001.md")
    await writeFile(taskPath, renderTask({ ...taskMetadata, status: "进行中" }), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("必须填写 owner 和 branch")

    await writeFile(taskPath, renderTask({ ...taskMetadata, status: "已完成", owner: "agent", branch: "codex/test" }), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("必须填写测试证据")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("认领和完成任务会同步状态、证据与只读看板", async () => {
  const projectRoot = await createFixture()
  try {
    await claimTask(projectRoot, "ZC-001", "codex", "codex/tasks")
    const claimed = (await loadTasks(projectRoot))[0]
    expect(claimed?.metadata.status).toBe("进行中")
    expect(claimed?.metadata.owner).toBe("codex")

    await completeTask(projectRoot, "ZC-001", "bun test packages/cli/tests/project-management.test.ts", "abc123")
    const completed = (await loadTasks(projectRoot))[0]
    expect(completed?.metadata.status).toBe("已完成")
    expect(completed?.metadata.test_evidence).toContain("bun test")
    expect(completed?.metadata.references).toBe("abc123")
    await expect(checkTasks(projectRoot)).resolves.toBeUndefined()
    expect(await readFile(join(projectRoot, "docs/developer/任务看板.md"), "utf8")).toContain("已完成")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("任务看板以优先级和任务 ID 稳定排序", async () => {
  const projectRoot = await createFixture()
  try {
    await writeFile(join(projectRoot, "docs/developer/tasks/ZC-001.md"), renderTask({ ...taskMetadata, priority: "P2" }), "utf8")
    await writeFile(join(projectRoot, "docs/developer/tasks/ZC-002.md"), renderTask({ ...taskMetadata, id: "ZC-002", priority: "P0" }), "utf8")
    await syncTasks(projectRoot)
    const board = await readFile(join(projectRoot, "docs/developer/任务看板.md"), "utf8")
    expect(board.indexOf("ZC-002")).toBeLessThan(board.indexOf("ZC-001"))
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("文档校验会拒绝失效链接与不存在的任务引用", async () => {
  const projectRoot = await createFixture()
  try {
    await expect(checkDocs(projectRoot)).resolves.toBeUndefined()
    await writeFile(join(projectRoot, "README.md"), "[失效](docs/user/不存在.md)\n", "utf8")
    await expect(checkDocs(projectRoot)).rejects.toThrow("无效本地链接")
    await writeFile(join(projectRoot, "README.md"), "提及 ZC-999。\n", "utf8")
    await expect(checkDocs(projectRoot)).rejects.toThrow("不存在的任务")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("SemVer 与 Changelog 按预发布规则比较并分类提交", () => {
  expect(compareSemVer(parseSemVer("1.0.0"), parseSemVer("1.0.0-rc.1"))).toBeGreaterThan(0)
  expect(compareSemVer(parseSemVer("1.0.0-beta.2"), parseSemVer("1.0.0-beta.11"))).toBeLessThan(0)
  const section = renderChangelogSection("1.2.0", "2026-07-15", ["feat(cli): 新入口", "fix: 修复边界", "chore: 清理"])
  expect(section).toContain("### 新增")
  expect(section).toContain("### 修复")
  expect(section).toContain("### 其他")
})

test("初次版本初始化同步所有版本文件，并拒绝不一致发布状态", async () => {
  const projectRoot = await createFixture()
  try {
    await writeFile(join(projectRoot, "VERSION"), "0.1.0\n", "utf8")
    await setVersion(projectRoot, "0.1.0", ["feat: 建立协作基础设施"])
    expect((await readFile(join(projectRoot, "VERSION"), "utf8")).trim()).toBe("0.1.0")
    expect(JSON.parse(await readFile(join(projectRoot, "packages/cli/package.json"), "utf8")).version).toBe("0.1.0")
    expect(await readFile(join(projectRoot, "CHANGELOG.md"), "utf8")).toContain("### 新增")
    await expect(checkRelease(projectRoot)).resolves.toBeUndefined()
    await expect(setVersion(projectRoot, "0.1.0", [])).rejects.toThrow("新版本必须高于当前版本")

    await writeFile(join(projectRoot, "packages/protocol/package.json"), '{"version":"0.2.0"}\n', "utf8")
    await expect(checkRelease(projectRoot)).rejects.toThrow("版本与 VERSION 不一致")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})
