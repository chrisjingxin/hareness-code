#!/usr/bin/env bun
/** 运行源码化 npm 包的安装前/安装后完整性门禁。 */

import { resolve } from "node:path"
import { validateVendoredWorkspace } from "./vendor_policy"

const root = resolve(import.meta.dir, "../..")
const issues = validateVendoredWorkspace(root)
if (issues.length > 0) {
  console.error(["源码化 npm 依赖门禁失败：", ...issues.map((issue) => `- ${issue}`)].join("\n"))
  process.exit(1)
}

console.log("源码化 npm 依赖门禁通过：五个发布包、provenance、workspace lock、Windows x64 DLL 和 ws 补丁均有效")
