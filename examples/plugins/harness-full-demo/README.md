# Harness Full Demo Plugin

这是一个可以直接安装的完整体验插件，同时包含 Agent Plugins 1.0 manifest 和 Claude Code
manifest。所有运行脚本只使用 Python 标准库，不需要额外安装 npm 或 pip 依赖。

## 包含的能力

| 能力 | Demo 内容 | 体验入口 |
| --- | --- | --- |
| Skill | `project-health` | `/skills` |
| Command | `health` | 在 `/` 菜单搜索 `health` |
| MCP | `plugin_inventory` | `/mcp` 或让 Agent 调用 |
| Agent | code reviewer、test reviewer、review lead | `/agents` |
| Policy | 三个 Agent 共用只读 Policy | Agent/Team 执行时生效 |
| Team | `demo-quality-team` | `/teams` |
| Hook | 阻止包含 `demo-forbidden` 的 Bash | 让 Agent 执行测试命令 |
| LSP | 为 `.demo` 文件提供 hover/definition | 让 Agent 调用 `lsp` |
| Monitor | 每 15 秒输出 Demo ready 状态 | 自动进入非可信 Monitor 上下文 |

## 一、准备模型配置

在仓库根目录执行。源码入口固定使用 Bun `1.2.19`：

```bash
cd /path/to/hareness-code
export HARNESS_API_KEY='你的模型 API Key'
npx --yes bun@1.2.19 run dev -- config show
```

确认输出中的默认模型 `available` 为 `true`。如果当前配置不是你的服务，先参考
`docs/user/examples/config.toml` 修改 `~/.harness/config.toml`。

仅启动 TUI 不需要密钥，但发送 Skill、Command、Agent 或 Team 任务需要可用模型。

## 二、校验 Plugin

校验不会写入 PluginStore：

```bash
npx --yes bun@1.2.19 run dev -- \
  plugins validate examples/plugins/harness-full-demo
```

预期结果：

- `format` 为 `hybrid`；
- `can_enable` 为 `true`；
- `diagnostics` 为空；
- agents、commands、hooks、lsp、mcp、monitors、policies、skills、teams 均存在；
- 每个组件的 `effective` 都为 `true`。

Hook、LSP、Monitor 显示 `adapted` 是正常结果，表示 Claude 格式已经转换为 Harness 当前支持的
受控运行子集。

## 三、安装并启用

安装：

```bash
npx --yes bun@1.2.19 run dev -- \
  plugins install examples/plugins/harness-full-demo
```

安装输出中复制两个字段：

```text
plugin.id
plugin.capability_fingerprint
```

安装后默认 `enabled: false`。使用刚复制的真实值启用：

```bash
npx --yes bun@1.2.19 run dev -- \
  plugins enable local-4ec36d4bf4835ff4/harness-full-demo \
  --capability-fingerprint bf5cd8f77fbfa7ee2aba60fb3f50de80acf88361f13161ef5a20d9c16123afb8
```

可再次检查：

```bash
npx --yes bun@1.2.19 run dev -- plugins inspect <plugin.id>
npx --yes bun@1.2.19 run dev -- plugins list
```

启用结果在下一次 Host 启动生效，因此完成启用后再启动 TUI：

```bash
export HARNESS_API_KEY='你的模型 API Key'
npx --yes bun@1.2.19 run dev
```

## 四、按顺序体验

### 1. Skill 和 Command

输入 `/skills`，应能搜索到 canonical ID 末尾为
`harness-full-demo/project-health` 的 Skill。

也可以输入 `/`，搜索 `health`，选择类似下面的动态命令：

```text
/plugin:<source-id>:harness-full-demo:health
```

示例参数：

```text
/plugin:<source-id>:harness-full-demo:health packages/agent
```

动态命令名中的 `<source-id>` 由安装来源生成，以 TUI 菜单显示的真实名称为准。

### 2. MCP

输入 `/mcp`，应看到来源为当前 Plugin、名称包含 `demo_tools` 的 stdio Server，状态为
`connected`，并加载一个名称末尾为 `plugin_inventory` 的工具。

可以直接向 Agent 输入：

```text
调用 Demo Plugin 的 plugin_inventory MCP 工具，列出插件包中的组件文件。
```

### 3. Agent

输入 `/agents`，应看到：

```text
demo-code-reviewer
demo-test-reviewer
demo-review-lead
```

也可以用自然语言触发受控委派：

```text
请让 demo-code-reviewer 只读检查 packages/agent/harness_agent/plugins，
给出三个带文件证据的发现。
```

这些 Agent 使用 `demo-readonly` Policy，不能写文件、执行 Shell 或访问网络。

### 4. 固定 Agent Team

输入：

```text
/teams
/teams show demo-quality-team
/teams run demo-quality-team 检查当前仓库的插件实现与测试覆盖
```

`run` 会返回 Team run ID。继续查询：

```text
/teams status <run-id>
```

Team 会并行运行 code reviewer 和 test reviewer，二者完成后再由 review lead 汇总。需要中止时：

```text
/teams cancel <run-id>
```

也可以体验根据 Agent 定义动态生成 Team：

```text
/teams generate demo-generated-team demo-review-lead demo-code-reviewer,demo-test-reviewer 2
/teams show demo-generated-team
/teams run demo-generated-team 对扩展机制做一次只读验收
```

### 5. Hook

先让 Agent 尝试一个安全命令；在默认审批模式下需要人工批准：

```text
请调用 execute 运行：printf 'hook-ok\n'
```

然后验证阻断：

```text
请调用 execute 运行：printf 'demo-forbidden\n'
```

第二条命令应被 Demo `PreToolUse` Hook 拒绝，并显示：

```text
Harness Full Demo Hook blocked demo-forbidden
```

### 6. LSP

仓库中已经包含 `.demo` 示例文件。输入：

```text
请调用 lsp 工具，对
examples/plugins/harness-full-demo/samples/example.demo
第 1 行第 1 列执行 hover。
```

结果应包含：

```text
Harness Full Demo LSP is active.
```

### 7. Monitor

Monitor 随 Host 启动，每 15 秒输出：

```text
harness-full-demo ready
```

该内容会以明确标记的“不可信 Plugin 数据”进入模型上下文。可以询问：

```text
当前上下文里是否存在 harness-full-demo 的 Monitor ready 数据？只回答有或没有。
```

Monitor 不是系统指令，Host 退出时会终止其整个进程组。

## 五、停用与卸载

停用：

```bash
npx --yes bun@1.2.19 run dev -- plugins disable local-4ec36d4bf4835ff4/harness-full-demo
```

停用、启用和删除都在下一次 Host 启动生效。

卸载并保留 Plugin data：

```bash
npx --yes bun@1.2.19 run dev -- plugins remove local-4ec36d4bf4835ff4/harness-full-demo
```

同时清理 Plugin data：

```bash
npx --yes bun@1.2.19 run dev -- \
  plugins remove local-4ec36d4bf4835ff4/harness-full-demo --purge-data
```

## 六、常见问题

### TUI 显示“模型未配置”

运行 `config show`，确认默认 Profile 的 `available`。最常见原因是
`HARNESS_API_KEY` 没有导出到启动 TUI 的同一个终端。

### 安装后看不到 Skill、Agent 或 Team

Plugin catalog 只在 Host 启动时读取。退出当前 TUI，完成 `enable` 后重新运行 `bun run dev`。

### MCP 或 Runtime 组件启动失败

确认命令行可执行 `python3 --version`。Demo 不依赖其他第三方包。

### 修改 Demo 后无法使用旧指纹启用

这是预期安全行为。重新安装，并从新安装输出复制新的 `capability_fingerprint`。
