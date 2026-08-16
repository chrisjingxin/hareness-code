/** TUI 文件图标与专色映射单元测试。 */

import { expect, test } from "bun:test"
import { getFileIconInfo } from "../../../src/tui/presentation/sidebar/file-icons"

test("getFileIconInfo: 目录展开与收起", () => {
  const collapsed = getFileIconInfo("src", "directory", false)
  expect(collapsed.icon).toContain("📁")
  expect(collapsed.color).toBe("#e6bb72")

  const expanded = getFileIconInfo("src", "directory", true)
  expect(expanded.icon).toContain("📂")
  expect(expanded.color).toBe("#e6bb72")
})

test("getFileIconInfo: 编程语言扩展名精准匹配", () => {
  // Python
  const python = getFileIconInfo("main.py", "file")
  expect(python.icon).toContain("🐍")
  expect(python.color).toBe("#3572a5")

  // TypeScript
  const ts = getFileIconInfo("app.ts", "file")
  expect(ts.icon).toContain("📄")
  expect(ts.color).toBe("#3178c6")

  // TypeScript React
  const tsx = getFileIconInfo("button.tsx", "file")
  expect(tsx.icon).toContain("📄")
  expect(tsx.color).toBe("#3178c6")

  // JavaScript
  const js = getFileIconInfo("index.js", "file")
  expect(js.icon).toContain("📄")
  expect(js.color).toBe("#f7df1e")

  // Rust
  const rust = getFileIconInfo("lib.rs", "file")
  expect(rust.icon).toContain("🦀")
  expect(rust.color).toBe("#dea584")

  // Go
  const go = getFileIconInfo("server.go", "file")
  expect(go.icon).toContain("🐹")
  expect(go.color).toBe("#00add8")

  // Shell
  const bash = getFileIconInfo("build.sh", "file")
  expect(bash.icon).toContain("💻")
})

test("getFileIconInfo: 特殊文件名优先匹配", () => {
  const pkg = getFileIconInfo("package.json", "file")
  expect(pkg.icon).toContain("📦")

  const docker = getFileIconInfo("Dockerfile", "file")
  expect(docker.icon).toContain("🐳")

  const gitignore = getFileIconInfo(".gitignore", "file")
  expect(gitignore.icon).toContain("🐙")

  const env = getFileIconInfo(".env", "file")
  expect(env.icon).toContain("🔑")

  const readme = getFileIconInfo("README.md", "file")
  expect(readme.icon).toContain("📝")
})

test("getFileIconInfo: 符号链接与默认未知文件", () => {
  const symlink = getFileIconInfo("link-target", "symlink")
  expect(symlink.icon).toContain("↪")

  const unknown = getFileIconInfo("unknown.xyz123", "file")
  expect(unknown.icon).toContain("📄")
})
