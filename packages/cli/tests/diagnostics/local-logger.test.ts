/** 本地诊断日志测试：级别、权限和字段截断。 */

import { afterEach, expect, test } from "bun:test"
import { mkdtemp, readFile, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { createDiagnosticLogger } from "../../src/diagnostics/local-logger"

const directories: string[] = []
afterEach(() => {
  // 临时目录由系统清理；测试不执行递归删除，避免误删风险。
  directories.length = 0
})

test("默认 info 日志不包含 debug 字段", async () => {
  const directory = await mkdtemp(join(tmpdir(), "harness-log-info-"))
  directories.push(directory)
  const logger = createDiagnosticLogger({
    directory,
    level: "info",
    now: () => new Date("2026-08-04T00:00:00.000Z"),
    pid: 7,
  })
  logger.info("web.handoff.opening", { phase: "opening" })
  logger.debug("web.internal", { hidden: "yes" })
  logger.error("web.bootstrap.failed", { stage: "agent.initialize" }, { message: "sensitive detail" })
  logger.close()

  const text = await readFile(logger.filePath!, "utf8")
  expect(text).toContain('"level":"info"')
  expect(text).toContain('"stage":"agent.initialize"')
  expect(text).not.toContain("web.internal")
  expect(text).not.toContain("sensitive detail")
  expect((await stat(directory)).mode & 0o777).toBe(0o700)
  expect((await stat(logger.filePath!)).mode & 0o777).toBe(0o600)
})

test("debug 模式记录经过单行化和截断的错误摘要", async () => {
  const directory = await mkdtemp(join(tmpdir(), "harness-log-debug-"))
  directories.push(directory)
  const logger = createDiagnosticLogger({ directory, level: "DEBUG", pid: 8 })
  logger.error("web.bootstrap.failed", {}, { message: `line1\n${"x".repeat(300)}` })
  logger.close()

  const lines = (await readFile(logger.filePath!, "utf8")).trim().split("\n")
  expect(lines).toHaveLength(1)
  const entry = JSON.parse(lines[0]!) as { message: string }
  expect(entry.message).not.toContain("\n")
  expect(entry.message.endsWith("…")).toBe(true)
})
