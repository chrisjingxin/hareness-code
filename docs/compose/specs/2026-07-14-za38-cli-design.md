# za38-cli Design Spec

> Enterprise coding agent CLI built on deepagents (Python kernel) + opentui (TS/Node TUI), distributed as a single npm package.

## [S1] Problem

We need an enterprise-internal coding agent CLI (`za38-cli`) that:

1. Provides a Claude Code / OpenCode-like interactive coding experience in the terminal
2. Uses opentui (Zig-native TUI framework, TS bindings) for high-quality terminal UI
3. Uses deepagents + LangChain + LangGraph (Python) as the agent kernel
4. Embeds za38 framework-specific commands for rapid agent code generation
5. Distributes via `npm install -g za38-cli` with transparent Python dependency management — users must not perceive this is a Python project

## [S2] Architecture Overview

Three-layer architecture with JSON-RPC over stdio as the IPC mechanism:

```
┌─────────────────────────────────────────────────────────┐
│  Presentation Layer (Node + opentui + React)            │
│  - Terminal UI rendering, user input, streaming display  │
│  - Slash command UI, config management                   │
│  - IPC Client: JSON-RPC over stdin/stdout                │
├─────────────────── stdio ────────────────────────────────┤
│  IPC Layer (JSON-RPC 2.0, newline-delimited)            │
│  - Node → Python: initialize, query, cancel, respond    │
│  - Python → Node: stream/* notifications                 │
├─────────────────────────────────────────────────────────┤
│  Agent Kernel (Python + deepagents + LangGraph)          │
│  - Agent reasoning loop (LangGraph astream)              │
│  - Tool execution (filesystem, shell, za38 commands)     │
│  - za38 code generation / scaffolding / infra            │
│  - Checkpointing (SqliteSaver, local)                    │
│  - Company LLM gateway provider                          │
└─────────────────────────────────────────────────────────┘
```

**Key design principle: Thin Protocol, Smart Kernel.** The Python kernel owns all business logic (agent reasoning, tool execution, code generation). The Node frontend is a rendering layer that receives events and sends user input. This minimizes the IPC surface, keeps a single source of truth for business logic, and allows future frontend swaps (e.g., web UI) without kernel changes.

## [S3] Project Structure

Monorepo with three packages:

```
za38-cli/
├── package.json                        # Root: npm workspaces
├── packages/
│   ├── cli/                            # @za38/cli — npm published package
│   │   ├── src/
│   │   │   ├── index.ts                # CLI entry (#!/usr/bin/env node)
│   │   │   ├── tui/                    # React + opentui presentation
│   │   │   │   ├── App.tsx             # Root component
│   │   │   │   ├── components/         # MessageBlock, ToolCallCard, InputBox, StatusBar
│   │   │   │   └── hooks/             # useAgentStream, useKeyboard
│   │   │   ├── ipc/                    # JSON-RPC client
│   │   │   │   ├── client.ts           # stdio JSON-RPC client
│   │   │   │   └── protocol.ts         # Message type definitions
│   │   │   ├── commands/               # Slash command handlers
│   │   │   └── config/                 # Config management
│   │   ├── scripts/
│   │   │   └── postinstall.js          # uv bootstrap + venv creation
│   │   ├── package.json
│   │   └── tsconfig.json
│   ├── agent/                          # za38-agent — Python package
│   │   ├── za38_agent/
│   │   │   ├── __init__.py
│   │   │   ├── __main__.py             # Entry: stdio JSON-RPC server
│   │   │   ├── server.py               # JSON-RPC async server
│   │   │   ├── agent.py                # create_za38_agent() — deepagents wrapper
│   │   │   ├── tools/                  # za38 vertical domain tools
│   │   │   │   ├── __init__.py
│   │   │   │   ├── codegen.py          # Agent code generation
│   │   │   │   ├── scaffold.py         # Project scaffolding
│   │   │   │   ├── ui_components.py    # UI component generation
│   │   │   │   └── infra.py            # Infrastructure adaptation
│   │   │   ├── providers/              # LLM provider integration
│   │   │   │   ├── __init__.py
│   │   │   │   └── za38_gateway.py     # Company LLM gateway provider
│   │   │   ├── middleware/             # Custom LangChain middleware
│   │   │   │   ├── __init__.py
│   │   │   │   ├── auth.py             # Auth middleware (company)
│   │   │   │   └── logging.py          # Logging middleware (company)
│   │   │   └── prompts/                # System prompts, templates
│   │   │       ├── system_prompt.md
│   │   │       └── templates/          # Jinja2 code generation templates
│   │   │           ├── agent_template.py.j2
│   │   │           ├── tool_template.py.j2
│   │   │           ├── project_scaffold/
│   │   │           └── ui_components/
│   │   ├── pyproject.toml
│   │   └── tests/
│   │       ├── test_tools.py
│   │       ├── test_server.py
│   │       └── test_agent.py
│   ├── opentui-react/                 # Vendored @opentui/react (private in opentui monorepo)
│   │   ├── src/                        # Copied from opentui/packages/react/src/
│   │   └── package.json                # Named @za38/opentui-react, depends on @opentui/core
│   └── protocol/                       # Shared protocol definitions
│       ├── src/
│       │   ├── types.ts                # JSON-RPC message types (TS)
│       │   └── index.ts
│       └── package.json
├── scripts/
│   ├── install.sh                     # Unix shell installer (curl | bash)
│   ├── install.ps1                    # Windows PowerShell installer
│   ├── install.cmd                    # Windows CMD installer
│   └── dev/                           # Development helper scripts
├── docs/
│   └── compose/
│       └── specs/                      # This spec
└── README.md
```

