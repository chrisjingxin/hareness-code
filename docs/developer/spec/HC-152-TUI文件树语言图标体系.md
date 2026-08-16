# HC-152 规格说明：TUI 文件树语言图标体系

## 1. 领域模型与接口定义

### 1.1 `FileIconInfo` 契约定义

在 `packages/cli/src/tui/presentation/sidebar/file-icons.ts` 中定义：

```ts
export type FileIconInfo = {
  /** 渲染图标字符（带尾部空格以保持固定 2 单元格宽度） */
  readonly icon: string
  /** 品牌专色（十六进制颜色值） */
  readonly color: string
}

/** 获取文件或目录的图标与专色 */
export function getFileIconInfo(name: string, kind: "directory" | "file" | "symlink", expanded?: boolean): FileIconInfo
```

### 1.2 规则映射表优先级

1. **特殊全名匹配**（优先级最高）：
   - `package.json` -> 📦 `#E8274B`
   - `tsconfig.json` ->  `#3178C6`
   - `Dockerfile` / `docker-compose.yml` ->  `#2496ED`
   - `.gitignore` / `.gitattributes` ->  `#F05032`
   - `.env` / `.env.*` ->  `#EBD15B`
   - `README.md` / `CHANGELOG.md` ->  `#519ABA`
   - `Makefile` ->  `#6D8086`
2. **文件扩展名匹配**：
   - Python: `py`, `ipynb` -> ` ` `#3572A5`
   - TypeScript: `ts`, `tsx` -> ` ` `#3178C6`
   - JavaScript: `js`, `jsx`, `mjs`, `cjs` -> ` ` `#F7DF1E`
   - Rust: `rs` -> ` ` `#DEA584`
   - Go: `go` -> ` ` `#00ADD8`
   - C/C++: `c`, `h` -> ` ` `#599EFF`；`cpp`, `hpp`, `cc` -> ` ` `#F34B7D`
   - Java/Kotlin: `java`, `kt` -> ` ` `#B07219`
   - Web: `html` -> ` ` `#E34F26`；`css`, `scss`, `less` -> ` ` `#1572B6`
   - 配置/标记: `json` -> ` ` `#CBCB41`；`yaml`, `yml` -> ` ` `#CB171E`；`toml` -> ` ` `#9C4221`
   - Shell: `sh`, `bash`, `zsh` -> ` ` `#4EAA25`
   - Markdown: `md`, `markdown` -> ` ` `#519ABA`
   - SQL: `sql` -> ` ` `#E38C00`
   - 默认常规文件 -> `📄 ` `#A6ACCD`
3. **文件夹状态**：
   - 展开: `󰝰 `（`\udb81\udf70` 或 `📂`）`#E6BB72`（琥珀金）
   - 收起: `󰉋 `（`\udb81\ude4b` 或 `📁`）`#E6BB72`（琥珀金）
4. **符号链接**：`↪ ` `#C4A7F2`

---

## 2. 视觉不变量

1. 图标与文字之间有 1 个半角空格分隔，视觉平整不黏连。
2. 选中行（`isSelected && focused`）时，前缀指示器与整行名称高亮为 `tuiTheme.primary`，图标颜色与文字保持协调。
