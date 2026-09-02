/** HC-166 第一轮 Shell grammar 与公开契约的先行失败测试。 */

import { expect, test } from "bun:test"
import { PassThrough, Readable } from "node:stream"
import { parseArgs } from "../src/args"
import {
  clientCapabilities,
  clientInteractionHandles,
  readPluginConsent,
} from "../src/index"

test("Plugin grammar uses names and scope, with install enabled in one operation", () => {
  expect(parseArgs(["plugins", "install", "./review", "--scope", "workspace"], "/work")).toEqual({
    kind: "plugins.install",
    cwd: "/work",
    configPath: undefined,
    params: { source: "./review", scope: "workspace" },
  })
  expect(parseArgs(["plugins", "inspect", "Review-Tools"], "/work")).toMatchObject({
    kind: "plugins.inspect",
    params: { name: "Review-Tools", scope: "user" },
  })
  expect(parseArgs(["plugins", "list"], "/work")).toMatchObject({
    kind: "plugins.list",
    params: { scope: "user" },
  })
  expect(parseArgs(["plugins", "update", "Review-Tools", "--source", "./review-v2"], "/work")).toMatchObject({
    kind: "plugins.update",
    params: { name: "Review-Tools", source: "./review-v2" },
  })
  expect(() => parseArgs(["plugins", "install", "./review", "--format", "qwen-code"], "/work")).toThrow()
  expect(() => parseArgs(["plugins", "enable", "Review-Tools", "--capability-fingerprint", "a".repeat(64)], "/work")).toThrow()
})

test("Plugin settings grammar does not expose internal CAS identity", () => {
  expect(parseArgs(["plugins", "settings", "list", "Review-Tools", "--scope", "workspace"], "/work")).toEqual({
    kind: "plugins.settings.list",
    cwd: "/work",
    configPath: undefined,
    params: { name: "Review-Tools", scope: "workspace" },
  })
  expect(parseArgs(["plugins", "settings", "set", "Review-Tools", "API_TOKEN", "--secret-stdin"], "/work")).toMatchObject({
    kind: "plugins.settings.set",
    params: { name: "Review-Tools", setting: "API_TOKEN", scope: "user" },
    secretStdin: true,
  })
})

test("Only Shell install/update advertises plugin consent handling on a real TTY", () => {
  const terminal = {
    stdin: { isTTY: true },
    stdout: { isTTY: true },
    stderr: { isTTY: true },
  }
  expect(clientInteractionHandles(parseArgs(["plugins", "install", "./review"], "/work"), terminal)).toEqual(["plugin_consent"])
  expect(clientInteractionHandles(parseArgs(["plugins", "update", "Review-Tools"], "/work"), terminal)).toEqual(["plugin_consent"])
  expect(clientInteractionHandles(parseArgs(["plugins", "list"], "/work"))).toEqual([])
  expect(clientCapabilities(parseArgs(["plugins", "update", "Review-Tools"], "/work"))).toContain("plugins.manage")
})

test("Plugin consent 的 TTY 边界：TTY 可 accept/cancel，pipe y 和 EOF 都要求 consent", async () => {
  const request = {
    request_id: "plugin-consent-test",
    type: "plugin_consent" as const,
    payload: {
      operation: "install" as const,
      preview: { name: "review-tools", components: [], warnings: [] },
    },
  }
  const terminal = (input: string, isTTY: boolean) => ({
    stdin: Object.assign(Readable.from([input]), { isTTY }),
    stdout: Object.assign(new PassThrough(), { isTTY }),
    stderr: Object.assign(new PassThrough(), { isTTY }),
  })

  await expect(readPluginConsent(request, terminal("y\n", true))).resolves.toMatchObject({ decision: "accept" })
  await expect(readPluginConsent(request, terminal("n\n", true))).resolves.toMatchObject({ decision: "cancel" })
  await expect(readPluginConsent(request, terminal("y\n", false))).rejects.toThrow("PLUGIN_CONSENT_REQUIRED")
  await expect(readPluginConsent(request, terminal("", true))).rejects.toThrow("PLUGIN_CONSENT_REQUIRED")
})

test("Plugin consent 缺少任一真实终端流时不声明 handler，不能被 pipe y 绕过", () => {
  const install = parseArgs(["plugins", "install", "./review"], "/work")
  const tty = {
    stdin: { isTTY: true },
    stdout: { isTTY: true },
    stderr: { isTTY: true },
  }
  expect(clientInteractionHandles(install, { ...tty, stdin: { isTTY: false } })).toEqual([])
  expect(clientInteractionHandles(install, { ...tty, stdout: { isTTY: false } })).toEqual([])
  expect(clientInteractionHandles(install, { ...tty, stderr: { isTTY: false } })).toEqual([])
})