## [S4] IPC Protocol

JSON-RPC 2.0 over stdin/stdout, newline-delimited (one JSON object per line).

### Node → Python (Requests)

| Method | Params | Response | Description |
|--------|--------|----------|-------------|
| `initialize` | `{client_info: {name, version}}` | `{server_info: {name, version}, capabilities: {...}}` | Handshake |
| `query` | `{message: string, thread_id?: string}` | `{thread_id: string, accepted: true}` | Start agent execution |
| `cancel` | `{thread_id: string}` | `{cancelled: true}` | Cancel current execution |
| `respond` | `{thread_id: string, decision: string}` | `{accepted: true}` | Respond to HITL interrupt |
| `shutdown` | `{}` | `{}` | Graceful shutdown |

### Python → Node (Notifications, no id)

| Method | Params | Description |
|--------|--------|-------------|
| `stream/text` | `{text: string, thread_id: string}` | LLM text token stream |
| `stream/tool_start` | `{tool_name: string, tool_id: string, args: object}` | Tool call started |
| `stream/tool_chunk` | `{tool_id: string, chunk: string}` | Tool argument streaming |
| `stream/tool_result` | `{tool_id: string, result: string, error?: string}` | Tool execution result |
| `stream/plan` | `{todos: [{content, status}]}` | Todo list update |
| `stream/done` | `{thread_id: string, usage: {input_tokens, output_tokens}}` | Agent turn complete |
| `stream/error` | `{message: string, code: string}` | Execution error |
| `stream/interrupt` | `{tool_id: string, tool_name: string, description: string}` | HITL interrupt request |
| `log` | `{level: string, message: string}` | Diagnostic log |

### Protocol Constraints

- **stdout** is exclusively for JSON-RPC protocol messages. Python debug/logging output goes to **stderr**.
- Node writes to Python's stdin, reads from Python's stdout.
- Long-running operations use streaming notifications (no response id). The `query` response only means "request accepted."
- Node-side watchdog: 120s timeout for no response → show timeout UI + cancel option.
- Messages are UTF-8 encoded, newline-delimited. No embedded newlines in JSON.

### Message Examples

