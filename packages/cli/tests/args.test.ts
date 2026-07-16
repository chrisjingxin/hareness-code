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
    threadId: undefined,
    sandbox: undefined,
  })
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

test("requires a prompt for non-interactive mode", () => {
  expect(() => parseArgs(["--non-interactive"])).toThrow("requires a value")
})
