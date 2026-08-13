---
id: HC-106
title: 按蓝色工作台设计整改 Web UI 并支持显式深浅主题
feature_area: Web UI 基础工作台（历史）
parent_task: HC-079
decomposed_by: 历史未记录
priority: P1
status: 已过时
owner: chrisjingxin
branch: codex/zc-106
reviewed_at: 2026-08-09
review_due: -
scope: 在不改变 Web Interactive Adapter 业务语义和 Handoff 安全边界的前提下，将现有 React Web 工作台整改为用户提供的蓝色三栏设计，提供默认浅色、可显式切换的深色主题，并补齐桌面与移动端的响应式、状态和可访问性表现。
acceptance: /web 首次打开固定使用浅色；用户可在当前 Web 接管期间切换深浅主题；1440×900 与 390×844 下的品牌栏、顶栏、Thread 导航、Timeline、Tool、Interaction、Composer 和工具抽屉符合 HC-106 设计且无横向溢出；所有现有 Web 工作流、权限和安全边界保持不变。
user_docs: docs/user/交互使用.md
developer_docs: docs/developer/spec/HC-106-按蓝色工作台设计整改WebUI.md
test_evidence: "bun test tests/web/application/adapter.test.ts tests/web/presentation: 93 pass 0 fail；bun run build: exit 0；bun run typecheck: exit 0；bun run test: 599 passed / 2 failed（test_package_root_contains_only_entrypoints 与 test_auto_edit_writes_without_interruption_but_shell_still_requires_approval 为预存基线失败，与本次改动无关，已用 git stash 复验基线）；bun run project:check: exit 0；真实 Chrome 抽查（1440×900/390×844 light/dark）22/22 PASS（几何、无根横向滚动、抽屉/scrim、tab、主题切换、状态语义色）"
references: docs/developer/task/HC-104-实现WebInteractiv.md、docs/developer/task/HC-105-建立WebBrowserE2E.md、docs/developer/task/HC-124-统筹WebUI工作台体验升级与.md
completed_at: -
---

## 背景

HC-104 已将 `/web` 从最小 DOM 页面改为 React DOM 工作台，并通过共享 `InteractiveController` 覆盖 Thread、Run、Interaction、模型、Skill、MCP 和 Slash Command。页面功能已经具备，但当前视觉实现仍使用暖灰/琥珀 token、通用工具栏和另一套消息布局，与用户确认的蓝色 Web UI 设计稿以及 TUI 的深色开发工具气质差异明显。

用户提供了结构一致的浅色和深色 HTML 设计稿。两份稿件共同确认了三栏信息架构、紧凑的 54px 顶部 chrome、蓝色唯一强调色、带角色标识的 Timeline、带状态摘要的 Tool、固定 Composer，以及 Model/Skills/MCP/Status 共享的右侧工作台。稿件中的时间只用于表达信息层级；当前 DTO 没有时间戳，实现不得编造时间。

## 当前存在的问题

- `styles.css` 的浅色是暖灰白，深色是暖黑配铜色；用户设计稿使用冷灰蓝底色和蓝色强调色，品牌识别、选中态和运行态不一致。
- 当前主题只跟随 `prefers-color-scheme`，用户不能显式切换，也无法保证首次打开默认浅色。
- 顶栏把 Model、Skills、MCP、Status、Help、返回和退出全部平铺；在中窄桌面与移动端容易拥挤，与设计稿的紧凑 meta chip + overflow action 不一致。
- 当前 Timeline 主要用左右消息气泡区分角色；设计稿要求角色标识和统一左对齐阅读流，Tool 与 Interaction 作为同一时间线中的缩进块。当前 DTO 没有时间戳，整改只能使用真实可用字段。
- 右侧面板当前一次只展示单个标题和内容；设计稿要求一个稳定的“工作台”容器，通过 Model/Skills/MCP/Status tab 切换。
- `styles.css` 同时保留 HC-104 早期 class 和现有 presentation canonical class，视觉规则重复，后续维护容易出现覆盖顺序依赖。
- 用户提供的 390px 静态稿只隐藏桌面侧栏，实际渲染仍出现顶栏和 Composer 横向裁切；实现不能直接复制该响应式 CSS。

## 为什么现在要修改

HC-105 将建立真实 Browser E2E 和 light/dark 截图基线。如果先对旧主题固化截图，再实施新设计，测试与基线需要重复建设。应先完成单一视觉整改，再由 HC-105 对最终 UI 建立稳定验收证据。

## 目标设计

```text
InteractiveSnapshot + Web 表现状态
  → WebApp 选择桌面/窄屏结构
  → semantic design token 解释浅色或深色
  → Thread / Timeline / Interaction / Composer / Utility Workspace
  → 用户操作仍只派发既有 WebIntent / InteractiveIntent
```

关键原则：

- 用户提供的浅色/深色 HTML 是视觉基准；业务数据、文案和可用性仍以当前 React 组件与共享 Core 为准，不复制静态示例数据或无功能按钮。
- 浅色是每次新 Web 接管的固定默认值，不读取系统主题；用户可在当前页面显式切换为深色或切回浅色。
- 主题只属于 Web 表现状态，不写入 Protocol、Agent Host、Thread 数据或用户配置，不触发 RPC。
- HC-104 的 Adapter/Core 依赖方向、Handoff、capability、busy、Interaction、Markdown 和安全 invariant 全部保持不变。
- 桌面使用 260px Thread 导航 + 弹性 Timeline + 372px 工具工作台；工具工作台关闭时中央列占用释放空间。
- 小于 900px 时只保留 Timeline 与 Composer；Thread 和工具工作台使用互斥抽屉，不能照抄设计稿中会裁切内容的固定宽度布局。