```jsonl
{"jsonrpc":"2.0","method":"initialize","params":{"client_info":{"name":"za38-cli","version":"0.1.0"}},"id":1}
{"jsonrpc":"2.0","result":{"server_info":{"name":"za38-agent","version":"0.1.0"},"capabilities":{"streaming":true,"hitl":true}},"id":1}
{"jsonrpc":"2.0","method":"query","params":{"message":"帮我生成一个客服agent","thread_id":"abc-123"},"id":2}
{"jsonrpc":"2.0","result":{"thread_id":"abc-123","accepted":true},"id":2}
{"jsonrpc":"2.0","method":"stream/text","params":{"text":"好的","thread_id":"abc-123"}}
{"jsonrpc":"2.0","method":"stream/text","params":{"text":"，我来","thread_id":"abc-123"}}
{"jsonrpc":"2.0","method":"stream/tool_start","params":{"tool_name":"generate_agent","tool_id":"call_1","args":{"description":"客服agent"}}}
{"jsonrpc":"2.0","method":"stream/tool_result","params":{"tool_id":"call_1","result":"已生成 agent 代码..."}}
{"jsonrpc":"2.0","method":"stream/done","params":{"thread_id":"abc-123","usage":{"input_tokens":1500,"output_tokens":800}}}
```

## [S5] Python Agent Kernel

### Agent Creation

Wraps deepagents' `create_deep_agent()`:

```python
# za38_agent/agent.py
from deepagents import create_deep_agent, DeepAgentState
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from za38_agent.tools import (
    generate_agent_tool, generate_tool_tool,
    init_project_tool, add_tool_tool,
    generate_component_tool,
    configure_auth_tool, configure_gateway_tool, configure_logging_tool,
)
from za38_agent.middleware import Za38AuthMiddleware, Za38LoggingMiddleware
from za38_agent.providers import resolve_model

def create_za38_agent(config: Za38Config) -> CompiledStateGraph:
    model = resolve_model(config.model)  # "za38:default" or "openai:gpt-4"

    tools = [
        generate_agent_tool,
        generate_tool_tool,
        init_project_tool,
        add_tool_tool,
        generate_component_tool,
        configure_auth_tool,
        configure_gateway_tool,
        configure_logging_tool,
    ]

    middleware = [
        Za38AuthMiddleware(config.auth),
        Za38LoggingMiddleware(config.logging),
    ]

    return create_deep_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=ZA38_SYSTEM_PROMPT,
        checkpointer=AsyncSqliteSaver.from_conn_string(config.db_path),
    )
```

### JSON-RPC Server

Async server reading from stdin, writing to stdout:

```python
# za38_agent/server.py
import asyncio, json, sys

class JsonRpcServer:
    def __init__(self):
        self.agent = None
        self.handlers = {
            "initialize": self.handle_initialize,
            "query": self.handle_query,
            "cancel": self.handle_cancel,
            "respond": self.handle_respond,
            "shutdown": self.handle_shutdown,
        }

    async def run(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(
            lambda: protocol, sys.stdin
        )
        async for line in reader:
            msg = json.loads(line.decode())
            await self.dispatch(msg)

    async def send(self, msg: dict):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    async def send_notification(self, method: str, params: dict):
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def handle_query(self, params):
        thread_id = params.get("thread_id", str(uuid.uuid4()))
        await self.send_response(params["_id"], {"thread_id": thread_id, "accepted": True})

        async for event in self.agent.astream(
            {"messages": [HumanMessage(content=params["message"])]},
            config={"configurable": {"thread_id": thread_id}},
            stream_mode=["messages", "updates"],
            subgraphs=True,
        ):
            notification = self.translate_event(event, thread_id)
            if notification:
                await self.send_notification(notification["method"], notification["params"])

        await self.send_notification("stream/done", {"thread_id": thread_id, "usage": {}})

    def translate_event(self, event, thread_id) -> dict | None:
        namespace, stream_mode, data = event
        if stream_mode == "messages":
            chunk, metadata = data
            if isinstance(chunk, AIMessageChunk):
                if chunk.content:
                    return {"method": "stream/text", "params": {"text": chunk.content, "thread_id": thread_id}}
                if chunk.tool_call_chunks:
                    tc = chunk.tool_call_chunks[0]
                    return {"method": "stream/tool_chunk", "params": {"tool_id": tc["id"], "chunk": tc["args"]}}
            elif isinstance(chunk, ToolMessage):
                return {"method": "stream/tool_result", "params": {"tool_id": tc_id, "result": chunk.content}}
        elif stream_mode == "updates":
            if "__interrupt__" in data:
                return {"method": "stream/interrupt", "params": {...}}
        return None
```

