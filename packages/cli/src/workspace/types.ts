/**
 * WorkspaceExplorer 领域契约：工作区文件树与文件预览的纯数据形状。
 *
 * 本模块独立于 InteractiveController（文件浏览不是 Agent 领域行为），只依赖
 * node 内置模块与 presentation-shared 纯函数，保证删除时不影响 Interactive Core。
 * 所有错误消息只含脱敏中文文案，绝不携带绝对路径、用户名、堆栈或文件内容。
 */

/** 文件树中的单行：目录、文件或符号链接；目录不带尾斜杠。 */
export type WorkspaceTreeRow = {
  /** 相对 workspace 根的路径；目录不带尾斜杠。 */
  readonly path: string
  /** 展示名（最后一段）。 */
  readonly name: string
  readonly kind: "directory" | "file" | "symlink"
  /** 路径段数 - 1；根目录的子项为 0。 */
  readonly depth: number
  /** 目录是否展开；文件恒为 false。 */
  readonly expanded: boolean
  /** 非 Git 懒加载期间目录行处于加载中。 */
  readonly loading: boolean
  /** 目录行恒 true；文件恒 false；目录 symlink 为 false（不展开）。 */
  readonly hasChildren: boolean
}

/** 文件树整体状态：加载、就绪、错误与超限截断提示。 */
export type WorkspaceTreeState = {
  readonly status: "idle" | "loading" | "ready" | "error"
  readonly rows: readonly WorkspaceTreeRow[]
  /** 工作区全部已知条目（用于 @ 提及搜索与目录浏览，包含折叠目录内的深层条目）。 */
  readonly allEntries?: readonly WorkspaceTreeRow[]
  /** 左侧文件树当前高亮行；null 表示无选中。 */
  readonly selectedPath: string | null
  /** 文件过多被截断时为 true，界面展示"仅展示部分内容"。 */
  readonly limited: boolean
  readonly message?: string
}

/** 单个文件的预览结果：内容、语言、行数、指纹与截断标记。 */
export type WorkspaceFilePreview = {
  readonly path: string
  readonly name: string
  readonly content: string
  /** canonical 语言 id；未知为 null（plaintext）。 */
  readonly language: string | null
  readonly sizeBytes: number
  /** 展示行数（可能因 2000 行截断而小于真实行数）。 */
  readonly lineCount: number
  readonly modifiedAtMs: number
  /** 任一截断（字节/行/行宽）为 true。 */
  readonly truncated: boolean
  /** stat 指纹（mtimeMs+size）；作为 PreviewCache key 的一部分。 */
  readonly version: string
}

/** 预览状态机：加载中 → 就绪 / 不支持 / 错误。 */
export type WorkspacePreviewState =
  | { readonly status: "idle" }
  | { readonly status: "loading"; readonly path: string }
  | { readonly status: "ready"; readonly file: WorkspaceFilePreview }
  | { readonly status: "unsupported"; readonly path: string; readonly reason: string; readonly sizeBytes: number }
  | { readonly status: "error"; readonly path: string; readonly code: WorkspaceErrorCode; readonly message: string }

/** Explorer 对外发布的完整快照：树 + 预览两个分片。 */
export type WorkspaceSnapshot = {
  readonly tree: WorkspaceTreeState
  readonly preview: WorkspacePreviewState
}

/** 表现层唯一输入入口：加载、刷新、目录展开、文件预览与预览刷新。 */
export type WorkspaceIntent =
  | { type: "workspace.load" }
  | { type: "workspace.refresh" }
  | { type: "workspace.toggle-directory"; path: string }
  | { type: "workspace.preview-file"; path: string }
  | { type: "workspace.refresh-preview"; path: string }

/** 稳定错误码；消息只含脱敏中文文案（不含绝对路径/用户名/堆栈/内容）。 */
export type WorkspaceErrorCode =
  | "invalid-path"
  | "outside-workspace"
  | "not-found"
  | "permission-denied"
  | "not-directory"
  | "not-file"
  | "unsupported-file"
  | "unsupported-encoding"
  | "workspace-too-large"
  | "workspace-changed"
  | "io-error"
  | "invalid-argument"

/** intent 受理结果；rejected 恒携带稳定错误码与脱敏文案。 */
export type WorkspaceOutcome =
  | { status: "accepted" }
  | { status: "rejected"; code: WorkspaceErrorCode; message: string }

/** WorkspaceExplorer 领域接口：快照、订阅、intent 受理与关闭。 */
export interface WorkspaceExplorer {
  getSnapshot(): WorkspaceSnapshot
  subscribe(listener: (snapshot: WorkspaceSnapshot) => void): () => void
  dispatch(intent: WorkspaceIntent): Promise<WorkspaceOutcome>
  close(): Promise<void>
}
