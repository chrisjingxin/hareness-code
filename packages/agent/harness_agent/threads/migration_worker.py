"""可终止的 Harness legacy SQLite migration worker。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from harness_agent.threads.thread_persistence import run_legacy_migration_child


def main() -> int:
    """只接收规范化数据库路径和非秘密迁移参数。

    ZC-108：``--attempt-id`` 和 ``--temp-dir`` 由父进程在 prepared manifest
    中登记；child 解析 manifest 验证归属后才写 child-ready。
    """
    parser = argparse.ArgumentParser(description="Harness legacy migration worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project-fingerprint", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--temp-dir", required=True)
    parser.add_argument("--test-phase")
    args = parser.parse_args()
    attempt_id = str(args.attempt_id)
    # absolute() 只规范相对路径，不跟随最后一段 symlink；真正的目录身份由
    # child 在访问 SQLite 前用 lstat 与 manifest 逐字段验证。
    temp_dir = Path(args.temp_dir).absolute()
    try:
        asyncio.run(
            run_legacy_migration_child(
                Path(args.database).resolve(),
                args.project_fingerprint,
                args.test_phase,
                attempt_id=attempt_id,
                temp_dir=temp_dir,
            )
        )
    except BaseException:
        # Parent classifies the database/state facts after this process has
        # exited.  Do not print exception text, paths, or configuration data.
        error = sys.exc_info()[1]
        code = str(error).splitlines()[0][:160] if error is not None else ""
        if not code.startswith("CHECKPOINT_"):
            code = "CHECKPOINT_MIGRATION_WORKER_FAILED"
        sys.stdout.write(code + "\n")
        sys.stdout.flush()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