### LLM Provider

Company internal gateway as a LangChain `BaseChatModel` subclass:

```python
# za38_agent/providers/za38_gateway.py
from langchain_core.language_models import BaseChatModel

class Za38GatewayModel(BaseChatModel):
    """Routes to company internal LLM gateway via za38 framework."""
    gateway_url: str
    auth_token: str = None  # Injected by Za38AuthMiddleware

    def _generate(self, messages, stop=None, **kwargs):
        # Call company gateway API
        response = httpx.post(
            f"{self.gateway_url}/chat/completions",
            json=self._format_messages(messages),
            headers={"Authorization": f"Bearer {self.auth_token}"},
        )
        return self._parse_response(response)

    async def _agenerate(self, messages, stop=None, **kwargs):
        # Async version with streaming support
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", ...) as resp:
                async for chunk in resp.aiter_lines():
                    yield self._parse_chunk(chunk)
```

### Middleware

```python
# za38_agent/middleware/auth.py
from langchain.agents.middleware import AgentMiddleware

class Za38AuthMiddleware(AgentMiddleware):
    """Injects company auth token into model calls."""
    def __init__(self, config: AuthConfig):
        self.config = config

    def wrap_model_call(self, request):
        # Inject auth headers/tokens before LLM call
        request.metadata["auth_token"] = self.config.get_token()
        return request
```

## [S6] Node Presentation Layer

### Component Tree

```tsx
// src/tui/App.tsx
import { createRoot } from "@za38/opentui-react"

function App({ ipcClient }: { ipcClient: IpcClient }) {
  const { messages, status, usage, sendMessage } = useAgentStream(ipcClient)

  return (
    <box flexDirection="column" height="100%" width="100%">
      <StatusBar status={status} usage={usage} />
      <scrollbox flexGrow={1} overflow="scroll">
        {messages.map(msg => (
          <MessageBlock key={msg.id} message={msg} />
        ))}
        {status === "thinking" && <ThinkingIndicator />}
      </scrollbox>
      <InputBox onSubmit={sendMessage} disabled={status === "thinking"} />
    </box>
  )
}
```

### Core Hook: useAgentStream

```typescript
// src/tui/hooks/useAgentStream.ts
function useAgentStream(client: IpcClient) {
  const [messages, dispatch] = useReducer(messageReducer, [])
  const [status, setStatus] = useState<"idle" | "thinking" | "error">("idle")
  const [usage, setUsage] = useState({ input_tokens: 0, output_tokens: 0 })

  useEffect(() => {
    const handlers = {
      "stream/text": (p) => dispatch({ type: "APPEND_TEXT", text: p.text }),
      "stream/tool_start": (p) => dispatch({ type: "TOOL_START", ...p }),
      "stream/tool_result": (p) => dispatch({ type: "TOOL_RESULT", ...p }),
      "stream/plan": (p) => dispatch({ type: "PLAN_UPDATE", todos: p.todos }),
      "stream/done": (p) => { setStatus("idle"); setUsage(p.usage) },
      "stream/error": (p) => { setStatus("error"); dispatch({ type: "ERROR", ...p }) },
      "stream/interrupt": (p) => dispatch({ type: "INTERRUPT", ...p }),
    }
    Object.entries(handlers).forEach(([method, handler]) => client.on(method, handler))
    return () => Object.entries(handlers).forEach(([method, handler]) => client.off(method, handler))
  }, [client])

  const sendMessage = useCallback((text: string) => {
    setStatus("thinking")
    dispatch({ type: "USER_MESSAGE", text })
    client.query(text)
  }, [client])

  return { messages, status, usage, sendMessage }
}
```

### Message Reducer

