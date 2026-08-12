/**
 * 一次性迁移：tasks→task、designs→spec、ZC→HC、文件名补功能简介。
 *
 * 仓库已完成迁移；再次运行仅在旧目录仍存在时生效。
 * 运行：bun run scripts/project/migrate-docs-layout.ts
 */

import { mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises"
import { basename, dirname, extname, join, relative, resolve } from "node:path"

import { TASK_ARCHIVE_DIR, TASK_DIR, parseTaskDocument, taskBriefFromTitle, taskFileName } from "./tasks"

const root = resolve(import.meta.dir, "../..")

type PathMap = Map<string, string>

async function main(): Promise<void> {
  const pathMap: PathMap = new Map()

  // 已存在的新规范文档先登记，避免二次迁移覆盖。
  await registerExisting(join(root, "docs/developer/task"), pathMap)
  await registerExisting(join(root, "docs/developer/spec"), pathMap)
  await registerExisting(join(root, "docs/developer/plan"), pathMap)
  await registerExisting(join(root, "docs/developer/todo"), pathMap)

  await migrateTaskTree(join(root, "docs/developer/tasks"), join(root, TASK_DIR), pathMap, true)
  await migrateDesignTree(join(root, "docs/developer/designs"), join(root, "docs/developer/spec"), pathMap)
  await migrateSibling(join(root, "docs/developer/plan"), pathMap)
  await migrateSibling(join(root, "docs/developer/todo"), pathMap)

  // 活动目录中已完成任务移入 archive。
  await archiveCompletedActiveTasks(pathMap)

  // 全仓文本替换路径与编号引用。
  await rewriteRepository(pathMap)

  // 清理旧目录（若已空或仅剩可删文件）。
  await removeIfExists(join(root, "docs/developer/tasks"))
  await removeIfExists(join(root, "docs/developer/designs"))

  console.log(`迁移完成：登记 ${pathMap.size} 条路径映射`)
  for (const [from, to] of [...pathMap.entries()].sort()) {
    if (from !== to) console.log(`  ${from} → ${to}`)
  }
}

async function registerExisting(directory: string, pathMap: PathMap): Promise<void> {
  for (const file of await listFiles(directory)) {
    if (!file.endsWith(".md")) continue
    const rel = relative(root, file)
    pathMap.set(rel, rel)
    // 也登记纯编号形式，便于后续链接替换。
    const name = basename(file, ".md")
    const idMatch = name.match(/^(HC-\d{3,}(?:-legacy)?)/)
    if (idMatch) {
      pathMap.set(`${dirname(rel)}/${idMatch[1]}.md`.replace(/\\/g, "/"), rel.replace(/\\/g, "/"))
    }
  }
}

async function migrateTaskTree(sourceDir: string, targetDir: string, pathMap: PathMap, isRoot: boolean): Promise<void> {
  let entries
  try {
    entries = await readdir(sourceDir, { withFileTypes: true })
  } catch {
    return
  }

  await mkdir(targetDir, { recursive: true })

  for (const entry of entries) {
    const sourcePath = join(sourceDir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === "archive") {
        await migrateTaskTree(sourcePath, join(root, TASK_ARCHIVE_DIR), pathMap, false)
      } else {
        await migrateTaskTree(sourcePath, join(targetDir, entry.name), pathMap, false)
      }
      continue
    }
    if (!entry.isFile()) continue

    const sourceRel = relative(root, sourcePath).replace(/\\/g, "/")

    if (entry.name === "任务看板.md") {
      // 看板将由 tasks:sync 重建
      continue
    }

    if (entry.name === "README.md") {
      const targetPath = join(targetDir, "README.md")
      const targetRel = relative(root, targetPath).replace(/\\/g, "/")
      if (!(await exists(targetPath))) {
        await writeFile(targetPath, await readFile(sourcePath, "utf8"), "utf8")
      }
      pathMap.set(sourceRel, targetRel)
      continue
    }

    if (!entry.name.endsWith(".md")) {
      const targetPath = join(targetDir, entry.name)
      await mkdir(dirname(targetPath), { recursive: true })
      if (!(await exists(targetPath))) await rename(sourcePath, targetPath)
      pathMap.set(sourceRel, relative(root, targetPath).replace(/\\/g, "/"))
      continue
    }

    // ZC-xxx.md 或 ZC-xxx-legacy.md
    const zcMatch = entry.name.match(/^(ZC-\d{3,}(?:-legacy)?)(?:-(.+))?\.md$/)
    if (!zcMatch) {
      // 非任务命名文件：原样迁入
      const targetPath = join(targetDir, entry.name)
      if (!(await exists(targetPath))) {
        await mkdir(dirname(targetPath), { recursive: true })
        await rename(sourcePath, targetPath)
      }
      pathMap.set(sourceRel, relative(root, targetPath).replace(/\\/g, "/"))
      continue
    }

    const oldId = zcMatch[1]
    const newId = oldId.replace(/^ZC-/, "HC-")
    let content = await readFile(sourcePath, "utf8")
    content = rewriteTaskDocumentContent(content, newId)

    let title = "未命名任务"
    try {
      title = parseTaskDocument(content, sourceRel).metadata.title
    } catch {
      const titleMatch = content.match(/^title:\s*(.+)$/m)
      if (titleMatch) title = titleMatch[1].trim()
    }

    const fileName = taskFileName(newId, title)
    const targetPath = join(targetDir, fileName)
    const targetRel = relative(root, targetPath).replace(/\\/g, "/")

    if (await exists(targetPath)) {
      pathMap.set(sourceRel, targetRel)
      pathMap.set(`docs/developer/tasks/${oldId}.md`, targetRel)
      pathMap.set(`${TASK_DIR}/${newId}.md`, targetRel)
      continue
    }

    await mkdir(dirname(targetPath), { recursive: true })
    await writeFile(targetPath, content, "utf8")
    pathMap.set(sourceRel, targetRel)
    pathMap.set(`docs/developer/tasks/${oldId}.md`, targetRel)
    pathMap.set(`docs/developer/task/${oldId}.md`, targetRel)
    pathMap.set(`${TASK_DIR}/${newId}.md`, targetRel)
  }
}

