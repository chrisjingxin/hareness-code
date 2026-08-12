/** 仓库协作脚本的回归测试：所有文件系统操作均限定在临时项目目录。 */

import { expect, test } from "bun:test"
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import {
  TASK_ARCHIVE_DIR,
  TASK_BOARD_PATH,
  TASK_DIR,
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
  taskFileName,
} from "./index"

const taskMetadata = {
  id: "HC-001",
  title: "测试任务",
  feature_area: "项目协作基础设施",
  parent_task: "-",
  decomposed_by: "codex",
  priority: "P0",
  status: "待认领",
  owner: "未认领",
  branch: "-",
  reviewed_at: "2026-08-09",
  review_due: "2099-12-31",
  scope: "验证协作脚本。",
  acceptance: "命令可执行。",
  user_docs: "不涉及",
  developer_docs: `${TASK_DIR}/README.md`,
  test_evidence: "-",
  references: "-",
  completed_at: "-",
}

/** 创建最小仓库夹具，便于验证项目级脚本而不影响真实工作区。 */
async function createFixture(): Promise<string> {
  const projectRoot = await mkdtemp(join(tmpdir(), "za38-project-management-"))
  await Promise.all([
    mkdir(join(projectRoot, "docs/user"), { recursive: true }),
    mkdir(join(projectRoot, "docs/developer/architecture"), { recursive: true }),
    mkdir(join(projectRoot, "docs/developer/project"), { recursive: true }),
    mkdir(join(projectRoot, TASK_DIR), { recursive: true }),
    mkdir(join(projectRoot, "packages/cli/src/interactive"), { recursive: true }),
    mkdir(join(projectRoot, "packages/cli/src/tui/application"), { recursive: true }),
    mkdir(join(projectRoot, "packages/protocol"), { recursive: true }),
    mkdir(join(projectRoot, "packages/agent/harness_agent"), { recursive: true }),
  ])

  const taskName = taskFileName(taskMetadata.id, taskMetadata.title)
  await Promise.all([
    writeFile(join(projectRoot, "README.md"), "# 入口\n\n[快速开始](docs/user/快速开始.md)\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/快速开始.md"), "# 快速开始\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/模型配置.md"), "# 模型配置\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/交互使用.md"), "# 交互使用\n", "utf8"),
    writeFile(join(projectRoot, "docs/user/故障排查.md"), "# 故障排查\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/architecture/架构总览.md"), "# 架构总览\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/project/开发工作流.md"), "# 开发工作流\n", "utf8"),
    writeFile(join(projectRoot, "docs/developer/project/变更检查清单.md"), "# 变更检查清单\n", "utf8"),
    writeFile(join(projectRoot, TASK_DIR, "README.md"), "# 任务\n", "utf8"),
    writeFile(join(projectRoot, TASK_DIR, taskName), renderTask(taskMetadata), "utf8"),
    writeFile(join(projectRoot, "packages/cli/package.json"), '{"name":"cli","version":"0.0.0"}\n', "utf8"),
    writeFile(join(projectRoot, "packages/protocol/package.json"), '{"name":"protocol","version":"0.0.0"}\n', "utf8"),
    writeFile(join(projectRoot, "packages/agent/pyproject.toml"), '[project]\nversion = "0.0.0"\n', "utf8"),
    writeFile(join(projectRoot, "packages/agent/harness_agent/__init__.py"), '__version__ = "0.0.0"\n', "utf8"),
    writeFile(join(projectRoot, "packages/cli/src/interactive/runtime.ts"), 'export const CLI_VERSION = "0.0.0"\n', "utf8"),
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
    const taskPath = join(projectRoot, TASK_DIR, taskFileName("HC-001", "测试任务"))
    await writeFile(taskPath, renderTask({ ...taskMetadata, status: "进行中" }), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("必须填写 owner 和 branch")

    await writeFile(taskPath, renderTask({ ...taskMetadata, status: "已完成", owner: "agent", branch: "codex/test" }), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("应位于")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("认领和完成任务会同步状态、证据、归档与只读看板", async () => {
  const projectRoot = await createFixture()
  try {
    await claimTask(projectRoot, "HC-001", "codex", "codex/tasks")
    const claimed = (await loadTasks(projectRoot))[0]
    expect(claimed?.metadata.status).toBe("进行中")
    expect(claimed?.metadata.owner).toBe("codex")

    await completeTask(projectRoot, "HC-001", "bun test scripts/project/project-management.test.ts", "abc123")
    expect(await loadTasks(projectRoot)).toHaveLength(0)
    const archivedPath = join(projectRoot, TASK_ARCHIVE_DIR, taskFileName("HC-001", "测试任务"))
    const archived = await readFile(archivedPath, "utf8")
    expect(archived).toContain("status: 已完成")
    expect(archived).toContain("bun test")
    expect(archived).toContain("abc123")
    await expect(checkTasks(projectRoot)).resolves.toBeUndefined()
    expect(await readFile(join(projectRoot, TASK_BOARD_PATH), "utf8")).not.toContain("HC-001")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("任务看板以优先级和任务 ID 稳定排序", async () => {
  const projectRoot = await createFixture()
  try {
    await writeFile(join(projectRoot, TASK_DIR, taskFileName("HC-001", "测试任务")), renderTask({ ...taskMetadata, priority: "P2" }), "utf8")
    await writeFile(join(projectRoot, TASK_DIR, taskFileName("HC-002", "另一任务")), renderTask({
      ...taskMetadata,
      id: "HC-002",
      title: "另一任务",
      priority: "P0",
    }), "utf8")
    await syncTasks(projectRoot)
    const board = await readFile(join(projectRoot, TASK_BOARD_PATH), "utf8")
    expect(board.indexOf("HC-002")).toBeLessThan(board.indexOf("HC-001"))
    expect(board).toContain("板块：项目协作基础设施")
    expect(board).toContain("拆解：codex")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("活动任务到期必须复核，过时任务必须记录替代依据", async () => {
  const projectRoot = await createFixture()
  try {
    const taskPath = join(projectRoot, TASK_DIR, taskFileName("HC-001", "测试任务"))
    await writeFile(taskPath, renderTask({ ...taskMetadata, reviewed_at: "2019-12-01", review_due: "2020-01-01" }), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("已到复核日期")

    await writeFile(taskPath, renderTask({
      ...taskMetadata,
      status: "已过时",
      review_due: "-",
      references: "HC-002",
    }), "utf8")
    await expect(loadTasks(projectRoot)).resolves.toHaveLength(1)
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("归档任务不进入活动看板，但历史文档引用仍通过校验", async () => {
  const projectRoot = await createFixture()
  try {
    const archiveDirectory = join(projectRoot, TASK_ARCHIVE_DIR)
    await mkdir(archiveDirectory, { recursive: true })
    const archivedName = taskFileName("HC-000", "归档任务")
    await writeFile(join(archiveDirectory, archivedName), renderTask({
      ...taskMetadata,
      id: "HC-000",
      title: "归档任务",
      status: "已完成",
      owner: "codex",
      branch: "codex/archive",
      test_evidence: "bun test",
      completed_at: "2026-07-30",
    }), "utf8")
    await writeFile(join(projectRoot, "README.md"), `提及 HC-000。\n\n[归档](${TASK_ARCHIVE_DIR}/${archivedName})\n`, "utf8")

    await syncTasks(projectRoot)
    expect((await loadTasks(projectRoot)).map(task => task.metadata.id)).toEqual(["HC-001"])
    expect(await readFile(join(projectRoot, TASK_BOARD_PATH), "utf8")).not.toContain("HC-000")
    await expect(checkDocs(projectRoot)).resolves.toBeUndefined()
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("归档研究快照可保留旧任务编号，但本地链接仍需有效", async () => {
  const projectRoot = await createFixture()
  try {
    const archiveDirectory = join(projectRoot, "docs/developer/research/archive")
    await mkdir(archiveDirectory, { recursive: true })
    await writeFile(join(archiveDirectory, "snapshot.md"), `历史任务 HC-999。\n\n[看板](../../task/任务看板.md)\n`, "utf8")
    await expect(checkDocs(projectRoot)).resolves.toBeUndefined()
    await writeFile(join(archiveDirectory, "snapshot.md"), "[失效](../../task/不存在.md)\n", "utf8")
    await expect(checkDocs(projectRoot)).rejects.toThrow("无效本地链接")
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
    await writeFile(join(projectRoot, "README.md"), "提及 HC-999。\n", "utf8")
    await expect(checkDocs(projectRoot)).rejects.toThrow("不存在的任务")
  } finally {
    await rm(projectRoot, { recursive: true, force: true })
  }
})

test("任务文件名必须带功能简介且与 id 一致", async () => {
  const projectRoot = await createFixture()
  try {
    const validPath = join(projectRoot, TASK_DIR, taskFileName("HC-001", "测试任务"))
    await rm(validPath)
    await writeFile(join(projectRoot, TASK_DIR, "HC-001.md"), renderTask(taskMetadata), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("功能简介")

    await rm(join(projectRoot, TASK_DIR, "HC-001.md"))
    await writeFile(join(projectRoot, TASK_DIR, "HC-009-错误简介.md"), renderTask(taskMetadata), "utf8")
    await expect(loadTasks(projectRoot)).rejects.toThrow("不一致")
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

test("renderTaskBoard 文案指向新目录", () => {
  const board = renderTaskBoard([])
  expect(board).toContain("docs/developer/task/")
  expect(board).toContain("docs/developer/task/archive/")
  expect(board).not.toContain("docs/developer/tasks/")
})
