#!/usr/bin/env python3
"""E2E PTY 桥：以伪终端运行命令，把桥进程的 stdin 转发到 pty、pty 输出转发到 stdout。

Playwright 测试需要真实 TTY 才能启动交互式 CLI（validateInteractiveTerminal），
`script` 在 stdin 为管道时在 macOS 上会挂起输出；本桥用 stdlib pty 提供确定行为。
用法：python3 e2e-pty.py -- <command> [args...]

macOS 注意事项（本机实测）：
- pty.fork() 默认窗口尺寸为 0x0，OpenTUI 等 TUI 无法渲染，必须在 fork 后设置
  TIOCSWINSZ（此处固定 120x40）。
- 主线程阻塞 read(pty master) 会被同进程其他线程的阻塞 read/select 饿死（数据
  到达也不返回）；必须在单线程里 select stdin 与 pty 两个 fd 再读取。
"""

import os
import pty
import select
import signal
import struct
import sys
import termios
import fcntl


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: e2e-pty.py -- <command> [args...]", file=sys.stderr)
        return 2

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

    stdin_open = True
    pty_open = True
    while stdin_open or pty_open:
        watch = [fd]
        if stdin_open:
            watch.append(0)
        readable, _, _ = select.select(watch, [], [], 30)
        if stdin_open and 0 in readable:
            try:
                data = os.read(0, 4096)
            except OSError:
                data = b""
            if data:
                try:
                    os.write(fd, data)
                except OSError:
                    pass
            else:
                # stdin EOF：关闭 master 使子进程输入侧收到 EOF（等价挂起写入端）。
                stdin_open = False
                try:
                    os.close(fd)
                except OSError:
                    pass
        if pty_open and fd in readable:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                os.write(1, data)
            else:
                pty_open = False

    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = 0
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.exit(main())
