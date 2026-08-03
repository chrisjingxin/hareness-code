"""可终止的 Harness legacy SQLite migration worker。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from harness_agent.thread_persistence import run_legacy_migration_child


def main() -> int:
    """只接收规范化数据库路径和非秘密迁移参数。"""
    parser = argparse.ArgumentParser(description="Harness legacy migration worker")
    parser.add_argument("--database", required=True)
    parser.add_argument("--project-fingerprint", required=True)
    parser.add_argument("--test-phase")
    args = parser.parse_args()
    try:
        asyncio.run(
            run_legacy_migration_child(
                Path(args.database).resolve(),
                args.project_fingerprint,
                args.test_phase,
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
