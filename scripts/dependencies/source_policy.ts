/** 内网依赖安装的源、锁文件和工具链前置校验。 */

import { readFileSync } from "node:fs"
import { resolve } from "node:path"

export const MIN_UV_VERSION = "0.11.1"
export const PYTHON_MIN_VERSION = "3.11.0"
export const PYTHON_MAX_VERSION = "4.0.0"

const PUBLIC_PACKAGE_HOSTS = [
  "files.pythonhosted.org",
  "github.com",
  "jsdelivr.net",
  "npmjs.org",
  "pypi.org",
  "pythonhosted.org",
  "raw.githubusercontent.com",
  "registry.npmjs.org",
  "registry.yarnpkg.com",
  "unpkg.com",
]

export type Environment = Record<string, string | undefined>

export interface InternalSources {
  npmRegistry: URL
  pythonIndex: URL
  pythonArtifactHosts: Set<string>
}

export interface ToolchainVersions {
  bun: string
  uv: string
  python: string
  expectedBun?: string
}

export class DependencyPreflightError extends Error {
  readonly issues: readonly string[]

  constructor(issues: readonly string[]) {
    super(["内网依赖前置检查失败：", ...issues.map((issue) => `- ${issue}`)].join("\n"))
    this.name = "DependencyPreflightError"
    this.issues = issues
  }
}

function nonEmpty(...values: Array<string | undefined>): string | undefined {
  return values.find((value) => value?.trim())?.trim()
}

function isPublicHost(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "")
  return PUBLIC_PACKAGE_HOSTS.some((publicHost) => normalized === publicHost || normalized.endsWith(`.${publicHost}`))
}

function parseInternalUrl(raw: string | undefined, label: string): URL | string {
  if (!raw) return `未配置 ${label}；请注入内网 registry/index 环境变量`
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return `${label} 不是合法 URL`
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return `${label} 必须使用 http 或 https`
  }
  if (!url.hostname || url.username || url.password || url.search || url.hash) {
    return `${label} 不得包含凭据/query/hash，且必须包含主机名`
  }
  if (isPublicHost(url.hostname)) {
    return `${label} 指向公网包源 ${url.hostname}；必须使用内网源`
  }
  return url
}

function hostFromValue(value: string): string | undefined {
  const candidate = value.includes("://") ? value : `https://${value}`
  try {
    const url = new URL(candidate)
    return url.hostname.toLowerCase().replace(/\.$/, "") || undefined
  } catch {
    return undefined
  }
}

