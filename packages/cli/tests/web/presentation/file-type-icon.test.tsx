/** 文件类型图标：扩展名/特殊文件名 → 类型 class 映射；FileTree 行携带类型 class；选中行保留类型色。 */
/** @jsxImportSource react */

import { afterAll, describe, expect, test } from "bun:test"

import { fileIconFor } from "../../../src/web/presentation/workspace-sidebar/file-type-icon"
import { FileTree } from "../../../src/web/presentation/workspace-sidebar/file-tree"
import type { WebIntent } from "../../../src/web/application/adapter"
import type { WorkspaceTreeRow, WorkspaceTreeState } from "../../../src/workspace/types"
import { makeSnapshot } from "./fixtures"
import { registerTestDom, render, type RenderHandle } from "./render"

const unregisterTestDom = registerTestDom()
afterAll(() => unregisterTestDom())

describe("fileIconFor 类型映射", () => {
  test("TS/JS/其他代码/样式/文档/配置/图片各归其类", () => {
    expect(fileIconFor("app.tsx").type).toBe("ts")
    expect(fileIconFor("index.ts").type).toBe("ts")
    expect(fileIconFor("a.js").type).toBe("js")
    expect(fileIconFor("b.jsx").type).toBe("js")
    expect(fileIconFor("main.py").type).toBe("code")
    expect(fileIconFor("run.sh").type).toBe("code")
    expect(fileIconFor("app.css").type).toBe("style")
    expect(fileIconFor("page.html").type).toBe("style")
    expect(fileIconFor("README.md").type).toBe("doc")
    expect(fileIconFor("package.json").type).toBe("config")
    expect(fileIconFor("config.yaml").type).toBe("config")
    expect(fileIconFor("logo.png").type).toBe("image")
    expect(fileIconFor("icon.svg").type).toBe("image")
  })

  test("特殊文件名优先于扩展名；未知扩展名回落 default；大小写不敏感", () => {
    expect(fileIconFor("Dockerfile").type).toBe("config")
    expect(fileIconFor("Makefile").type).toBe("config")
    expect(fileIconFor(".gitignore").type).toBe("config")
    expect(fileIconFor("LICENSE").type).toBe("doc")
    expect(fileIconFor("archive.xyz").type).toBe("default")
    expect(fileIconFor("App.TSX").type).toBe("ts")
  })
})

describe("FileTree 类型图标渲染", () => {
  function row(overrides: Partial<WorkspaceTreeRow>): WorkspaceTreeRow {
    return {
      path: "src",
      name: "src",
      kind: "directory",
      depth: 0,
      expanded: true,
      loading: false,
      hasChildren: true,
      ...overrides,
    }
  }

  function mountTree(rows: readonly WorkspaceTreeRow[], selectedPath: string | null = null): RenderHandle {
    const tree: WorkspaceTreeState = { status: "ready", rows, selectedPath, limited: false }
    const intents: WebIntent[] = []
    return render(
      <FileTree snapshot={makeSnapshot({ workspaceTree: tree, workspaceSidebar: { threadRatio: 0.38, selectedPath, widthPx: 280 } })} dispatch={intent => intents.push(intent)} />,
    )
  }

  test("文件行携带 file-row-icon-<type> class；目录行不带类型 class", () => {
    const handle = mountTree([
      row({}),
      row({ path: "src/a.tsx", name: "a.tsx", kind: "file", depth: 1, hasChildren: false }),
      row({ path: "src/readme.md", name: "readme.md", kind: "file", depth: 1, hasChildren: false }),
    ])
    try {
      const rows = handle.container.querySelectorAll<HTMLElement>(".file-row")
      expect(rows[0]!.querySelector("[class*='file-row-icon-']")).toBeNull()
      expect(rows[1]!.querySelector(".file-row-icon-ts")).not.toBeNull()
      expect(rows[2]!.querySelector(".file-row-icon-doc")).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })

  test("选中行保留类型 class（类型色不被选中态覆盖）", () => {
    const handle = mountTree(
      [row({ path: "a.ts", name: "a.ts", kind: "file", hasChildren: false })],
      "a.ts",
    )
    try {
      const icon = handle.container.querySelector(".file-row.is-selected .file-row-icon-ts")
      expect(icon).not.toBeNull()
    } finally {
      handle.unmount()
    }
  })
})
