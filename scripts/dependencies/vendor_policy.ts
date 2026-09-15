/** 校验临时分支源码化 npm 发布工件、workspace 链路与 Windows x64 目标。 */

import { createHash } from "node:crypto"
import { existsSync, lstatSync, readFileSync, readlinkSync, readdirSync, statSync } from "node:fs"
import { join, relative, resolve } from "node:path"

export interface VendoredPackageSpec {
  name: string
  version: string
  path: string
  tarball: string
  integrity: string
}

export const VENDORED_PACKAGE_SPECS: readonly VendoredPackageSpec[] = [
  {
    name: "@opentui/core",
    version: "0.4.3",
    path: "third_party/npm/@opentui/core",
    tarball: "https://registry.npmjs.org/@opentui/core/-/core-0.4.3.tgz",
    integrity: "sha512-rrJfAk13tALDqldYjhc78eWQ+aKq1iknJgffIOg3OwyZoqQo+p6gtuqyhmWvXIfQzlNUbpgpCPcxbXlhMnlaHQ==",
  },
  {
    name: "@opentui/react",
    version: "0.4.3",
    path: "third_party/npm/@opentui/react",
    tarball: "https://registry.npmjs.org/@opentui/react/-/react-0.4.3.tgz",
    integrity: "sha512-HtC/+lMURaZlifiJNVsn84YqKMywXQmkeSNbUTlbbeS6YM6reahaacnZEYZdnEUgklO6+r/J1tG8UBhqO26X7Q==",
  },
  {
    name: "@opentui/core-win32-x64",
    version: "0.4.3",
    path: "third_party/npm/@opentui/core-win32-x64",
    tarball: "https://registry.npmjs.org/@opentui/core-win32-x64/-/core-win32-x64-0.4.3.tgz",
    integrity: "sha512-NuoqvWKGXaYnmlqvu7Gg2lLI6yVMnS9OfWBvxp+7Q+McSgHFSTQmYBXaPpvQ8HikpQXE1nCeMPtuSG4PdZHe2w==",
  },
  {
    name: "bun-ffi-structs",
    version: "0.2.4",
    path: "third_party/npm/bun-ffi-structs",
    tarball: "https://registry.npmjs.org/bun-ffi-structs/-/bun-ffi-structs-0.2.4.tgz",
    integrity: "sha512-AJzsqoVFs1KBbJbWHIYrVZLDC3NhTqqh25awRXqzoLzmBAKr5oqk6+CwuYHAekKx+VBCYVohBoKuRq40dV+TYg==",
  },
  {
    name: "react-devtools-core",
    version: "7.0.1",
    path: "third_party/npm/react-devtools-core",
    tarball: "https://registry.npmjs.org/react-devtools-core/-/react-devtools-core-7.0.1.tgz",
    integrity: "sha512-C3yNvRHaizlpiASzy7b9vbnBGLrhvdhl1CbdU6EnZgxPNbai60szdLtl+VL76UNOt5bOoVTOz5rNWZxgGt+Gsw==",
  },
]

const TARGET_PLATFORM = { os: "win32", cpu: "x64" } as const
const PATCH_SOURCE = "patches/react-devtools-core@7.0.1.patch"

type JsonRecord = Record<string, unknown>

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function readJson(path: string): unknown {
  try {
    return JSON.parse(readFileSync(path, "utf-8")) as unknown
  } catch {
    return undefined
  }
}

/** 计算 provenance 使用的无平台差异目录摘要。 */
export function directorySha256(directory: string): string {
  const entries: string[] = []

  function visit(current: string): void {
    for (const name of readdirSync(current).sort()) {
      // Bun creates workspace links below each local package. They are install
      // output, not part of the npm tarball and must not change its digest.
      if (name === "node_modules" || name === ".bun") continue
      const absolute = join(current, name)
      const relativePath = relative(directory, absolute).split("\\").join("/")
      const stats = lstatSync(absolute)
      if (stats.isSymbolicLink()) {
        entries.push(`${relativePath}\tsymlink:${readlinkSync(absolute)}\t0\n`)
      } else if (stats.isDirectory()) {
        visit(absolute)
      } else if (stats.isFile()) {
        const payload = readFileSync(absolute)
        entries.push(`${relativePath}\t${sha256Hex(payload)}\t${payload.byteLength}\n`)
      }
    }
  }

  visit(directory)
  return sha256Hex(entries.join(""))
}