```typescript
// src/tui/hooks/messageReducer.ts
type Message = UserMessage | AIMessage | ToolCallMessage | ErrorMessage

function messageReducer(state: Message[], action: Action): Message[] {
  switch (action.type) {
    case "USER_MESSAGE":
      return [...state, { id: nanoid(), role: "user", content: action.text }]
    case "APPEND_TEXT":
      // Append token to last AI message, or create new one
      const lastMsg = state[state.length - 1]
      if (lastMsg?.role === "ai" && lastMsg.streaming) {
        return [...state.slice(0, -1), { ...lastMsg, content: lastMsg.content + action.text }]
      }
      return [...state, { id: nanoid(), role: "ai", content: action.text, streaming: true }]
    case "TOOL_START":
      return [...state, { id: action.tool_id, role: "tool", name: action.tool_name, args: action.args, status: "running" }]
    case "TOOL_RESULT":
      return state.map(m => m.id === action.tool_id ? { ...m, result: action.result, status: "done" } : m)
    case "PLAN_UPDATE":
      // Update todo list (rendered in a sidebar/panel)
      return state
    case "ERROR":
      return [...state, { id: nanoid(), role: "error", content: action.message }]
    default:
      return state
  }
}
```

### IPC Client

```typescript
// src/ipc/client.ts
import { EventEmitter } from "events"
import { ChildProcess } from "child_process"

class IpcClient extends EventEmitter {
  private nextId = 1
  private pending = new Map<number, {resolve, reject}>()

  constructor(private stdin: Writable, private stdout: Readable) {
    super()
    this.stdout.on("data", this.onData)
  }

  private buffer = ""
  private onData = (chunk: Buffer) => {
    this.buffer += chunk.toString()
    const lines = this.buffer.split("\n")
    this.buffer = lines.pop() ?? ""
    for (const line of lines) {
      if (!line.trim()) continue
      const msg = JSON.parse(line)
      if (msg.method) {
        // Notification
        this.emit(msg.method, msg.params)
      } else if (msg.id && this.pending.has(msg.id)) {
        // Response
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(msg.error)
        else resolve(msg.result)
      }
    }
  }

  async call(method: string, params: object = {}): Promise<any> {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.send({ jsonrpc: "2.0", method, params, id })
    })
  }

  query(message: string, threadId?: string) {
    return this.call("query", { message, thread_id: threadId })
  }

  private send(msg: object) {
    this.stdin.write(JSON.stringify(msg) + "\n")
  }
}
```

## [S7] Distribution & Packaging

### Primary Install: Shell Installer (curl | bash)

The primary distribution is a shell installer, similar to Claude Code's approach. Users do not need Node/npm pre-installed.

```bash
# macOS / Linux
curl -fsSL https://za38.internal/install.sh | bash

# Windows PowerShell
irm https://za38.internal/install.ps1 | iex

# Windows CMD
curl -fsSL https://za38.internal/install.cmd -o install.cmd && install.cmd && del install.cmd
```

The installer script (`scripts/install.sh` / `scripts/install.ps1`) performs:
1. Detect platform (darwin/linux/win32) and arch (arm64/x64)
2. Check if Bun is installed; if not, download and install Bun runtime
3. `bun install -g @za38/cli` — installs the npm package (triggers postinstall for Python setup)
4. Verify `za38` is on PATH; if not, add `~/.bun/bin` to shell profile
5. Print success message with next steps

This means the npm package + postinstall is the underlying mechanism, but the user-facing entry point is the shell installer. Advanced users can still `npm install -g @za38/cli` directly if they already have Node/Bun.

### npm Package Configuration (underlying mechanism)

> **Dependency note**: `@opentui/react` and `@opentui/solid` are **private workspace packages** in the opentui monorepo — not published to npm. Only `@opentui/core` (and a few others) are published. We **vendor** `@opentui/react` into our monorepo as a workspace package (`packages/opentui-react/`), copied from the local opentui source at `/Users/zhangjingxin/Code/OpenSource/opentui/packages/react/`. Its imports to `@opentui/core` resolve to the npm-published version. This gives us full control over the React reconciler while using the published native core.

