/**
 * 开发文档入口、链接和任务引用校验。
 */

import { readFile } from "node:fs/promises"
import { dirname, join, relative, resolve, sep } from "node:path"

import { TASK_BOARD_PATH, TASK_DIR, TASK_ID_MATCH_GLOBAL, listMarkdownFiles, loadArchivedTaskIds, loadTasks } from "./tasks"

const root = resolve(import.meta.dir, "../..")

/** 校验文档入口、任务看板和所有本地 Markdown 链接。 */
export async function checkDocs(projectRoot = root): Promise<void> {
  const required = [
    "README.md",
    "docs/user/快速开始.md",
    "docs/user/模型配置.md",
    "docs/user/交互使用.md",
    "docs/user/故障排查.md",
    "docs/developer/architecture/架构总览.md",
    "docs/developer/project/开发工作流.md",
    "docs/developer/project/变更检查清单.md",
    TASK_BOARD_PATH,
    `${TASK_DIR}/README.md`,
  ]
  for (const path of required) {
    try {
      await readFile(join(projectRoot, path), "utf8")
    } catch {
      throw new Error(`缺少必需文档：${path}`)
    }
  }

  const documents = [join(projectRoot, "README.md"), ...await listMarkdownFiles(join(projectRoot, "docs"))]
  const activeTaskIds = (await loadTasks(projectRoot)).map(task => task.metadata.id)
  const taskIds = new Set([...activeTaskIds, ...(await loadArchivedTaskIds(projectRoot))])
  for (const document of documents) {
    const content = await readFile(document, "utf8")
    for (const link of markdownLinks(content)) {
      if (isExternalLink(link)) continue
      const target = link.split("#", 1)[0]
      if (!target) continue
      const resolved = resolve(dirname(document), target)
      try {
        await readFile(resolved)
      } catch {
        throw new Error(`${relative(projectRoot, document)} 包含无效本地链接：${link}`)
      }
    }

    // 归档研究材料是历史快照，允许保留当时已经退出当前任务系统的编号；活动文档仍必须严格校验。
    if (!isHistoricalResearchDocument(projectRoot, document)) {
      for (const id of content.match(TASK_ID_MATCH_GLOBAL) ?? []) {
        if (!taskIds.has(id)) throw new Error(`${relative(projectRoot, document)} 引用了不存在的任务：${id}`)
      }
    }
  }
}

function isHistoricalResearchDocument(projectRoot: string, document: string): boolean {
  const archive = join(projectRoot, "docs/developer/research/archive")
  return document === archive || document.startsWith(`${archive}${sep}`)
}

function markdownLinks(content: string): string[] {
  return [...content.matchAll(/\[[^\]]*\]\(([^)]+)\)/g)].map(match => match[1]?.trim() ?? "")
}

function isExternalLink(value: string): boolean {
  return /^(?:https?:|mailto:|#)/i.test(value)
}