export function sha256Hex(payload: Uint8Array | string): string {
  return createHash("sha256").update(payload).digest("hex")
}

function packageRoot(root: string, spec: VendoredPackageSpec): string {
  return resolve(root, spec.path)
}

function lockJson(path: string): JsonRecord | undefined {
  try {
    // Bun lockfile v1 is JSON with trailing commas; no package data is parsed
    // from strings here, so removing only closing-token commas is sufficient.
    const source = readFileSync(path, "utf-8").replace(/,\s*([}\]])/g, "$1")
    const parsed = JSON.parse(source) as unknown
    return isRecord(parsed) ? parsed : undefined
  } catch {
    return undefined
  }
}

function packageManifest(root: string, spec: VendoredPackageSpec): JsonRecord | undefined {
  const parsed = readJson(join(packageRoot(root, spec), "package.json"))
  return isRecord(parsed) ? parsed : undefined
}

function stringValue(record: JsonRecord | undefined, key: string): string | undefined {
  const value = record?.[key]
  return typeof value === "string" ? value : undefined
}

function objectValue(record: JsonRecord | undefined, key: string): JsonRecord | undefined {
  const value = record?.[key]
  return isRecord(value) ? value : undefined
}

function hasWorkspaceDependency(record: JsonRecord | undefined, key: string): boolean {
  return stringValue(record, key) === "workspace:*"
}

function validatePackageFiles(root: string, spec: VendoredPackageSpec): string[] {
  const issues: string[] = []
  const directory = packageRoot(root, spec)
  const manifestPath = join(directory, "package.json")
  const manifest = packageManifest(root, spec)
  if (!existsSync(directory)) {
    issues.push(`${spec.name}: missing vendored directory ${spec.path}`)
    return issues
  }
  if (!manifest) {
    issues.push(`${spec.name}: invalid or missing package.json`)
    return issues
  }
  if (stringValue(manifest, "name") !== spec.name) {
    issues.push(`${spec.name}: package name mismatch`)
  }
  if (stringValue(manifest, "version") !== spec.version) {
    issues.push(`${spec.name}: version mismatch; expected ${spec.version}`)
  }
  if (stringValue(manifest, "license") !== "MIT") {
    issues.push(`${spec.name}: license must be MIT`)
  }
  if (spec.name === "@opentui/core-win32-x64") {
    const os = manifest.os
    const cpu = manifest.cpu
    if (!Array.isArray(os) || !os.includes(TARGET_PLATFORM.os) || !Array.isArray(cpu) || !cpu.includes(TARGET_PLATFORM.cpu)) {
      issues.push(`${spec.name}: must target win32/x64`)
    }
    const dll = join(directory, "opentui.dll")
    if (!existsSync(dll) || !statSync(dll).isFile() || statSync(dll).size === 0) {
      issues.push(`${spec.name}: missing opentui.dll`)
    }
  }
  return issues
}

function validateProvenance(root: string): string[] {
  const issues: string[] = []
  const provenancePath = resolve(root, "third_party/npm/provenance.json")
  const provenance = readJson(provenancePath)
  if (!isRecord(provenance)) return [`missing or invalid provenance ${provenancePath}`]
  if (provenance.schemaVersion !== 1) issues.push("provenance schemaVersion must be 1")
  const target = objectValue(provenance, "target")
  if (target?.os !== TARGET_PLATFORM.os || target?.cpu !== TARGET_PLATFORM.cpu) {
    issues.push("provenance target must be win32/x64")
  }
  const packages = objectValue(provenance, "packages")
  if (!packages) return [...issues, "provenance packages must be an object"]

  for (const spec of VENDORED_PACKAGE_SPECS) {
    const entry = objectValue(packages, spec.name)
    if (!entry) {
      issues.push(`${spec.name}: missing provenance entry`)
      continue
    }
    if (entry.path !== spec.path || entry.name !== spec.name || entry.version !== spec.version) {
      issues.push(`${spec.name}: provenance identity mismatch`)
    }
    if (entry.integrity !== spec.integrity) {
      issues.push(`${spec.name}: provenance integrity mismatch`)
    }
    if (entry.tarball !== spec.tarball) {
      issues.push(`${spec.name}: provenance tarball mismatch`)
    }
    if (entry.license !== "MIT") issues.push(`${spec.name}: provenance license mismatch`)
    const expectedDirectoryHash = stringValue(entry, "directorySha256")
    const directory = packageRoot(root, spec)
    if (!expectedDirectoryHash || !existsSync(directory) || directorySha256(directory) !== expectedDirectoryHash) {
      issues.push(`${spec.name}: directory hash mismatch`)
    }
  }

  const patches = objectValue(provenance, "patches")
  const patch = objectValue(patches, "react-devtools-core")
  const patchPath = resolve(root, PATCH_SOURCE)
  if (!patch || patch.source !== PATCH_SOURCE || !existsSync(patchPath) || patch.sourceSha256 !== sha256Hex(readFileSync(patchPath))) {
    issues.push("react-devtools-core: patch source provenance mismatch")
  }
  return issues
}

