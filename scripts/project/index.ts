/**
 * 仓库协作命令入口；具体规则按任务、文档和发布职责分离。
 */

import { resolve } from "node:path"

import { checkDocs } from "./docs"
import { checkRelease, setVersion } from "./release"
import { checkTasks, claimTask, completeTask, syncTasks } from "./tasks"

export * from "./docs"
export * from "./release"
export * from "./tasks"

type CommandOptions = Record<string, string>

const root = resolve(import.meta.dir, "../..")

/** 根据子命令运行项目管理操作，供 package.json 统一调用。 */
export async function main(argv = process.argv.slice(2)): Promise<void> {
  const [command, ...args] = argv
  const options = parseOptions(args)
  switch (command) {
    case "docs:check":
      await checkDocs(root)
      return
    case "tasks:sync":
      await syncTasks(root)
      return
    case "tasks:check":
      await checkTasks(root)
      return
    case "task:claim": {
      const id = positional(args)[0]
      if (!id) throw new Error("用法：task:claim <ID> --owner <名称> --branch <分支>")
      await claimTask(root, id, requiredOption(options, "owner"), requiredOption(options, "branch"))
      return
    }
    case "task:complete": {
      const id = positional(args)[0]
      if (!id) throw new Error("用法：task:complete <ID> --evidence <测试证据> [--references <提交或 PR>]")
      await completeTask(root, id, requiredOption(options, "evidence"), options.references)
      return
    }
    case "version:set": {
      const version = positional(args)[0]
      if (!version) throw new Error("用法：version:set <SemVer>")
      await setVersion(root, version)
      return
    }
    case "release:check":
      await checkRelease(root)
      return
    case "project:check":
      await checkDocs(root)
      await checkTasks(root)
      await checkRelease(root)
      return
    default:
      throw new Error("用法：docs:check|tasks:sync|tasks:check|task:claim|task:complete|version:set|release:check|project:check")
  }
}

function parseOptions(args: readonly string[]): CommandOptions {
  const options: CommandOptions = {}
  for (let index = 0; index < args.length; index++) {
    const value = args[index]
    if (!value?.startsWith("--")) continue
    const [key, inline] = value.slice(2).split("=", 2)
    const next = inline ?? args[index + 1]
    if (!key || !next || next.startsWith("--")) throw new Error(`选项 ${value} 缺少值`)
    options[key] = next
    if (inline === undefined) index += 1
  }
  return options
}

function positional(args: readonly string[]): string[] {
  const values: string[] = []
  for (let index = 0; index < args.length; index++) {
    const value = args[index]
    if (!value?.startsWith("--")) values.push(value)
    else if (!value.includes("=")) index += 1
  }
  return values
}

function requiredOption(options: CommandOptions, key: string): string {
  const value = options[key]
  if (!value) throw new Error(`缺少 --${key}`)
  return value
}

if (import.meta.main) {
  main().catch(error => {
    console.error(`project-management: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  })
}