```json
// packages/cli/package.json
{
  "name": "@za38/cli",
  "version": "0.1.0",
  "bin": {
    "za38": "./dist/index.js"
  },
  "scripts": {
    "postinstall": "node scripts/postinstall.js",
    "build": "bun build src/index.ts --outdir dist --target node"
  },
  "dependencies": {
    "@opentui/core": "^0.4.3",
    "@za38/opentui-react": "workspace:*",
    "@za38/protocol": "workspace:*"
  },
  "optionalDependencies": {
    "@opentui/core-darwin-arm64": "^0.4.3",
    "@opentui/core-darwin-x64": "^0.4.3",
    "@opentui/core-linux-arm64": "^0.4.3",
    "@opentui/core-linux-x64": "^0.4.3"
  }
}
```

The project structure adds `packages/opentui-react/` (vendored from opentui source).

### postinstall Script

```javascript
// scripts/postinstall.js
const { downloadUv, execUv, getDataDir } = require("./setup/utils")
const path = require("path")
const fs = require("fs/promises")

async function main() {
  const dataDir = getDataDir()  // ~/.za38/ or platform equivalent
  const venvPath = path.join(dataDir, "venv")

  // Skip if venv already exists and is valid
  try {
    await fs.access(path.join(venvPath, "pyvenv.cfg"))
    console.log("za38-cli: venv already exists, skipping setup")
    return
  } catch {}

  // 1. Download uv binary (single static binary, platform-specific)
  console.log("za38-cli: downloading uv...")
  const uvPath = await downloadUv(process.platform, process.arch)

  // 2. Install Python 3.12 via uv (downloads python-build-standalone)
  console.log("za38-cli: installing Python 3.12...")
  await execUv(uvPath, ["python", "install", "3.12"])

  // 3. Create virtual environment
  console.log("za38-cli: creating virtual environment...")
  await execUv(uvPath, ["venv", venvPath, "--python", "3.12"])

  // 4. Install za38-agent Python package
  console.log("za38-cli: installing agent dependencies...")
  await execUv(uvPath, ["pip", "install", "--venv", venvPath, "za38-agent"])

  console.log("za38-cli: setup complete!")
}

main().catch(err => {
  console.error("za38-cli: setup failed:", err.message)
  process.exit(1)
})
```

### CLI Entry Point

```typescript
// src/index.ts
#!/usr/bin/env node
import { spawn } from "child_process"
import { resolveVenvPython } from "./setup/paths"
import { IpcClient } from "./ipc/client"
import { createCliRenderer } from "@opentui/core"
import { createRoot } from "@za38/opentui-react"
import { App } from "./tui/App"

async function main() {
  const pythonPath = resolveVenvPython()
  if (!pythonPath) {
    console.error("za38-cli: Python environment not found. Run npm rebuild @za38/cli.")
    process.exit(1)
  }

  // Spawn Python agent kernel as child process
  const child = spawn(pythonPath, ["-m", "za38_agent"], {
    stdio: ["pipe", "pipe", "inherit"],  // stdin=pipe, stdout=pipe, stderr=inherit
  })

  child.on("exit", (code) => {
    if (code !== 0) console.error(`za38-cli: agent process exited with code ${code}`)
    process.exit(code ?? 1)
  })

  // Create IPC client
  const ipcClient = new IpcClient(child.stdin, child.stdout)
  await ipcClient.call("initialize", { client_info: { name: "za38-cli", version: "0.1.0" } })

  // Start opentui renderer + React app
  const renderer = await createCliRenderer()
  const root = createRoot(renderer)
  root.render(<App ipcClient={ipcClient} />)
}

main()
```

## [S8] za38 Vertical Domain Commands

> **Status: Deferred.** za38 vertical commands (codegen, scaffold, ui_components, infra) will be integrated in a later phase. The MVP focuses on core coding agent functionality (filesystem/shell/todo tools via deepagents), mirroring dcode's capabilities. The architecture is designed to allow za38 tools to be added later as additional `@tool` functions in the `tools/` package without structural changes.

### Code Generation (codegen.py)

