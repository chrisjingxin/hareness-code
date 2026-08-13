# HC-144：文件变更审批双栏与行内高亮 Diff 设计

> 原始需求：[HC-144](../task/archive/HC-144-文件审批高亮Diff.md)

## 通俗问题说明

当前审批里已经有准确的 unified diff，但它被塞进一整段普通文字中。用户需要自己辨认 `+`、`-`、
`@@` 和行号，在 Web 与终端上都难以快速确认“原来是什么、准备改成什么”。

本设计不重新计算文件变更，也不让前端根据工具参数猜测结果。Agent 在准备 mutation 时已经固定了
current、proposed content、参数指纹和 diff；界面只接收这同一计划产生的有界展示副本：

```text
PreparedFileMutation
  ├─ expected identity + proposed content → 批准后 CAS commit
  └─ bounded unified diff + metadata       → approval presentation
```

用户看到的 diff 可以因既有限额而截断，但截断会在操作按钮之前明确标记。批准的对象始终是完整的
prepared mutation，而不是“当前屏幕恰好显示出来的行”。这与 HC-142 记录的产品决定一致。

## 已确认现状

- `FileMutationService` 从同一 `PreparedFileMutation` 生成最多 200 行、16 KiB 的 unified diff，
  并将它嵌入动态审批 description；批准后以计划内 expected identity 和 proposed content 提交。
- `interaction.approval.payload` 当前严格只有 `interrupt_id`、`description`、`requests`、`decisions`；
  Interactive approval DTO 也只有 description、requests、decisions 和 deadline。
- Web `InteractionForm` 把 description 当段落、把 requests 当 JSON `<pre>`；现有 Shiki Worker 已有
  64 KiB/2,000 行限制、超时、缓存、熔断和纯文本降级。
- TUI `ConversationTimeline` 同样显示 description 与通用 requests 预览；当前 OpenTUI 版本已经提供
  `DiffRenderable` 的 `split|unified`、行号、增删颜色、Tree-sitter 和 resize 支持。
- 现有审批按批次逐项串行发出；非文件工具与无法识别的载荷必须继续走通用审批卡。
- HC-142 已过时：审批预览不要求完整显示，截断只需明确告知且继续允许批准或拒绝。

## 目标与非目标

### 目标

- 文件审批具有可扫描的路径、操作、增删统计、行号、增删底色和语法高亮。
- Web 同时提供 Codex 风格左右对比和 Claude 风格行内 diff；增删行使用 Codex 风格低饱和语义底色、
  深色行号槽和亮色左侧色带；TUI 根据终端宽度自动选择最可读形式。
- 展示数据只来自同一 prepared plan，不从中文 description、模型参数或当前磁盘重新构造拟议内容。
- Protocol、Interactive Core 与 Renderer 对未知/缺失展示数据向后安全降级，决策回写不依赖高亮成功。
- 保留已有 200 行/16 KiB 限额、截断可批准语义和全部文件安全边界。

### 非目标

- 不改变文件工具 schema、匹配算法、Snapshot 或 CAS 提交。
- 不实现逐字符/单词级 diff；首版以准确行级对齐和语法 token 高亮为准。
- 不提供跨文件 review、评论、暂存、选择部分行批准或编辑拟议内容。
- 不持久化用户选择的 diff 模式，也不把源码 diff 写入历史 Interaction。
- 不新增语法高亮、Diff 或虚拟列表依赖。

## 目标流程与职责

```text
SnapshotFileToolContract prepare
  → FileMutationService 固定 PreparedFileMutation
  → 从 plan.diff 投影 FileDiffPresentation（仍受 200 行 / 16 KiB 限制）
  → Host 内短生命周期 presentation seam 按当前 Run/调用指纹登记
  → RunCoordinator 逐项审批时取回 presentation
  → interaction.approval.payload.presentation
  → Interactive Core 只做 typed 投影
  ├─ Web：共享 parser 对齐 → split/unified React Diff card → Shiki
  └─ TUI：unified diff → OpenTUI DiffRenderable → Tree-sitter
```

职责边界：

- Agent 决定“展示哪一个已经准备好的 mutation”，并负责大小上限和截断事实。
- Protocol 只定义跨进程稳定形状，不包含 CSS、终端列宽或高亮 token。
- `presentation-shared` 只解析标准 unified diff、推导语言和对齐视图，不参与授权。
- Web/TUI 只选择布局、颜色和滚动；任何渲染失败都不能改变 approval decisions。

## Protocol 设计

`approvalRequest.payload` 增加可选 `presentation`。首个 tagged variant 为：

```ts
type FileDiffPresentation = {
  kind: "file_diff"
  operation: "write" | "edit" | "delete"
  path: string
  added_lines: number
  removed_lines: number
  truncated: boolean
  unified_diff: string
}
```

约束：

