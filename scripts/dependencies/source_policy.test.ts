import { mkdtempSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { resolve } from "node:path"
import { expect, test } from "bun:test"
import {
  DependencyPreflightError,
  expectedBunVersion,
  resolveInternalSources,
  validateBunLockSource,
  validateToolchainVersions,
  validateUvLockSource,
} from "./source_policy"

const INTERNAL_ENV = {
  HARNESS_NPM_REGISTRY: "https://npm.intranet.example/",
  HARNESS_PYPI_INDEX: "https://pypi.intranet.example/simple/",
}
const ROOT = resolve(import.meta.dir, "../..")

test("requires explicit non-public npm and Python sources", () => {
  expect(() => resolveInternalSources({})).toThrow(DependencyPreflightError)
  expect(() => resolveInternalSources({
    HARNESS_NPM_REGISTRY: "https://registry.npmjs.org/",
    HARNESS_PYPI_INDEX: "https://pypi.intranet.example/simple/",
  })).toThrow("公网包源")
  expect(() => resolveInternalSources({
    ...INTERNAL_ENV,
    PIP_INDEX_URL: "https://pypi.org/simple/",
  })).toThrow("PIP_INDEX_URL 配置")
})

test("accepts internal source aliases and rejects public lock provenance", () => {
  const sources = resolveInternalSources(INTERNAL_ENV)
  expect(sources.npmRegistry.hostname).toBe("npm.intranet.example")
  expect(sources.pythonIndex.hostname).toBe("pypi.intranet.example")

  const issues = validateUvLockSource(resolve(ROOT, "packages/agent/uv.lock"), sources)
  expect(issues.some((issue) => issue.includes("公网包源") || issue.includes("不一致"))).toBe(true)
})

test("validates internal uv provenance and permits Bun locks without embedded URLs", () => {
  const sources = resolveInternalSources(INTERNAL_ENV)
  const temp = mkdtempSync(resolve(tmpdir(), "harness-source-policy-"))
  writeFileSync(`${temp}/uv.lock`, [
    '[[package]]',
    'name = "deepagents"',
    'version = "0.7.3"',
    'source = { registry = "https://pypi.intranet.example/simple/" }',
    'wheels = [{ url = "https://pypi.intranet.example/packages/deepagents.whl" }]',
    '',
  ].join("\n"))
  writeFileSync(`${temp}/bun.lock`, '{"packages": {"safe": ["safe@1.0.0", "", {}, "sha512-x"],},}')

  expect(validateUvLockSource(`${temp}/uv.lock`, sources)).toEqual([])
  expect(validateBunLockSource(`${temp}/bun.lock`, sources)).toEqual([])
})

test("rejects a public URL embedded in a Bun lock", () => {
  const sources = resolveInternalSources(INTERNAL_ENV)
  const temp = `${mkdtempSync(resolve(tmpdir(), "harness-source-policy-public-"))}/bun.lock`
  writeFileSync(temp, '{"packages": {"bad": ["bad@1.0.0", "https://registry.npmjs.org/bad.tgz", {}, "sha512-x"],},}')
  expect(validateBunLockSource(temp, sources)).toContain(`Bun 锁文件 ${temp} 仍指向公网包源 registry.npmjs.org`)
})

test("checks the repository toolchain contract", () => {
  expect(expectedBunVersion(ROOT)).toBe("1.2.19")
  expect(validateToolchainVersions({ bun: "1.2.19", uv: "0.11.30", python: "3.12.9", expectedBun: "1.2.19" })).toEqual([])
  expect(validateToolchainVersions({ bun: "1.3.14", uv: "0.10.9", python: "3.10.9", expectedBun: "1.2.19" })).toHaveLength(3)
})