```python
@tool
def generate_agent(description: str, tools: list[str] = None, state_schema: str = None) -> str:
    """Generate a complete za38 agent with graph definition, tools, state, and system prompt.

    Args:
        description: What the agent should do (e.g., "customer service agent for handling refunds")
        tools: List of tool names to include (e.g., ["web_search", "file_read"])
        state_schema: Custom state schema description (optional)
    """
    template = env.get_template("agent_template.py.j2")
    code = template.render(description=description, tools=tools or [], state_schema=state_schema)
    return f"Generated agent code:\n```python\n{code}\n```"

@tool
def generate_tool(name: str, description: str, parameters: str) -> str:
    """Generate a single LangChain tool function."""
    template = env.get_template("tool_template.py.j2")
    code = template.render(name=name, description=description, parameters=parameters)
    return f"Generated tool code:\n```python\n{code}\n```"
```

### Project Scaffolding (scaffold.py)

```python
@tool
def init_project(name: str, template: str = "basic") -> str:
    """Initialize a new za38 agent project with directory structure, config, and example files.

    Args:
        name: Project name
        template: Template type ("basic", "customer_service", "code_review")
    """
    project_dir = create_project_structure(name, template)
    return f"Project initialized at {project_dir}"

@tool
def add_tool(project_path: str, tool_name: str, tool_description: str) -> str:
    """Add a new tool to an existing za38 agent project."""
    # Generate tool file, update imports, update agent config
    return f"Tool '{tool_name}' added to {project_path}"
```

### UI Component Generation (ui_components.py)

```python
@tool
def generate_component(component_type: str, props: dict = None) -> str:
    """Generate za38 UI component code (dialog, chat interface, etc.).

    Args:
        component_type: Component type ("dialog", "chat", "form", "status_bar")
        props: Component properties
    """
    template = env.get_template(f"ui_components/{component_type}.j2")
    code = template.render(props=props or {})
    return f"Generated {component_type} component:\n```python\n{code}\n```"
```

### Infrastructure Adaptation (infra.py)

```python
@tool
def configure_auth(project_path: str, auth_type: str = "token") -> str:
    """Generate auth configuration for company infrastructure."""
    config = generate_auth_config(auth_type)
    return f"Auth configuration generated for {project_path}"

@tool
def configure_gateway(project_path: str, gateway_url: str) -> str:
    """Configure LLM gateway endpoint."""
    config = generate_gateway_config(gateway_url)
    return f"Gateway configuration generated for {project_path}"

@tool
def configure_logging(project_path: str, log_level: str = "INFO") -> str:
    """Configure company logging system integration."""
    config = generate_logging_config(log_level)
    return f"Logging configuration generated for {project_path}"
```

### Templates

Jinja2 templates stored in `za38_agent/prompts/templates/`:
- `agent_template.py.j2` — Full agent code template
- `tool_template.py.j2` — Single tool function template
- `project_scaffold/` — Project structure templates
- `ui_components/` — UI component templates (dialog, chat, form, etc.)

Templates are designed to be extensible — when the user provides specific za38 framework details, templates can be updated without changing tool logic.

## [S9] Error Handling & Process Lifecycle

### Process Lifecycle

```
npm install -g @za38/cli
  └─ postinstall: download uv → install Python → create venv → install za38-agent

za38 (CLI invocation)
  ├─ Node entry: resolve venv python → spawn `python -m za38_agent`
  ├─ Python: start JSON-RPC server on stdin/stdout
  ├─ Node: initialize handshake → start opentui renderer → render React app
  ├─ User interaction: query → stream events → done → next query
  └─ Exit (Ctrl+C / /exit):
       ├─ Node: send shutdown notification
       ├─ Python: cleanup (close checkpointer, flush logs)
       └─ Both processes exit
```

### Error Scenarios

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| Python process crash | Node detects child exit code ≠ 0 | Show error message + offer restart |
| IPC timeout | Node watchdog (120s no response) | Show timeout UI + cancel option |
| Python stderr output | Node inherits stderr → visible in terminal | User can see Python errors directly |
| Venv missing on startup | Node checks venv path exists | Prompt user to run `npm rebuild` |
| uv download failure | postinstall catches network error | Show error + retry instructions |
| LLM gateway unreachable | Python sends `stream/error` notification | Node shows error + retry option |

