# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: smoke.spec.ts >> 命令菜单与命令执行：/help 显示帮助面板文案
- Location: tests/e2e/smoke.spec.ts:43:1

# Error details

```
Error: E2E：等待 /web URL 超时（/var/folders/9m/14r3v7xd2llf3sbqh9r622yr0000gn/T/za38-e2e-cli-fYn27c/url.txt）
```

# Test source

```ts
  17  |   /** 等待 opener 把新的 /web URL 写入文件（内容不同于 previous），带超时。 */
  18  |   waitForNewUrl(previous: string, timeoutMs?: number): Promise<string>
  19  |   /** 等待并返回 TUI 输出中是否出现子串（轮询，带超时）。 */
  20  |   waitForOutput(substring: string, timeoutMs?: number): Promise<boolean>
  21  |   /** 终止 CLI（幂等）。 */
  22  |   stop(): Promise<void>
  23  |   /** CLI 是否已退出。 */
  24  |   exited: Promise<number | null>
  25  | }
  26  | 
  27  | const e2eDir = fileURLToPath(new URL(".", import.meta.url))
  28  | const cliDir = resolve(e2eDir, "../..")
  29  | const repoRoot = resolve(cliDir, "../../..")
  30  | const ptyBridge = resolve(repoRoot, "scripts/e2e-pty.py")
  31  | const agentDir = resolve(repoRoot, "packages/agent")
  32  | const agentPython = resolve(agentDir, ".venv/bin/python")
  33  | 
  34  | /** 启动一个独立的交互式 CLI（echo Agent + URL 文件 opener），自动执行 /web 并返回首个 URL。 */
  35  | export async function startCli(extraEnv: Record<string, string> = {}): Promise<CliHarness> {
  36  |   const workDir = await mkdtemp(join(tmpdir(), "za38-e2e-cli-"))
  37  |   const urlFile = join(workDir, "url.txt")
  38  |   const home = join(workDir, "home")
  39  | 
  40  |   const child = spawn(
  41  |     agentPython,
  42  |     [ptyBridge, "--", process.execPath, "run", "src/index.ts"],
  43  |     {
  44  |       cwd: cliDir,
  45  |       env: {
  46  |         ...process.env,
  47  |         HOME: home,
  48  |         HARNESS_ECHO_MODE: "1",
  49  |         HARNESS_AGENT_PYTHON: agentPython,
  50  |         PYTHONPATH: agentDir,
  51  |         HARNESS_E2E_WEB_URL_FILE: urlFile,
  52  |         ...extraEnv,
  53  |       },
  54  |       stdio: ["pipe", "pipe", "pipe"],
  55  |     },
  56  |   )
  57  |   let output = ""
  58  |   const outputBuffer: string[] = []
  59  |   child.stdout?.on("data", chunk => {
  60  |     output += chunk.toString("utf8")
  61  |     outputBuffer.push(chunk.toString("utf8"))
  62  |   })
  63  |   child.stderr?.on("data", chunk => {
  64  |     outputBuffer.push(`[stderr] ${chunk.toString("utf8")}`)
  65  |   })
  66  | 
  67  |   // TUI 渲染就绪后自动执行 /web；URL 由测试专用 opener 写入文件。
  68  |   await new Promise(resolveTimer => setTimeout(resolveTimer, 5_000))
  69  |   child.stdin?.write("/web\r")
  70  |   const url = await waitForUrlFile(urlFile)
  71  |   return {
  72  |     url,
  73  |     writeInput(text: string) {
  74  |       child.stdin?.write(`${text}\r`)
  75  |     },
  76  |     async waitForNewUrl(previous: string, timeoutMs = 20_000) {
  77  |       const deadline = Date.now() + timeoutMs
  78  |       while (Date.now() < deadline) {
  79  |         try {
  80  |           const content = (await readFile(urlFile, "utf8")).trim()
  81  |           if (content && content !== previous) return content
  82  |         } catch {
  83  |           // 尚未写入。
  84  |         }
  85  |         await new Promise(resolveTimer => setTimeout(resolveTimer, 250))
  86  |       }
  87  |       throw new Error(`E2E：等待新的 /web URL 超时`)
  88  |     },
  89  |     async waitForOutput(substring: string, timeoutMs = 15_000) {
  90  |       const deadline = Date.now() + timeoutMs
  91  |       while (Date.now() < deadline) {
  92  |         if (output.includes(substring)) return true
  93  |         await new Promise(resolveTimer => setTimeout(resolveTimer, 100))
  94  |       }
  95  |       return output.includes(substring)
  96  |     },
  97  |     async stop() {
  98  |       if (child.exitCode === null) child.kill()
  99  |       await new Promise<void>(resolveTimer => child.once("exit", () => resolveTimer()))
  100 |       await rm(workDir, { recursive: true, force: true })
  101 |     },
  102 |     exited: new Promise<number | null>(resolveTimer => child.once("exit", code => resolveTimer(code))),
  103 |   }
  104 | }
  105 | 
  106 | async function waitForUrlFile(urlFile: string, timeoutMs = 30_000): Promise<string> {
  107 |   const deadline = Date.now() + timeoutMs
  108 |   while (Date.now() < deadline) {
  109 |     try {
  110 |       const content = (await readFile(urlFile, "utf8")).trim()
  111 |       if (content) return content
  112 |     } catch {
  113 |       // 文件尚未写入。
  114 |     }
  115 |     await new Promise(resolveTimer => setTimeout(resolveTimer, 250))
  116 |   }
> 117 |   throw new Error(`E2E：等待 /web URL 超时（${urlFile}）`)
      |         ^ Error: E2E：等待 /web URL 超时（/var/folders/9m/14r3v7xd2llf3sbqh9r622yr0000gn/T/za38-e2e-cli-fYn27c/url.txt）
  118 | }
  119 | 
  120 | /** 把页面 URL 转为 /ui WebSocket URL（含 UI token）。 */
  121 | export function uiSocketUrl(pageUrl: string): string {
  122 |   const url = new URL(pageUrl)
  123 |   const token = url.hash.replace(/^#ui=/, "")
  124 |   return `ws://${url.host}${url.pathname}/ui?ui=${encodeURIComponent(token)}`
  125 | }
  126 | 
```