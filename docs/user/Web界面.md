# Web 工作台

Web 工作台通过 `/web` 在浏览器中接管当前 Thread。它与 TUI 共用同一条 Timeline、Run、审批和 Composer 语义；本页说明 Web 端的视觉层级和外围操作。

## 主题与状态

- 页面使用暖中性画布：浅色以骨白和白色为主，深色以炭黑和暖灰为主。
- 蓝色只用于当前选中、运行中、焦点和链接等交互强调；成功、等待、失败和取消仍使用各自的文字与图标语义。
- 顶栏的 Run 状态是最高状态层级，会显示“正在运行”“等待交互”“运行失败”等可读文案和状态点。状态来源于当前 Run，不由 CSS 推测。
- 顶栏的 Model、Approval、返回 TUI 和更多操作属于次级 chrome；更多菜单可以切换浅色/深色主题。

## 工作区外围

- 左侧 Sidebar 的“新建 Thread”是次级按钮，不会压过当前 Run 或对话内容。
- 当前 Thread 使用浅蓝背景和左侧蓝色轨道标识；未选中的 Thread 仅在 hover 时显示轻量背景。
- Context Dock 的 Code、Model、Skills、MCP、Status tab 与关闭按钮共用一层 header，减少重复标题占用；Help 仍从顶栏更多菜单打开。
- Sidebar、Dock、Thread、Files、Model、Skills、MCP 和 Status 的现有操作保持不变。打开/关闭面板不会改变 Thread 或 Agent 状态。

## 控件与可访问性

Web 控件使用统一的 compact、standard、field 尺寸和暖中性边界。必要的小号文字使用较深的 muted token，键盘聚焦显示蓝色 focus ring；运行、等待、失败和取消不只依赖颜色区分。

Composer 的发送、取消、IME、Slash Command 和审批/问答行为见[交互使用](交互使用.md#web-工作台)。本轮视觉调整不修改 Protocol、Python AgentHost、RunCoordinator、InteractiveSnapshot 或 Web intent。
