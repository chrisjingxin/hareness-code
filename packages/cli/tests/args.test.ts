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
