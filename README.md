# Harness Code（za38-cli）

Harness Code 是面向企业研发场景的 Coding Agent CLI。终端交互由 Bun/OpenTUI 提供，Agent 内核基于 Python、deepagents、LangChain 和 LangGraph，并通过 stdio JSON-RPC 通信。

> 当前处于开发态：请从源码运行。跨平台安装包以及 `curl`、PowerShell、CMD 安装器尚未交付，不能将其视为可用的生产安装方式。

## 开始使用

本地开发需要 Bun、Python 3.11+ 及 `packages/agent/.venv` 中的 Agent 依赖。复制示例配置后，将 API Key 放入指定环境变量：

```bash
cp .harness/config.toml.example .harness/config.toml
export HARNESS_API_KEY='你的企业网关密钥'
bun run dev -- --config .harness/config.toml
```

可使用 `bun run dev -- --help` 查看当前 CLI 参数；无头运行示例：

```bash
bun run dev -- --non-interactive --message "解释当前目录的项目结构"
```

详细说明：

- [快速开始](docs/user/快速开始.md)
- [模型配置](docs/user/模型配置.md)
- [交互使用](docs/user/交互使用.md)
- [安全与沙箱](docs/user/安全与沙箱.md)
- [故障排查](docs/user/故障排查.md)

参与开发请从 [开发工作流](docs/developer/开发工作流.md) 开始；任务以 [任务看板](docs/developer/任务看板.md) 和任务源文件为准。
