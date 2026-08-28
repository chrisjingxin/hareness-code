import { expect, test } from "bun:test"

import { parseArgs } from "../src/args"

test("parses a non-interactive JSON run", () => {
  expect(parseArgs(["--non-interactive", "summarize this", "--json", "--config", "/tmp/za38.toml"], "/work")).toEqual({
    kind: "run",
    message: "summarize this",
    nonInteractive: true,
    json: true,
    cwd: "/work",
    configPath: "/tmp/za38.toml",
    resume: false,
    sandbox: undefined,
  })
})

test("--resume 只打开交互式 thread 选择器，不接受 thread_id", () => {
  expect(parseArgs(["--resume"], "/work")).toMatchObject({ kind: "run", resume: true, nonInteractive: false })
  expect(() => parseArgs(["--resume", "thread-secret"], "/work")).toThrow("does not accept a thread id")
  expect(() => parseArgs(["--resume=thread-secret"], "/work")).toThrow("does not accept a thread id")
  expect(() => parseArgs(["--resume", "-n", "继续"], "/work")).toThrow("requires the interactive TUI")
  expect(() => parseArgs(["--continue"], "/work")).toThrow("not supported")
})

test("sandbox 开关只接受企业远端模式或显式关闭", () => {
  expect(parseArgs(["--sandbox"], "/work").sandbox).toBe("remote")
  expect(parseArgs(["--sandbox=false"], "/work").sandbox).toBeFalse()
  expect(() => parseArgs(["--sandbox=docker"], "/work")).toThrow("only supports remote")
})

test("parses the read-only config management commands", () => {
  expect(parseArgs(["config", "show", "--config", "/tmp/za38.toml"], "/work")).toEqual({
    kind: "config.show",
    cwd: "/work",
    configPath: "/tmp/za38.toml",
  })
})

test("parses Skill catalog and management commands", () => {
  expect(parseArgs(["skills", "list"], "/work")).toEqual({
    kind: "skills.list",
    cwd: "/work",
    configPath: undefined,
    params: { include_disabled: true },
  })
  expect(parseArgs(["skills", "inspect", "project/review"], "/work")).toMatchObject({
    kind: "skills.inspect",
    params: { id: "project/review" },
  })
  expect(parseArgs(["skills", "trust", "--workspace", "/work", "project/review"], "/other")).toMatchObject({
    kind: "skills.set_enabled",
    cwd: "/work",
    params: { id: "project/review", enabled: true },
  })
  expect(parseArgs(["skills", "install", "review", "--market", "enterprise", "--version", "1.2.0"], "/work")).toMatchObject({
    kind: "skills.install",
    params: { market: "enterprise", name: "review", version: "1.2.0" },
  })
})

test("parses Plugin validation, install, trust and removal commands", () => {
  expect(parseArgs(["plugins", "validate", "./review.zip", "--format", "claude-code"], "/work")).toEqual({
    kind: "plugins.validate",
    cwd: "/work",
    configPath: undefined,
    params: { source: "./review.zip", format: "claude-code" },
  })
  expect(parseArgs(["plugins", "install", "./review"], "/work")).toMatchObject({
    kind: "plugins.install",
    params: { source: "./review", format: "auto" },
  })
  expect(parseArgs([
    "plugins",
    "enable",
    "local-source/review",
    "--capability-fingerprint",
    "a".repeat(64),
  ], "/work")).toMatchObject({
    kind: "plugins.set_enabled",
    params: {
      id: "local-source/review",
      enabled: true,
      capability_fingerprint: "a".repeat(64),
    },
  })
  expect(parseArgs(["plugins", "remove", "local-source/review", "--purge-data"], "/work")).toMatchObject({
    kind: "plugins.remove",
    params: { id: "local-source/review", purge_data: true },
  })
  expect(() => parseArgs(["plugins", "enable", "local-source/review"], "/work")).toThrow("capability-fingerprint")
  expect(() => parseArgs(["plugins", "install", "./review", "--format", "gemini"], "/work")).toThrow("only supports")
})

test("requires a prompt for non-interactive mode", () => {
  expect(() => parseArgs(["--non-interactive"])).toThrow("requires a value")
})

test("parses harness logs list and --run forms with filters, limit, cursor, --cwd", () => {
  expect(parseArgs(["logs"], "/work")).toEqual({
    kind: "logs",
    cwd: "/work",
    json: false,
    flat: false,
    limit: 20,
    thread: undefined,
    run: undefined,
    level: undefined,
    event: undefined,
    component: undefined,
    cursor: undefined,
  })

  expect(parseArgs(["logs", "--run", "abc123def", "--level", "warn", "--component", "agent", "--limit", "50", "--json", "--cwd", "/proj"], "/work")).toEqual({
    kind: "logs",
    cwd: "/proj",
    json: true,
    flat: false,
    limit: 50,
    thread: undefined,
    run: "abc123def",
    level: "warn",
    event: undefined,
    component: "agent",
    cursor: undefined,
  })

  expect(parseArgs(["logs", "--run", "abc123def", "--flat", "--event", "tool", "--cursor", "eyJ2IjoxfQ"], "/work")).toMatchObject({
    kind: "logs",
    limit: 200,
    run: "abc123def",
    flat: true,
    event: "tool",
    cursor: "eyJ2IjoxfQ",
  })
  expect(() => parseArgs(["logs", "--cursor", "eyJ2IjoxfQ"])).toThrow("--cursor requires --thread or --run")
  expect(() => parseArgs(["logs", "--run", "abc", "--cursor", "eyJ2IjoxfQ"])).toThrow("--cursor requires --flat or --json")
  expect(() => parseArgs(["logs", "--flat"])).toThrow("--flat requires --thread or --run")
  expect(() => parseArgs(["logs", "--run", "abc", "--flat", "--json"])).toThrow("--flat and --json are mutually exclusive")

  // limit 边界
  expect(() => parseArgs(["logs", "--limit", "0"])).toThrow("--limit must be integer 1..1000")
  expect(() => parseArgs(["logs", "--limit", "1001"])).toThrow("--limit must be integer 1..1000")
  expect(() => parseArgs(["logs", "--level", "trace"])).toThrow("--level must be one of")
  expect(() => parseArgs(["logs", "--component", "sidecar"])).toThrow("--component must be cli or agent")

  // 不接受 config
  expect(() => parseArgs(["logs", "--config", "/x.toml"])).toThrow("does not accept --config")
})

test("parses logs --thread and rejects conflicting or missing selectors", () => {
  expect(parseArgs(["logs", "--thread", "thread-prefix"], "/work")).toMatchObject({
    kind: "logs",
    thread: "thread-prefix",
    run: undefined,
    flat: false,
    limit: 200,
  })
  expect(() => parseArgs(["logs", "--thread"])).toThrow("--thread requires a value")
  expect(() => parseArgs(["logs", "--run"])).toThrow("--run requires a value")
  expect(() => parseArgs([
    "logs",
    "--thread",
    "thread-prefix",
    "--run",
    "run-prefix",
  ])).toThrow("THREAD_RUN_CONFLICT")
})

test("logs 命令使用 --cwd 覆盖工作区", () => {
  const c = parseArgs(["logs", "--cwd", "/other"], "/work")
  expect(c.kind).toBe("logs")
  expect((c as any).cwd).toBe("/other")
})
