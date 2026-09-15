# Harness Code（za38-cli）

Harness Code 是面向企业研发场景的 Coding Agent CLI。终端交互由 Bun/OpenTUI 提供，Agent 内核基于 Python、deepagents、LangChain 和 LangGraph，并通过 stdio JSON-RPC 通信。

> 当前处于开发态：请从源码运行。跨平台安装包以及 `curl`、PowerShell、CMD 安装器尚未交付，不能将其视为可用的生产安装方式。

## 开始使用

本地开发固定使用 Bun `1.2.19`、Python 3.11+ 和 `uv`。先注入企业 npm/Python 索引，再通过统一入口安装锁定依赖、应用 DeepAgents 补丁并执行零 Anthropic 门禁：

```bash
mkdir -p ~/.harness
export HARNESS_NPM_REGISTRY='内网 npm registry 地址'
export HARNESS_PYPI_INDEX='内网 Python simple index 地址'
bun run deps:install
cp docs/user/examples/config.toml ~/.harness/config.toml
export HARNESS_API_KEY='你的企业网关密钥'
bun run dev
```

首次在内网重新解析锁文件时使用 `bun run deps:resolve`；它会在显式内网源下重新生成 `bun.lock` 和 `packages/agent/uv.lock`，通过审查后，后续工作副本只使用 `bun run deps:install` 冻结安装。缺少内网源、工具链版本不符或锁文件仍指向公网时，安装会在联网前失败。

Windows x64 内网提前验证使用临时分支 `feat_hc_179_内网依赖源码化`：五个关键 npm 发布包（含完整发布入口与 `dist/**`）位于 `third_party/npm/` 并按 workspace 链接，`provenance.json` 记录 tarball integrity 与目录哈希；安装后还会校验实际 realpath，`deps:install`/`deps:resolve` 只允许 win32/x64，其他依赖仍从现有内网源安装。该例外不代表 canonical `master` 的永久发布策略，正式内网包可用并完成目标环境验收后应移除。

推荐通过 `api_key_env` 引用环境变量。若本机开发环境无法预先设置环境变量，可在权限为 `0600` 的 `~/.harness/config.toml` 模型 Profile 中设置 `api_key` 作为降级值；环境变量非空时始终优先。

可使用 `bun run dev -- --help` 查看当前 CLI 参数；无头运行示例：

```bash
bun run dev -- --non-interactive --message "解释当前目录的项目结构"
```

详细说明：

- [快速开始](docs/user/快速开始.md)
- [模型配置](docs/user/模型配置.md)
- [交互使用](docs/user/交互使用.md)
- [插件管理](docs/user/插件管理.md)
- [完整 Plugin Demo](examples/plugins/harness-full-demo/README.md)
- [安全与沙箱](docs/user/安全与沙箱.md)
- [故障排查](docs/user/故障排查.md)

终端界面使用企业源的 `@opentui/core` / `@opentui/react` `0.4.3`、React `19.2.6` 和 `react-reconciler` `0.33.0`。企业引入 OpenTUI Core 时须同时镜像其目标平台 native optional packages；项目不再携带本地 Core 源码或自行构建的 FFI artifact。

参与开发请从 [开发工作流](docs/developer/project/开发工作流.md) 开始；任务以 [任务看板](docs/developer/task/任务看板.md) 和任务源文件为准。
