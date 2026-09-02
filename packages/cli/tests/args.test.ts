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

test("parses Plugin validation, installation, name/scope management and removal commands", () => {
  expect(parseArgs(["plugins", "validate", "./review.zip", "--format", "claude-code"], "/work")).toEqual({
    kind: "plugins.validate",
    cwd: "/work",
    configPath: undefined,
    params: { source: "./review.zip", format: "claude-code" },
  })
  expect(parseArgs(["plugins", "install", "./review"], "/work")).toMatchObject({
    kind: "plugins.install",
    params: { source: "./review", scope: "user" },
  })
  expect(parseArgs([
    "plugins",
    "enable",
    "Review-Tools",
    "--scope",
    "workspace",
  ], "/work")).toMatchObject({
    kind: "plugins.set_enabled",
    params: {
      name: "Review-Tools",
      enabled: true,
      scope: "workspace",
    },
  })
  expect(parseArgs(["plugins", "update", "Review-Tools", "--source", "./review-v2"], "/work")).toMatchObject({
    kind: "plugins.update",
    params: { name: "Review-Tools", source: "./review-v2" },
  })
  expect(parseArgs(["plugins", "remove", "Review-Tools", "--purge-data"], "/work")).toMatchObject({
    kind: "plugins.remove",
    params: { name: "Review-Tools", purge_data: true },
  })
  expect(() => parseArgs(["plugins", "enable", "Review-Tools", "--capability-fingerprint", "a".repeat(64)], "/work")).toThrow()
  expect(() => parseArgs(["plugins", "install", "./review", "--format", "qwen-code"], "/work")).toThrow()
  expect(parseArgs(["plugins", "validate", "./za38-extension", "--format", "qwen-code"], "/work")).toMatchObject({
    kind: "plugins.validate",
    params: { source: "./za38-extension", format: "qwen-code" },
  })
})

test("parses Plugin Settings management without exposing internal identity in argv", () => {
  expect(parseArgs(["plugins", "settings", "list"], "/work")).toMatchObject({
    kind: "plugins.settings.list",
    params: { scope: "user" },
  })
  expect(parseArgs(["plugins", "settings", "list", "--scope", "workspace"], "/work")).toEqual({
    kind: "plugins.settings.list",
    cwd: "/work",
    configPath: undefined,
    params: { scope: "workspace" },
  })
  expect(parseArgs([
    "plugins",
    "settings",
    "set",
    "Review-Tools",
    "ZA38_TOKEN",
    "--scope",
    "user",
    "--secret-stdin",
  ], "/work")).toEqual({
    kind: "plugins.settings.set",
    cwd: "/work",
    configPath: undefined,
    params: {
      name: "Review-Tools",
      setting: "ZA38_TOKEN",
      scope: "user",
    },
    secretStdin: true,
  })
  expect(() => parseArgs([
    "plugins", "settings", "set", "Review-Tools", "ZA38_TOKEN", "fake-secret", "--secret-stdin",
  ], "/work")).toThrow("does not accept a value")
})

test("Plugin Settings 按动作严格拒绝未知 option、缺值和重复 scope", () => {

  expect(() => parseArgs([
    "plugins", "settings", "set", "Review-Tools", "ZA38_TOKEN",
    "--totally-unknown", "plugin/x", "--also-unknown", "TOKEN",
    "--secret-stdin",
  ], "/work")).toThrow("unsupported option")
  expect(() => parseArgs([
    "plugins", "settings", "list", "--totally-unknown",
  ], "/work")).toThrow("unsupported option")
  expect(() => parseArgs([
    "plugins", "settings", "remove", "Review-Tools", "ZA38_TOKEN", "--secret-stdin",
  ], "/work")).toThrow("unsupported option")
  expect(() => parseArgs([
    "plugins", "settings", "list", "--secret-stdin",
  ], "/work")).toThrow("unsupported option")
  expect(() => parseArgs([
    "plugins", "settings", "set", "Review-Tools", "ZA38_TOKEN",
    "--scope", "user", "--scope", "workspace", "--secret-stdin",
  ], "/work")).toThrow("may only be specified once")
  expect(() => parseArgs([
    "plugins", "settings", "set", "Review-Tools", "ZA38_TOKEN",
    "--scope", "--secret-stdin",
  ], "/work")).toThrow("--scope requires a value")
  expect(() => parseArgs([
    "plugins", "settings", "set", "Review-Tools", "ZA38_TOKEN",
    "--package-digest", "a".repeat(64), "--secret-stdin",
  ], "/work")).toThrow("unsupported option")
})

test("Plugin Settings 拒绝同时选择 --workspace 与 --cwd", () => {
  const mutation = ["Review-Tools", "ZA38_TOKEN"]

  for (const action of ["list", "set", "remove"] as const) {
    const args = ["plugins", "settings", action, "--workspace", "/a", "--cwd", "/b"]
    if (action === "list") {
      expect(() => parseArgs(args, "/fallback")).toThrow("--workspace and --cwd are mutually exclusive")
    } else {
      expect(() => parseArgs([...args, ...mutation, ...(action === "set" ? ["--secret-stdin"] : [])], "/fallback"))
        .toThrow("--workspace and --cwd are mutually exclusive")
    }
  }
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
