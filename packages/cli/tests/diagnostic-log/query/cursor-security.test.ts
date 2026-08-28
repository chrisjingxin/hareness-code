/** harness logs cursor 绑定、失效与离线安全边界测试。 */

import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { chmod, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, symlink, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import { queryLogs } from "../../../src/diagnostic-log/query"

function record(fingerprint: string, sequence: number) {
  return {
    schema_version: 1,
    timestamp_ms: 1_787_800_000_000 + sequence,
    level: "info",
    event: "run.started",
    component: "agent",
    process: { pid: 4321, started_at_ms: 1_787_800_000_000, record_sequence: sequence },
    project_fingerprint: fingerprint,
    thread_id: "thread-cursor-security",
    run_id: "run-cursor-security",
    fields: { mode: "build", resumed: false, approval_mode: "default", model_profile_id: "default" },
  }
}

async function setup() {
  const root = await mkdtemp(join(tmpdir(), "harness-cursor-root-"))
  const cwd = await mkdtemp(join(tmpdir(), "harness-cursor-cwd-"))
  const fingerprint = createHash("sha256").update(await realpath(cwd)).digest("hex")
  const directory = join(root, "2026-08-27")
  const file = join(directory, "agent-1787800000000-4321-0001.jsonl")
  await mkdir(directory, { recursive: true })
  const contents = `${[1, 2, 3].map(sequence => JSON.stringify(record(fingerprint, sequence))).join("\n")}\n`
  await writeFile(file, contents)
  return { root, cwd, fingerprint, file, contents }
}

async function firstCursor(value: Awaited<ReturnType<typeof setup>>): Promise<string> {
  const result = await queryLogs({ cwd: value.cwd, json: true, limit: 1, run: "run-cursor" }, value.root)
  expect(result.next_cursor).toBeString()
  return result.next_cursor!
}

function rewriteCursor(token: string, mutate: (value: any) => void): string {
  const value = JSON.parse(Buffer.from(token, "base64url").toString("utf8"))
  mutate(value)
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url")
}

test("snapshot 文件删除、缩小、同尺寸替换和 symlink 替换均令 cursor 过期", async () => {
  for (const mutation of ["delete", "shrink", "replace", "symlink"] as const) {
    const seeded = await setup()
    const outside = join(await mkdtemp(join(tmpdir(), "harness-cursor-outside-")), "outside.jsonl")
    try {
      const cursor = await firstCursor(seeded)
      if (mutation === "delete") await rm(seeded.file)
      if (mutation === "shrink") await writeFile(seeded.file, seeded.contents.slice(0, -20))
      if (mutation === "replace") await writeFile(seeded.file, seeded.contents.replaceAll("build", "other"))
      if (mutation === "symlink") {
        await writeFile(outside, seeded.contents)
        await rm(seeded.file)
        await symlink(outside, seeded.file)
      }
      await expect(queryLogs({
        cwd: seeded.cwd,
        json: true,
        limit: 1,
        run: "run-cursor",
        cursor,
      }, seeded.root)).rejects.toMatchObject({ code: "CURSOR_EXPIRED" })
    } finally {
      await rm(seeded.root, { recursive: true, force: true })
      await rm(seeded.cwd, { recursive: true, force: true })
      await rm(join(outside, ".."), { recursive: true, force: true })
    }
  }
})

test("cursor 拒绝 workspace、selector 和 filter 错配", async () => {
  const seeded = await setup()
  const otherCwd = await mkdtemp(join(tmpdir(), "harness-cursor-other-cwd-"))
  try {
    const cursor = await firstCursor(seeded)
    const cases = [
      { cwd: otherCwd, json: true, limit: 1, run: "run-cursor", cursor },
      { cwd: seeded.cwd, json: true, limit: 1, run: "different", cursor },
      { cwd: seeded.cwd, json: true, limit: 1, run: "run-cursor", level: "warn" as const, cursor },
    ]
    for (const query of cases) {
      await expect(queryLogs(query, seeded.root)).rejects.toMatchObject({ code: "CURSOR_MISMATCH" })
    }
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
    await rm(otherCwd, { recursive: true, force: true })
  }
})

test("cursor 拒绝 malformed、超长、未知字段和路径伪造", async () => {
  const seeded = await setup()
  try {
    const valid = await firstCursor(seeded)
    const invalid = [
      "%not-base64url%",
      "a".repeat(16_385),
      Buffer.from("{}", "utf8").toString("base64url"),
      rewriteCursor(valid, value => { value.unknown = true }),
      rewriteCursor(valid, value => { value.files[0].path = "../outside.jsonl" }),
      rewriteCursor(valid, value => { value.files[0].path = "/tmp/outside.jsonl" }),
    ]
    for (const cursor of invalid) {
      await expect(queryLogs({
        cwd: seeded.cwd,
        json: true,
        limit: 1,
        run: "run-cursor",
        cursor,
      }, seeded.root)).rejects.toMatchObject({ code: "INVALID_CURSOR" })
    }
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("list、thread、run、JSON 和 cursor 查询不修改日志文件", async () => {
  const seeded = await setup()
  try {
    const beforeContents = await readFile(seeded.file)
    const beforeStat = await stat(seeded.file)
    const beforeNames = await readdir(join(seeded.root, "2026-08-27"))
    await queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, seeded.root)
    await queryLogs({ cwd: seeded.cwd, json: false, limit: 200, thread: "thread-cursor" }, seeded.root)
    const first = await queryLogs({ cwd: seeded.cwd, json: true, limit: 1, run: "run-cursor" }, seeded.root)
    await queryLogs({ cwd: seeded.cwd, json: true, limit: 2, run: "run-cursor", cursor: first.next_cursor! }, seeded.root)
    const afterStat = await stat(seeded.file)
    expect(await readFile(seeded.file)).toEqual(beforeContents)
    expect(await readdir(join(seeded.root, "2026-08-27"))).toEqual(beforeNames)
    expect(afterStat.size).toBe(beforeStat.size)
    expect(afterStat.mtimeMs).toBe(beforeStat.mtimeMs)
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("缺失日志根目录成功返回空列表，非目录根返回稳定错误", async () => {
  const seeded = await setup()
  const missing = join(seeded.root, "missing")
  const notDirectory = join(seeded.root, "not-a-directory")
  try {
    await writeFile(notDirectory, "not a log root")
    const empty = await queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, missing)
    expect(empty.threads).toEqual([])
    await expect(queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, notDirectory))
      .rejects.toMatchObject({ code: "LOG_ROOT_UNREADABLE" })
  } finally {
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})

test("整体不可读的日志根目录返回 LOG_ROOT_UNREADABLE", async () => {
  const seeded = await setup()
  try {
    await chmod(seeded.root, 0o000)
    await expect(queryLogs({ cwd: seeded.cwd, json: false, limit: 20 }, seeded.root))
      .rejects.toMatchObject({ code: "LOG_ROOT_UNREADABLE" })
  } finally {
    await chmod(seeded.root, 0o700)
    await rm(seeded.root, { recursive: true, force: true })
    await rm(seeded.cwd, { recursive: true, force: true })
  }
})