- 字段严格 `additionalProperties: false`；计数为非负整数，operation/kind 为枚举。
- `path` 是 backend 的工作区逻辑绝对路径，不暴露宿主机真实路径。
- `unified_diff` 是 Agent 已有有界 diff；Schema 给出字符上限，Python seam 继续执行真实 UTF-8
  16 KiB 与 200 行上限。空文件创建允许 `unified_diff=""`，界面显示“创建空文件”。
- presentation 可选是为了让非文件审批和缺失展示数据继续合法；未知 kind 由协议校验拒绝，不能静默
  当成已知文件 diff。
- `description` 保留简短的人类摘要和批次序号；当存在 file_diff 时不再重复嵌入整段 diff。
  `requests` 继续保留原协议事实，Renderer 对文件卡默认不重复铺开参数。

## 同一计划绑定与短生命周期传递

展示 seam 不能解析中文 description，也不能在 `RunCoordinator` 根据 `old_string/new_string` 重算文件。
实现采用 Host 生命周期内、内存有界的 `ApprovalPresentationStore`（最终命名可按邻近模块调整）：

1. 动态 HITL description 回调仍先 canonicalize request，并调用 contract 的单一 `approval_details()`；
   details 同时返回短 description 和由当前 `PreparedFileMutation.diff` 投影的 presentation。
2. Store 本身挂在单个 `RunState` 上，再以 `tool name + 原始参数稳定指纹` 登记
   presentation；Run/Thread 身份由外层实例隔离。它只保存已经受 200 行/16 KiB 限制的
   文本和元数据，不复制 current/proposed 完整内容。
3. `RunCoordinator` 处理对应 action request 时用同一身份取回；命中才加入 wire payload。
   同一 Run 中的 LangGraph interrupt 重放可复用这份只读展示，直到 Run 结束清理；它不扩大提交权限。
4. 审批结束、Run 取消/结束、TTL/LRU 淘汰或 Host close 都清除条目。取回失败、身份不一致或数据畸形时
   只退回现有通用 description/requests，不阻止拒绝，也不把缺失展示当成批准依据。

presentation 是只读视图，不参与 commit。真正写盘仍调用已存在的
`prepared(thread_id, tool_call_id, fingerprint)` 并消费同一计划；因此 UI 字段不能放宽或替代 Tool Call
ID、参数 fingerprint、Snapshot、Policy、expected identity 或 backend CAS。

## Diff 解析与对齐

Agent 继续生成标准 unified diff，避免 Protocol 携带 Web/TUI 专属的占位行。CLI 的共享纯 parser：

- 识别 `---`、`+++`、`@@ -old,count +new,count @@`、context/add/remove 和
  `\ No newline at end of file`；保留每行原文本和 old/new 1-based 行号。
- 在一个 hunk 内把连续 remove/add block 以最大长度配对；缺失侧产生仅用于 split 布局的空 cell。
  这只是视觉对齐，不声称逐行语义配对或逐字符差异。
- unified 模式严格保持服务端行顺序；split 模式不跨 hunk 移动行。
- 解析失败返回 tagged failure。Web 显示原始 unified diff 纯文本；TUI 的 Diff renderer 失败时显示同一
  原文。空 diff 使用 operation 和统计生成空文件提示。
- 语言只由共享 `language-catalog` 根据 `path` 后缀推导；未知后缀为 plaintext。

## Web 交互与视觉规则

- 卡片头部依次显示操作、路径、`+N / -N`；`truncated=true` 时在 diff 上方显示持续可见的警告：
  “预览已按 200 行或 16 KiB 上限截断；批准仍会应用完整变更”。
- 可用内容宽度不少于 760 CSS px 时默认 split，低于该值默认 unified；用户可在当前审批内切换
  “左右 / 行内”。窗口 resize 只在用户尚未手动选择时更新默认模式。
- split 两栏共享 hunk 边界与垂直行；各栏显示独立 old/new 行号。长行允许水平滚动，不静默裁掉文本。
- unified 按 context/remove/add 顺序显示双行号；删除为红色语义面、新增为绿色语义面，颜色之外保留
  `-`/`+` 标记，满足非颜色识别。
- Shiki 对 before/after 代码按 hunk 的连续源码片段高亮，再把 span 映射回行，避免逐行高亮丢失多行
  语法上下文。Worker loading 不锁审批按钮；unknown、too-large、timeout、load-failed 都保留 diff
  语义色和纯文本。
- 文件卡默认不再铺开通用 requests JSON；保留可访问的“请求参数”折叠入口用于诊断。非文件审批
  继续使用当前 description + requests 预览。
- Diff 模式切换只属于当前卡片的本地布局状态，不发送业务 intent。普通拒绝是 fail-closed 动作，点击
  后立即提交；批准仍保留二次提交确认，拒绝并反馈仍需填写反馈后提交。
- Web 待处理审批 Dock 是对话列中的可收缩 flex item，受视口剩余高度约束并独立纵向滚动；页面根层
  继续保持固定工作台布局，避免长 Diff 撑出页面后遮挡审批动作和 Composer。

## TUI 交互与视觉规则

