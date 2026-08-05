/** Web Shiki 静态语言加载器：基于 shiki/core + JS Regex Engine 的单例 Highlighter 工厂。 */

import { createHighlighterCore, type HighlighterCore } from "shiki/core"
import { createJavaScriptRegexEngine } from "shiki/engine/javascript"

import bash from "shiki/langs/bash.mjs"
import c from "shiki/langs/c.mjs"
import cpp from "shiki/langs/cpp.mjs"
import css from "shiki/langs/css.mjs"
import go from "shiki/langs/go.mjs"
import html from "shiki/langs/html.mjs"
import java from "shiki/langs/java.mjs"
import javascript from "shiki/langs/javascript.mjs"
import json from "shiki/langs/json.mjs"
import jsx from "shiki/langs/jsx.mjs"
import markdown from "shiki/langs/markdown.mjs"
import python from "shiki/langs/python.mjs"
import tsx from "shiki/langs/tsx.mjs"
import typescript from "shiki/langs/typescript.mjs"
import yaml from "shiki/langs/yaml.mjs"

import darkPlus from "shiki/themes/dark-plus.mjs"
import githubDark from "shiki/themes/github-dark.mjs"

let highlighterPromise: Promise<HighlighterCore> | null = null

/** 获取或按需初始化全局单例 Shiki Highlighter（零网络依赖，零 Oniguruma WASM）。 */
export async function getShikiHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      langs: [
        javascript,
        typescript,
        jsx,
        tsx,
        json,
        python,
        bash,
        go,
        java,
        c,
        cpp,
        html,
        css,
        yaml,
        markdown,
      ],
      themes: [darkPlus, githubDark],
      engine: createJavaScriptRegexEngine(),
    })
  }
  return highlighterPromise
}