完整接口、token、布局、状态和测试决策见 [HC-106 方案设计](../spec/HC-106-按蓝色工作台设计整改WebUI.md)。

## 实施步骤

1. 清理 `styles.css` 的重复旧 class，建立单一 semantic token 层；写入设计稿确认的 light/dark token，默认 light，验证所有既有组件 class 均有唯一规则来源。
2. 在 Web Adapter 增加纯表现层的 `theme` 与 header overflow 状态及对应 intent；在 `WebApp` 根节点通过 `data-theme` 应用主题，不接入系统主题、Protocol 或后端持久化。
3. 整改 `web-app.tsx` 的品牌区、紧凑顶栏、meta chip、返回动作、主题/帮助/退出 overflow menu；保持 capability 和 busy 决策来自现有 snapshot。
4. 按设计稿重排 Thread 导航、Timeline message、Tool、历史/当前 Interaction 和 Composer；复用现有组件数据与 intent，不复制 reducer 或增加静态控件。
5. 将 UtilityPanels 整理为稳定工作台壳与 Model/Skills/MCP/Status tab；保留 Help 入口、搜索、刷新、错误、空态、管理权限和 typed intent。
6. 完成 `<900px` 的响应式收敛：顶栏只显示必要信息，Thread/Utility 使用抽屉与遮罩，Interaction 和 Composer 在 390px 内换行，长路径/代码只在自身滚动。
7. 为 theme、header menu、tab、响应式 class、ARIA 状态和视觉语义增加 presentation/adapter focused tests；不得用大范围 snapshot 替代行为断言。
8. 更新 Web 用户说明；运行 CLI focused tests、build、typecheck 和项目级检查，再交给 HC-105 建立真实浏览器双主题截图基线。

## 范围

- `packages/cli/src/web/application/adapter.ts` 的 Web 表现状态与 intent。
- `packages/cli/src/web/presentation/` 的工作台结构、主题、组件视觉和响应式布局。
- 对应 Web Adapter/presentation 单元与 DOM 测试。
- `/web` 深浅主题使用说明和设计/验收文档。

## 非范围

- 不改变 `InteractiveController`、Agent RPC、Protocol、Python Host、Handoff 或控制租约。
- 不新增 Thread、Run、Model、Skill、MCP、Interaction 或附件能力。
- 不把主题偏好写入 TOML、SQLite、localStorage 或服务端；跨 `/web` 接管持久化另立需求。
- 不引入新的 UI 框架、CSS-in-JS、远端字体、远端图片或图标依赖。
- 不复制设计稿中的硬编码模型、Thread、工具输出、版本号和示例文案。
- 不在本任务建立 Playwright fixture 或提交最终视觉基线；该工作属于 HC-105。

## 验收清单

- [ ] 每次新 `/web` 接管首次渲染均为浅色，不受操作系统 `prefers-color-scheme` 影响；切换深色/浅色无需刷新并更新可访问名称。
- [ ] 主题变化只更新 Web 表现状态和 `data-theme`，不产生 Agent RPC、Thread 变更或 Handoff lifecycle 消息。
- [ ] 1440×900 下品牌栏宽 260px、顶栏高 54px、中央阅读列最大约 880px、工具工作台宽约 372px；关闭工具工作台后中央列自然扩展。
- [ ] 390×844 下 Thread 与工具工作台均通过抽屉访问，顶栏、Interaction、Composer 和操作按钮无裁切、横向溢出或不可达区域。
- [ ] 用户、Assistant、System、Tool、running/completed/failed、approval/question、loading/empty/error/disabled/readonly 均使用 design 中的语义 token 和层级。
- [ ] Model/Skills/MCP/Status 在同一工具工作台内通过 tab 切换；Help、主题、返回 TUI 与退出入口在桌面和移动端均可达。
- [ ] active Run/Interaction 时的 Thread 切换和返回限制、capability 隐藏/禁用、Markdown 安全和 Handoff 规则与 HC-104 保持一致。
- [ ] 全部可交互元素具有 hover、pressed、focus-visible 和 disabled 状态；窄屏主要触控目标不小于 44px，并遵守 `prefers-reduced-motion`。
- [ ] `styles.css` 不再存在同一组件的两套历史 class 规则或依赖文件后半段覆盖前半段才能正确显示的情况。
- [ ] Web focused tests、`bun run build`、`bun run typecheck`、`bun run test` 与 `bun run project:check` 通过；随后由 HC-105 生成最终四组浏览器截图。

## 前置

- HC-104。

## 后续

- HC-105。

## 定期复核记录

- 2026-08-09（Codex）：任务的主题切换、三栏骨架和组件基础已经成为当前实现历史，但“蓝色工作台”作为未完成的最终视觉目标已被用户最新确认的 HC-124“暖中性色画布 + 克制蓝色交互强调”取代。继续按本任务验收会与当前设计源冲突，因此标记为已过时；已有实现与测试证据保留，剩余响应式、视觉层级和可访问性分别由 HC-125～HC-130 接管。