- `ConversationTimeline` 获得终端可用宽度；审批内容宽度不少于 120 列时设置 `view="split"`，否则
  `view="unified"`。resize 后由 OpenTUI renderer 重建视图，不改变当前选中的审批决定。
- 先用共享 parser 确认 Agent 提供的 `unified_diff` 结构可用，再交给 OpenTUI
  `DiffRenderable`，开启行号、现有 `markdownSyntax` 和 `getCommonSyntaxClient()`；解析失败直接显示原文。
- 有界预览如果在 hunk 中途截断，原始 hunk header 仍描述完整 mutation，而 OpenTUI 会
  严格校验可见行数。交给 renderer 前要移除非标准截断 marker，并把展示副本的 hunk
  old/new count 收缩为实际可见行；Protocol 统计和 prepared mutation 不得被改写。
- 增删背景使用与普通代码面具有明确色差的深绿 `#1a4d1a` / 深红 `#4d1a1a`，内容区与行号槽
  保持连续色块并保留 `+`/`-`；颜色须避免在终端有限色阶映射后与普通深灰背景合并。长行采用可读
  换行，不能因为列宽省略源码。
- 截断警告位于 Diff 前、审批选择器前。Diff/Tree-sitter 异常时显示原始纯文本，选择器和超时逻辑
  继续工作。
- 首版不增加新的审批快捷键或 TUI 模式切换控件，避免与现有 select/反馈输入争抢焦点。

## 关键 invariant

1. **授权来源不变**：UI presentation 永远不能用于重建、修改或提交文件。
2. **同源展示**：file_diff 只从对应 `PreparedFileMutation.diff` 投影，不读取当前磁盘、不解析中文说明。
3. **有界且显式**：wire diff 不超过既有 200 行/16 KiB；截断状态在按钮之前始终可见。
4. **截断可批准**：显示上限和视口不是安全拒绝条件；所有现有允许/拒绝决定保持可用。
5. **失败不致盲区**：presentation 缺失/畸形、高亮失败或 renderer 失败时至少显示 description 与有界原文，
   不出现空白审批。
6. **安全边界不降级**：Workspace、Policy、敏感路径、Snapshot、一次性计划、改参失效和 CAS 冲突不变。
7. **源码不持久化**：presentation 不进入 Transcript、SQLite、日志、规则记录或历史 Interaction 卡。
8. **双端同语义**：Web/TUI 可以布局不同，但 operation、path、统计、truncated 和 diff 行内容一致。

## 错误与降级语义

| 情况 | 可观察结果 | 审批行为 |
| --- | --- | --- |
| 无 presentation / 非文件工具 | 现有 description + requests | 不变 |
| presentation 身份未命中或已淘汰 | 通用审批卡，不伪造 diff | 不变 |
| unified diff 解析失败 | 原始有界 diff 纯文本 + 提示 | 不变 |
| 未知语言 | diff 语义色 + plaintext | 不变 |
| Shiki/Tree-sitter 超时或失败 | 原位纯文本降级 | 不变，不锁按钮 |
| `truncated=true` | 醒目截断 banner + 已展示部分 | 仍可允许/拒绝 |
| 空文件创建 | “创建空文件”，统计 `+0/-0` | 仍可允许/拒绝 |
| Protocol 载荷违反 Schema | 既有协议错误/fail-closed | 不接受畸形 Interaction |

## 可观察验收与测试

- Python：create/edit/delete 的 details 与 prepared plan 一致；多 Run/Thread/相同参数、LRU/TTL、取消、
  消费、参数变化和缺失 resolver 不串用；截断仍发送 approval decisions。
- Protocol：正反 fixtures 覆盖 file_diff、空 diff、枚举/计数/额外字段/长度错误；生成的 TS/Python 类型一致。
- Interactive：presentation 原样投影且不影响 timeout/reject fallback；未知/缺失数据安全。
- Shared presenter：多 hunk、replace block、纯增/纯删、不同长度配对、no-newline、CRLF 文本和畸形输入。
- Web：宽/窄默认、手动切换后 resize、行号/增删标记、截断 banner、折叠参数、ARIA、Shiki 各降级状态。
- TUI：119/120 列边界、resize、split/unified、空/截断/畸形 diff、Tree-sitter 降级和审批选择/反馈回归。
- 项目：运行 Protocol check、Python/CLI focused tests、build、typecheck、全量 test、project:check 与 diff check。

## 回滚思路

Protocol 的 `presentation` 是可选字段。若新 Renderer 出现回归，可先停止 Agent 附带该字段，双端立即
回到现有 description/requests 卡；不需要修改文件 mutation、审批决策或提交路径。随后删除未使用的
presentation DTO/Renderer 即可，已准备和已提交的文件计划不受影响。

## 已确认的体验选择

- Web 默认断点：可用 diff 宽度 `760px`；更宽默认左右、更窄默认行内，仍可手动切换。
- TUI 默认断点：审批内容宽度 `120` 列；更宽左右、更窄行内，不新增切换快捷键。
- 截断文案明确说明“批准应用完整变更”，并继续保留所有现有审批选项。
