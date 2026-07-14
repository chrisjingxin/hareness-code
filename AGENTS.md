# Repository Guidelines

## Project Structure & Module Organization

- `packages/cli/`: `@za38/cli` TypeScript entrypoint, OpenTUI presentation code, and IPC client tests.
- `packages/protocol/`: shared TypeScript JSON-RPC method names and payload types.
- `packages/agent/`: `za38-agent` Python package. `za38_agent/server.py` owns stdio JSON-RPC; `agent.py` builds the deepagents graph; `providers/` contains model adapters.
- `packages/*/tests/`: package-local tests. Python tests live in `packages/agent/tests/`.
- `docs/compose/`: design history. `.agent/` holds the current implementation plan.

Keep presentation logic in `cli`, Agent/business logic in `agent`, and cross-process contracts in `protocol`.

## Build, Test, and Development Commands

- `bun run dev`: run the CLI workspace entrypoint during development.
- `bun run build`: build every Bun workspace package.
- `bun run typecheck`: type-check the OpenTUI/TypeScript source.
- `bun run test`: run workspace test scripts.
- `cd packages/cli && bun test`: run TypeScript IPC/TUI tests.
- `cd packages/agent && .venv/bin/python -m pytest -q`: run Python tests using the project virtual environment.

## Coding Style & Naming Conventions

Use TypeScript ESM with 2-space indentation, `camelCase` values/functions, and `PascalCase` classes/types. Keep protocol names as stable string constants such as `stream/text` and `stream/done`.

Use Python 4-space indentation, `snake_case` modules/functions, `PascalCase` classes, type hints on APIs, and concise docstrings. Add necessary comments in Chinese; explain intent, safety constraints, or non-obvious behavior rather than restating code. Keep stdout in the Python server exclusively for newline-delimited JSON-RPC; send diagnostics to stderr or structured log events.

No formatter or linter is configured yet. Match surrounding code and avoid unrelated formatting churn.

## Testing Guidelines

Name tests `test_<behavior>` in Python and `*.test.ts` in Bun. Cover both sides of an IPC change: Python dispatch/stream behavior and TypeScript frame handling. Prefer mock models or mock HTTP servers; never require real model credentials in tests. Add regression coverage for cancellation, interrupt/resume, malformed frames, and terminal error events when changing server lifecycle code.

## Commit & Pull Request Guidelines

Follow the existing Conventional Commit style, for example `feat: Node IPC 客户端，JSON-RPC over stdio` or `fix: handle cancelled agent run`. Keep commits focused by package.

PRs should state the affected layer, behavior change, tests run, and any configuration or protocol impact. Include terminal screenshots for OpenTUI changes and call out any new environment variables or filesystem permissions.

## Security & Configuration

Never commit API keys or gateway credentials. Configure model secrets through environment variables referenced by TOML configuration. Treat workspace paths, MCP configuration, shell commands, and streamed tool output as untrusted input.