async function migrateDesignTree(sourceDir: string, targetDir: string, pathMap: PathMap): Promise<void> {
  let entries
  try {
    entries = await readdir(sourceDir, { withFileTypes: true })
  } catch {
    return
  }
  await mkdir(targetDir, { recursive: true })

  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".md")) continue
    const sourcePath = join(sourceDir, entry.name)
    const sourceRel = relative(root, sourcePath).replace(/\\/g, "/")
    let content = await readFile(sourcePath, "utf8")
    content = content
      .replaceAll("docs/developer/tasks/", "docs/developer/task/")
      .replaceAll("docs/developer/designs/", "docs/developer/spec/")
      .replace(/\bZC-(\d{3,}(?:-legacy)?)\b/g, "HC-$1")

    let brief = "规格说明"
    const remediation = entry.name.match(/^ZC-(\d+)-ZC-(\d+)-(.+)\.md$/)
    const plain = entry.name.match(/^(ZC-\d{3,})(?:-(.+))?\.md$/)

    let targetName: string
    if (remediation) {
      const idPart = `HC-${remediation[1]}-${remediation[2]}`
      brief = taskBriefFromTitle(remediation[3].replace(/\.md$/, "") || "联合补救")
      targetName = `${idPart}-${brief}.md`
    } else if (plain) {
      const newId = plain[1].replace(/^ZC-/, "HC-")
      // 优先用匹配任务的 title 简介；否则用文件名后缀或 H1
      const h1 = content.match(/^#\s+(.+)$/m)?.[1]?.trim()
      const fromName = plain[2]
      brief = taskBriefFromTitle(fromName || h1 || newId)
      // 若能在 pathMap 找到对应 task 文件，用其 brief
      const taskHint = [...pathMap.values()].find(v => basename(v).startsWith(`${newId}-`))
      if (taskHint) {
        const taskBrief = basename(taskHint).slice(newId.length + 1).replace(/\.md$/, "")
        if (taskBrief) brief = taskBrief
      }
      targetName = `${newId}-${brief}.md`
    } else {
      targetName = entry.name.replace(/^ZC-/, "HC-")
    }

    const targetPath = join(targetDir, targetName)
    const targetRel = relative(root, targetPath).replace(/\\/g, "/")
    if (!(await exists(targetPath))) {
      await writeFile(targetPath, content, "utf8")
    }
    pathMap.set(sourceRel, targetRel)
    pathMap.set(`docs/developer/designs/${entry.name}`, targetRel)
    const idOnly = entry.name.replace(/\.md$/, "").replace(/^ZC-/, "HC-")
    pathMap.set(`docs/developer/spec/${idOnly}.md`, targetRel)
    pathMap.set(`docs/developer/designs/${idOnly.replace(/^HC-/, "ZC-")}.md`, targetRel)
  }
}

