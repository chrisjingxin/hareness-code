/**
 * Windows 控制台 VT 输入模式修复。
 *
 * conhost（传统 CMD/PowerShell 窗口）需要 ENABLE_VIRTUAL_TERMINAL_INPUT (0x0200)
 * 才会把鼠标滚轮事件翻译为 SGR 序列写入 stdin。OpenTUI 的 setupTerminal() 内部
 * 调用 stdin.setRawMode(true)，libuv 会通过 SetConsoleMode 重置控制台标志，
 * 导致 VT 输入模式丢失。
 *
 * 本模块通过 hook setRawMode + 定时轮询持续守护 VT 输入标志，
 * 参考 opencode 的 terminal-win32.ts 中 win32InstallCtrlCGuard 的守护模式。
 */
import { dlopen, ptr } from "bun:ffi"
import type { ReadStream } from "node:tty"

const STD_INPUT_HANDLE = -10
/** 让 conhost 将输入事件翻译为 VT 转义序列（含鼠标滚轮）。 */
const ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
/** 确保鼠标事件被报告到输入缓冲区。 */
const ENABLE_MOUSE_INPUT = 0x0010

const kernel = () =>
  dlopen("kernel32.dll", {
    GetStdHandle: { args: ["i32"], returns: "ptr" },
    GetConsoleMode: { args: ["ptr", "ptr"], returns: "i32" },
    SetConsoleMode: { args: ["ptr", "u32"], returns: "i32" },
  })

let k32: ReturnType<typeof kernel> | undefined

function load(): boolean {
  if (process.platform !== "win32") return false
  try {
    k32 ??= kernel()
    return true
  } catch {
    return false
  }
}

/** 读取当前控制台输入模式，失败返回 undefined。 */
function getMode(handle: unknown): number | undefined {
  const buf = new Uint32Array(1)
  if (k32!.symbols.GetConsoleMode(handle as never, ptr(buf)) === 0) return undefined
  return buf[0]
}

/** 确保 VT 输入和鼠标标志已设置，已设置时跳过系统调用。 */
function enforce(handle: unknown): void {
  const mode = getMode(handle)
  if (mode === undefined) return
  if ((mode & ENABLE_VIRTUAL_TERMINAL_INPUT) !== 0 && (mode & ENABLE_MOUSE_INPUT) !== 0) return
  k32!.symbols.SetConsoleMode(handle as never, mode | ENABLE_VIRTUAL_TERMINAL_INPUT | ENABLE_MOUSE_INPUT)
}

let unhook: (() => void) | undefined

/**
 * 安装 VT 输入模式守护：hook stdin.setRawMode 并在 100ms 轮询中持续保持标志。
 * 必须在 createCliRenderer() 之后调用（setupTerminal 会重置 console mode）。
 * 返回清理函数；非 Windows 平台返回 undefined。
 */
export function win32InstallVtInputGuard(): (() => void) | undefined {
  if (!process.stdin.isTTY) return undefined
  if (!load()) return undefined
  // 防止重复安装。
  if (unhook) return unhook

  const stdin = process.stdin as ReadStream
  const original = stdin.setRawMode
  const handle = k32!.symbols.GetStdHandle(STD_INPUT_HANDLE)

  // 立即执行一次，覆盖 setupTerminal 中 setRawMode 造成的重置。
  enforce(handle)
  // libuv 可能在下一个 tick 再次应用 console mode，双重保障。
  setImmediate(() => enforce(handle))

  // hook setRawMode：每次 raw mode 切换后重新设置 VT 输入标志。
  let wrapped: ReadStream["setRawMode"] | undefined
  if (typeof original === "function") {
    wrapped = (mode: boolean) => {
      const result = original.call(stdin, mode)
      enforce(handle)
      setImmediate(() => enforce(handle))
      return result
    }
    stdin.setRawMode = wrapped
  }

  // 低频轮询兜底：防止 Zig 原生层或其他路径绕过 JS 直接修改 console mode。
  const interval = setInterval(() => enforce(handle), 100)
  interval.unref()

  let done = false
  unhook = () => {
    if (done) return
    done = true
    clearInterval(interval)
    if (wrapped && stdin.setRawMode === wrapped) {
      stdin.setRawMode = original
    }
    unhook = undefined
  }
  return unhook
}
