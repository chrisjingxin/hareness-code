/** 加载 Web JS/CSS 构建产物；源码开发模式下即时构建同一份资源。 */

import { existsSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { resolve } from "node:path"

export type WebAssets = {
  script: string
  style: string
}

export async function browserBundle(): Promise<WebAssets> {
  const built = resolve(import.meta.dir, "../../dist/web.js")
  const builtStyle = resolve(import.meta.dir, "../../dist/web.css")
  if (existsSync(built) && existsSync(builtStyle)) {
    const [script, style] = await Promise.all([readFile(built, "utf8"), readFile(builtStyle, "utf8")])
    return { script, style }
  }
  const result = await Bun.build({
    entrypoints: [resolve(import.meta.dir, "app.tsx")],
    target: "browser",
    minify: true,
  })
  const scriptOutput = result.outputs.find(output => output.path.endsWith(".js"))
  const styleOutput = result.outputs.find(output => output.path.endsWith(".css"))
  if (!result.success || !scriptOutput || !styleOutput) {
    throw new Error(
      result.logs.map(log => log.message).join("\n") || "Web bundle build failed",
    )
  }
  return {
    script: await scriptOutput.text(),
    style: await styleOutput.text(),
  }
}