### Graceful Shutdown

```typescript
// Node side
process.on("SIGINT", async () => {
  await ipcClient.call("shutdown")
  child.kill("SIGTERM")
  setTimeout(() => process.exit(0), 1000)
})
```

```python
# Python side
async def handle_shutdown(self, params):
    if self.agent:
        # Close checkpointer, flush logs
        await self.cleanup()
    await self.send_response(params["_id"], {})
    sys.exit(0)
```

## [S10] Testing Strategy

### Python Tests (pytest)

```python
# tests/test_tools.py
class TestCodegenTools:
    def test_generate_agent_basic(self):
        result = generate_agent.invoke({"description": "a simple agent"})
        assert "create_deep_agent" in result
        assert "graph" in result

    def test_generate_agent_with_tools(self):
        result = generate_agent.invoke({"description": "agent", "tools": ["web_search"]})
        assert "web_search" in result

# tests/test_server.py
class TestJsonRpcServer:
    async def test_initialize(self):
        response = await server.handle_request({"method": "initialize", ...})
        assert response["result"]["server_info"]["name"] == "za38-agent"

    async def test_query_streams_text(self):
        notifications = []
        server.send_notification = lambda m, p: notifications.append((m, p))
        await server.handle_query({"message": "hello"})
        assert any(n[0] == "stream/text" for n in notifications)
        assert any(n[0] == "stream/done" for n in notifications)
```

### Node Tests (bun test)

```typescript
// tests/messageReducer.test.ts
import { messageReducer } from "../src/tui/hooks/messageReducer"

test("APPEND_TEXT creates new AI message", () => {
  const state = messageReducer([], { type: "APPEND_TEXT", text: "Hello" })
  expect(state).toHaveLength(1)
  expect(state[0].role).toBe("ai")
  expect(state[0].content).toBe("Hello")
})

test("APPEND_TEXT appends to existing AI message", () => {
  let state = messageReducer([], { type: "APPEND_TEXT", text: "Hello" })
  state = messageReducer(state, { type: "APPEND_TEXT", text: " world" })
  expect(state).toHaveLength(1)
  expect(state[0].content).toBe("Hello world")
})
```

### Integration Tests

```python
# tests/test_integration.py
async def test_full_stack():
    """Spawn Python server, send query via JSON-RPC, verify stream events."""
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "za38_agent",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    # Send initialize
    await send_jsonrpc(proc.stdin, {"method": "initialize", "id": 1})
    # Send query
    await send_jsonrpc(proc.stdin, {"method": "query", "params": {"message": "hello"}, "id": 2})
    # Collect notifications
    events = await collect_notifications(proc.stdout)
    assert any(e["method"] == "stream/done" for e in events)
```

## [S11] MVP Scope & Iteration Plan

### MVP (v0.1.0) — First Runnable Version

1. **Project scaffolding**: Monorepo structure, build tooling, TypeScript/Python config
2. **IPC protocol**: `initialize` + `query` + `stream/text` + `stream/done` working end-to-end
3. **Python agent kernel**: deepagents integration with basic system prompt + one test tool
4. **Node TUI**: Basic chat interface (input box + message list + streaming text display)
5. **`generate_agent` tool**: Single za38 code generation tool working
6. **npm packaging**: postinstall script that creates venv via uv

### v0.2.0 — Extended za38 Commands

- Complete code generation tools (`generate_tool`)
- Project scaffolding (`init_project`, `add_tool`)
- UI component generation (`generate_component`)
- Infrastructure configuration tools

### v0.3.0 — Enterprise Integration

- Company LLM gateway provider implementation (with actual auth/gateway/logging details)
- Auth/logging middleware implementation
- HITL interrupt interaction in TUI
- Configuration system (`.za38/config.toml`)
- Subagent support

### v0.4.0 — Polish

- Slash commands (`/help`, `/model`, `/clear`, `/history`)
- Token usage display
- Session persistence and history
- Error recovery UI
- Performance optimization (lazy imports, connection pooling)