function collectUrls(text: string): string[] {
  return (text.match(/https?:\/\/[^\s"'`<>]+/g) ?? []).map((value) => value.replace(/[),\]}]+$/, ""))
}

function parseVersion(value: string): [number, number, number] | undefined {
  const match = value.trim().match(/^(\d+)\.(\d+)(?:\.(\d+))?/)
  if (!match) return undefined
  return [Number(match[1]), Number(match[2]), Number(match[3] ?? 0)]
}

function compareVersions(left: string, right: string): number | undefined {
  const leftParts = parseVersion(left)
  const rightParts = parseVersion(right)
  if (!leftParts || !rightParts) return undefined
  for (let index = 0; index < leftParts.length; index += 1) {
    if (leftParts[index] !== rightParts[index]) return leftParts[index] > rightParts[index] ? 1 : -1
  }
  return 0
}

export function expectedBunVersion(root: string): string {
  const manifest = JSON.parse(readFileSync(resolve(root, "package.json"), "utf-8")) as { packageManager?: unknown }
  if (typeof manifest.packageManager !== "string" || !manifest.packageManager.startsWith("bun@")) {
    throw new DependencyPreflightError(["package.json 缺少 bun@<version> 的 packageManager 约束"])
  }
  return manifest.packageManager.slice("bun@".length)
}

export function resolveInternalSources(environment: Environment = process.env): InternalSources {
  const issues: string[] = []
  const npmValue = parseInternalUrl(
    nonEmpty(environment.HARNESS_NPM_REGISTRY, environment.npm_config_registry, environment.NPM_CONFIG_REGISTRY),
    "HARNESS_NPM_REGISTRY",
  )
  const pythonValue = parseInternalUrl(
    nonEmpty(environment.HARNESS_PYPI_INDEX, environment.UV_DEFAULT_INDEX, environment.UV_INDEX_URL),
    "HARNESS_PYPI_INDEX",
  )
  if (typeof npmValue === "string") issues.push(npmValue)
  if (typeof pythonValue === "string") issues.push(pythonValue)
  if (typeof npmValue === "string" || typeof pythonValue === "string") throw new DependencyPreflightError(issues)

  const artifactHosts = new Set<string>([pythonValue.hostname.toLowerCase().replace(/\.$/, "")])
  const configuredArtifactHosts = nonEmpty(environment.HARNESS_PYPI_ARTIFACT_HOSTS)
  if (configuredArtifactHosts) {
    for (const rawHost of configuredArtifactHosts.split(",")) {
      const hostname = hostFromValue(rawHost.trim())
      if (!hostname) {
        issues.push(`HARNESS_PYPI_ARTIFACT_HOSTS 含无效主机 ${rawHost.trim()}`)
      } else if (isPublicHost(hostname)) {
        issues.push(`HARNESS_PYPI_ARTIFACT_HOSTS 指向公网主机 ${hostname}`)
      } else {
        artifactHosts.add(hostname)
      }
    }
  }
  for (const key of ["UV_INDEX", "UV_EXTRA_INDEX_URL", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL"]) {
    const configured = nonEmpty(environment[key])
    if (!configured) continue
    const urls = collectUrls(configured)
    if (urls.length === 0) {
      issues.push(`${key} 未包含可验证的 http(s) URL`)
      continue
    }
    for (const url of urls) {
      const issue = validateUrlHost(url, `${key} 配置`, artifactHosts)
      if (issue) issues.push(issue)
    }
  }
  if (issues.length > 0) throw new DependencyPreflightError(issues)
  return { npmRegistry: npmValue, pythonIndex: pythonValue, pythonArtifactHosts: artifactHosts }
}

function validateUrlHost(raw: string, label: string, allowedHosts: Set<string>): string | undefined {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return `${label} 含无效 URL ${raw}`
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return `${label} 必须使用 http 或 https`
  }
  if (url.username || url.password || url.search || url.hash) {
    return `${label} 不得包含凭据/query/hash`
  }
  const hostname = url.hostname.toLowerCase().replace(/\.$/, "")
  if (isPublicHost(hostname)) return `${label} 仍指向公网包源 ${hostname}`
  if (!allowedHosts.has(hostname)) return `${label} 主机 ${hostname} 与当前内网源不一致`
  return undefined
}

export function validateBunLockSource(lockPath: string, sources: InternalSources): string[] {
  let text: string
  try {
    text = readFileSync(lockPath, "utf-8")
  } catch {
    return [`找不到 Bun 锁文件 ${lockPath}`]
  }
  const allowedHosts = new Set([sources.npmRegistry.hostname.toLowerCase().replace(/\.$/, "")])
  return [...new Set(
    collectUrls(text)
      .map((url) => validateUrlHost(url, `Bun 锁文件 ${lockPath}`, allowedHosts))
      .filter((issue): issue is string => Boolean(issue)),
  )]
}

export function validateUvLockSource(lockPath: string, sources: InternalSources): string[] {
  let text: string
  try {
    text = readFileSync(lockPath, "utf-8")
  } catch {
    return [`找不到 uv 锁文件 ${lockPath}`]
  }
  const issues: string[] = []
  const allowedHosts = sources.pythonArtifactHosts
  const registryUrls = [...text.matchAll(/source\s*=\s*\{\s*registry\s*=\s*"([^"]+)"/g)].map((match) => match[1])
  if (registryUrls.length === 0) issues.push(`uv 锁文件 ${lockPath} 没有可验证的 registry 来源`)
  for (const url of registryUrls) {
    const issue = validateUrlHost(url, `uv 锁文件 registry ${lockPath}`, allowedHosts)
    if (issue) issues.push(issue)
  }
  const artifactUrls = collectUrls(text)
  if (artifactUrls.length === 0) issues.push(`uv 锁文件 ${lockPath} 没有可验证的制品地址`)
  for (const url of artifactUrls) {
    const issue = validateUrlHost(url, `uv 锁文件制品 ${lockPath}`, allowedHosts)
    if (issue) issues.push(issue)
  }
  return [...new Set(issues)]
}

export function validateLockSources(root: string, sources: InternalSources): string[] {
  return [
    ...validateBunLockSource(resolve(root, "bun.lock"), sources),
    ...validateUvLockSource(resolve(root, "packages/agent/uv.lock"), sources),
  ]
}

export function validateToolchainVersions(versions: ToolchainVersions): string[] {
  const issues: string[] = []
  const expectedBun = versions.expectedBun
  if (expectedBun && versions.bun !== expectedBun) {
    issues.push(`Bun 版本为 ${versions.bun}，仓库要求 ${expectedBun}`)
  }
  if (!parseVersion(versions.uv)) {
    issues.push(`无法解析 uv 版本 ${versions.uv}`)
  } else if ((compareVersions(versions.uv, MIN_UV_VERSION) ?? -1) < 0) {
    issues.push(`uv 版本为 ${versions.uv}，至少需要 ${MIN_UV_VERSION}`)
  }
  const python = parseVersion(versions.python)
  if (!python) {
    issues.push(`无法解析 Python 版本 ${versions.python}`)
  } else {
    const pythonText = python.join(".")
    if ((compareVersions(pythonText, PYTHON_MIN_VERSION) ?? -1) < 0 || (compareVersions(pythonText, PYTHON_MAX_VERSION) ?? 1) >= 0) {
      issues.push(`Python 版本为 ${versions.python}，要求 >=${PYTHON_MIN_VERSION}, <${PYTHON_MAX_VERSION}`)
    }
  }
  return issues
}