async function migrateSibling(directory: string, pathMap: PathMap): Promise<void> {
  let entries
  try {
    entries = await readdir(directory, { withFileTypes: true })
  } catch {
    return
  }

  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".md")) continue
    if (entry.name.startsWith("HC-")) {
      pathMap.set(relative(root, join(directory, entry.name)).replace(/\\/g, "/"), relative(root, join(directory, entry.name)).replace(/\\/g, "/"))
      continue
    }
    const zcMatch = entry.name.match(/^(ZC-\d{3,})(?:-(.+))?\.md$/)
    if (!zcMatch) continue

    const sourcePath = join(directory, entry.name)
    const sourceRel = relative(root, sourcePath).replace(/\\/g, "/")
    const newId = zcMatch[1].replace(/^ZC-/, "HC-")
    let content = await readFile(sourcePath, "utf8")
    content = content
      .replaceAll("docs/developer/tasks/", "docs/developer/task/")
      .replaceAll("docs/developer/designs/", "docs/developer/spec/")
      .replace(/\bZC-(\d{3,}(?:-legacy)?)\b/g, "HC-$1")

    const taskHint = [...pathMap.values()].find(v => basename(v).startsWith(`${newId}-`) && v.includes("/task/"))
    let brief = zcMatch[2] ? taskBriefFromTitle(zcMatch[2]) : "实施说明"
    if (taskHint) {
      brief = basename(taskHint).slice(newId.length + 1).replace(/\.md$/, "")
    } else {
      const h1 = content.match(/^#\s+(.+)$/m)?.[1]?.trim()
      if (h1) brief = taskBriefFromTitle(h1)
    }

    const targetName = `${newId}-${brief}.md`
    const targetPath = join(directory, targetName)
    const targetRel = relative(root, targetPath).replace(/\\/g, "/")
    if (!(await exists(targetPath))) {
      await writeFile(targetPath, content, "utf8")
    }
    // 删除旧 ZC 文件
    if (sourcePath !== targetPath) {
      await rm(sourcePath, { force: true })
    }
    pathMap.set(sourceRel, targetRel)
    pathMap.set(join(relative(root, directory), `${zcMatch[1]}.md`).replace(/\\/g, "/"), targetRel)
    pathMap.set(join(relative(root, directory), `${newId}.md`).replace(/\\/g, "/"), targetRel)
  }
}

async function archiveCompletedActiveTasks(pathMap: PathMap): Promise<void> {
  const activeDir = join(root, TASK_DIR)
  const archiveDir = join(root, TASK_ARCHIVE_DIR)
  await mkdir(archiveDir, { recursive: true })
  for (const file of await listFiles(activeDir)) {
    if (dirname(file) !== activeDir) continue
    const name = basename(file)
    if (!name.startsWith("HC-") || !name.endsWith(".md")) continue
    const rel = relative(root, file).replace(/\\/g, "/")
    let content = await readFile(file, "utf8")
    let status = content.match(/^status:\s*(.+)$/m)?.[1]?.trim()
    if (status !== "已完成") continue

    // 保证 front matter id 已是 HC
    content = content.replace(/^id:\s*ZC-/m, "id: HC-")
    const target = join(archiveDir, name)
    const targetRel = relative(root, target).replace(/\\/g, "/")
    await writeFile(target, content, "utf8")
    await rm(file, { force: true })
    pathMap.set(rel, targetRel)
  }
}

function rewriteTaskDocumentContent(content: string, newId: string): string {
  let next = content
  // front matter id
  next = next.replace(/^id:\s*ZC-(\d{3,}(?:-legacy)?)\s*$/m, `id: ${newId}`)
  next = next.replace(/^parent_task:\s*ZC-(\d{3,}(?:-legacy)?)\s*$/m, (_m, num) => `parent_task: HC-${num}`)
  next = next
    .replaceAll("docs/developer/tasks/", "docs/developer/task/")
    .replaceAll("docs/developer/designs/", "docs/developer/spec/")
    .replace(/\bZC-(\d{3,}(?:-legacy)?)\b/g, "HC-$1")
  return next
}

async function rewriteRepository(pathMap: PathMap): Promise<void> {
  const textFiles = [
    ...await listFiles(join(root, "docs")),
    join(root, "README.md"),
    join(root, "AGENTS.md"),
    join(root, "CONTEXT.md"),
  ]

  // 按路径长度降序替换，避免短路径抢先匹配
  const replacements = [...pathMap.entries()]
    .filter(([from, to]) => from !== to)
    .sort((a, b) => b[0].length - a[0].length)

  for (const file of textFiles) {
    if (!file.endsWith(".md") && !file.endsWith(".json")) continue
    // 跳过即将删除的旧目录内文件（可能仍存在未移动残留）
    const rel = relative(root, file).replace(/\\/g, "/")
    if (rel.startsWith("docs/developer/tasks/") || rel.startsWith("docs/developer/designs/")) continue

    let content = await readFile(file, "utf8")
    const original = content

    for (const [from, to] of replacements) {
      content = content.split(from).join(to)
    }
    content = content
      .replaceAll("docs/developer/tasks/", "docs/developer/task/")
      .replaceAll("docs/developer/designs/", "docs/developer/spec/")
      .replace(/\bZC-(\d{3,}(?:-legacy)?)\b/g, "HC-$1")

    if (content !== original) {
      await writeFile(file, content, "utf8")
    }
  }
}

async function listFiles(directory: string): Promise<string[]> {
  let entries
  try {
    entries = await readdir(directory, { withFileTypes: true })
  } catch {
    return []
  }
  const nested = await Promise.all(entries.map(async entry => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return listFiles(path)
    return entry.isFile() ? [path] : []
  }))
  return nested.flat()
}

async function exists(path: string): Promise<boolean> {
  try {
    await readFile(path)
    return true
  } catch {
    return false
  }
}

async function removeIfExists(path: string): Promise<void> {
  try {
    await rm(path, { recursive: true, force: true })
  } catch {
    // ignore
  }
}

if (import.meta.main) {
  main().catch(error => {
    console.error(error)
    process.exitCode = 1
  })
}
