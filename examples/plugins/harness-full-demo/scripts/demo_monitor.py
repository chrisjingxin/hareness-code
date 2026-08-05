"""Harness Full Demo 的有界后台 Monitor。"""

from __future__ import annotations

import os
import time


def main() -> None:
    """定期输出简单状态；Host 关闭时由 PluginRuntimeManager 终止。"""
    while True:
        print(
            f"harness-full-demo ready; workspace={os.getcwd()}",
            flush=True,
        )
        time.sleep(15)


if __name__ == "__main__":
    main()