function validateWorkspaceEdges(root: string): string[] {
  const issues: string[] = []
  const rootManifest = readJson(resolve(root, "package.json"))
  const rootRecord = isRecord(rootManifest) ? rootManifest : undefined
  const workspaces = rootRecord?.workspaces
  if (!Array.isArray(workspaces) || !workspaces.includes("third_party/npm/*") || !workspaces.includes("third_party/npm/@opentui/*")) {
    issues.push("root workspaces must include third_party/npm/* and third_party/npm/@opentui/*")
  }
  const patchedDependencies = objectValue(rootRecord, "patchedDependencies")
  if (patchedDependencies && Object.keys(patchedDependencies).some((key) => key.startsWith("react-devtools-core@"))) {
    issues.push("duplicate root patchedDependencies for react-devtools-core")
  }

  const cli = readJson(resolve(root, "packages/cli/package.json"))
  const cliRecord = isRecord(cli) ? cli : undefined
  if (!hasWorkspaceDependency(objectValue(cliRecord, "dependencies"), "@opentui/core")) {
    issues.push("CLI @opentui/core must use workspace:*")
  }
  if (!hasWorkspaceDependency(objectValue(cliRecord, "dependencies"), "@opentui/react")) {
    issues.push("CLI @opentui/react must use workspace:*")
  }
  if (!hasWorkspaceDependency(objectValue(cliRecord, "devDependencies"), "react-devtools-core")) {
    issues.push("CLI react-devtools-core must use workspace:*")
  }

  const core = packageManifest(root, VENDORED_PACKAGE_SPECS[0])
  const react = packageManifest(root, VENDORED_PACKAGE_SPECS[1])
  const devtools = packageManifest(root, VENDORED_PACKAGE_SPECS[4])
  if (!hasWorkspaceDependency(objectValue(core, "dependencies"), "bun-ffi-structs")) {
    issues.push("@opentui/core -> bun-ffi-structs must use workspace:*")
  }
  if (!hasWorkspaceDependency(objectValue(core, "optionalDependencies"), "@opentui/core-win32-x64")) {
    issues.push("@opentui/core -> @opentui/core-win32-x64 must use workspace:*")
  }
  if (!hasWorkspaceDependency(objectValue(react, "dependencies"), "@opentui/core")) {
    issues.push("@opentui/react -> @opentui/core must use workspace:*")
  }
  if (stringValue(objectValue(devtools, "dependencies"), "ws") !== "7.5.10") {
    issues.push("react-devtools-core ws dependency must be exactly 7.5.10")
  }
  return issues
}

function validateLock(root: string): string[] {
  const path = resolve(root, "bun.lock")
  const parsed = lockJson(path)
  if (!parsed) return [`invalid or missing Bun lockfile ${path}`]
  const packages = objectValue(parsed, "packages")
  if (!packages) return ["Bun lockfile packages must be an object"]
  const issues: string[] = []
  for (const spec of VENDORED_PACKAGE_SPECS) {
    const entry = packages[spec.name]
    const first = Array.isArray(entry) && typeof entry[0] === "string" ? entry[0] : ""
    if (!first.startsWith(`${spec.name}@workspace:`)) {
      issues.push(`${spec.name}: registry fallback in Bun lockfile`)
    }
    if (JSON.stringify(entry ?? "").includes("registry.npmjs.org") || JSON.stringify(entry ?? "").includes("registry.")) {
      issues.push(`${spec.name}: registry fallback metadata in Bun lockfile`)
    }
  }
  return issues
}

/** 汇总安装前和安装后共用的源码化 npm 门禁。 */
export function validateVendoredWorkspace(root: string): string[] {
  const issues = VENDORED_PACKAGE_SPECS.flatMap((spec) => validatePackageFiles(root, spec))
  return [...issues, ...validateProvenance(root), ...validateWorkspaceEdges(root), ...validateLock(root)]
}
