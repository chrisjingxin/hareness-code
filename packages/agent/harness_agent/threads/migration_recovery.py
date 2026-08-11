"""ZC-108 离线迁移恢复命令：在不依赖 PID 扫描的前提下收敛 active attempt。

用户在 ``exit_unknown`` 残留下需要停止所有 Harness worker 后手动执行此命令。
没有确认参数时只读诊断并返回 ``CHECKPOINT_MIGRATION_OFFLINE_CONFIRMATION_REQUIRED``；
apply 模式仍获取 canonical migration lock，但该锁不能证明 orphan child 已退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Sequence

from harness_agent.threads.thread_persistence import (
    ThreadPersistence,
    ThreadPersistenceError,
    _assert_migration_file_identity_supported,
    _fsync_directory_best_effort,
    _migration_attempt_dir_path,
    _migration_attempt_manifest_path,
    _migration_attempt_staging_path,
    _migration_build_attempt_manifest_payload,
    _migration_cleanup_attempt_temps,
    _migration_database_fingerprint_sync,
    _migration_fingerprint_matches,
    _migration_path_entry_exists,
    _migration_poison_path,
    _migration_poison_staging_path,
    _migration_remove_empty_attempt_dir,
    _migration_state_staging_path,
    _migration_unlink_strict_marker,
    _migration_write_attempt_manifest,
    _parse_migration_attempt_manifest,
    _parse_migration_state,
    _validate_migration_attempt_directory,
    _MigrationFileLock,
    _MIGRATION_LOCK_SUFFIX,
    _MIGRATION_STATE_VERSION,
    _MIGRATION_STATE_SUFFIX,
)


def _diagnose_attempt(path: Path) -> dict[str, object]:
    """只读诊断 active attempt 的当前状态。"""
    manifest_path = _migration_attempt_manifest_path(path)
    poison_path = _migration_poison_path(path)
    state_path = path.with_name(path.name + _MIGRATION_STATE_SUFFIX)
    result: dict[str, object] = {
        "database": path.name,
        "manifest_exists": manifest_path.exists(),
        "poison_exists": poison_path.exists(),
        "state_exists": state_path.exists(),
    }
    try:
        manifest = _parse_migration_attempt_manifest(
            manifest_path, expected_database=path.name,
        )
        if manifest is not None:
            result["manifest_status"] = manifest.status
            result["manifest_attempt_id"] = manifest.attempt_id[:8]
            result["manifest_source_user_version"] = manifest.source.user_version
            if manifest.settled_result is not None:
                result["manifest_settled_result"] = manifest.settled_result
    except (OSError, ValueError) as exc:
        result["manifest_error"] = str(exc).split(":")[0][:80]
    if path.is_file():
        try:
            conn = sqlite3.connect(path)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                result["database_user_version"] = int(version)
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            result["database_user_version"] = "unreadable"
    else:
        result["database_user_version"] = "missing"
    return result


def _offline_settle_attempt_sync(path: Path) -> str:
    """在确认所有 worker 已停止后收敛 active attempt。

    只接受 canonical basename、严格 marker、完整 fingerprint 和精确登记文件。
    active ``prepared/exit_unknown`` 按 state/source/final/backup 事实收敛。
    ``preparing`` 的未登记 identity 文件只走离线特殊规则。
    """
    manifest_path = _migration_attempt_manifest_path(path)
    manifest = _parse_migration_attempt_manifest(
        manifest_path, expected_database=path.name,
    )
    if manifest is None:
        residuals = (
            _migration_poison_path(path),
            path.with_name(path.name + _MIGRATION_STATE_SUFFIX),
            _migration_attempt_staging_path(path),
            _migration_poison_staging_path(path),
            _migration_state_staging_path(path),
        )
        if any(_migration_path_entry_exists(residual) for residual in residuals):
            return "CHECKPOINT_MIGRATION_ATTEMPT_MISSING"
        return "OK_NO_ATTEMPT"
    if manifest.is_settled:
        if manifest.settled_database is None or not path.is_file():
            return "CHECKPOINT_MIGRATION_STATE_INVALID"
        connection = sqlite3.connect(path)
        try:
            current = _migration_database_fingerprint_sync(connection)
        finally:
            connection.close()
        if not _migration_fingerprint_matches(manifest.settled_database, current):
            return "CHECKPOINT_MIGRATION_SETTLED_DATABASE_MISMATCH"
        state_path = path.with_name(path.name + _MIGRATION_STATE_SUFFIX)
        for marker_path in (
            _migration_state_staging_path(path),
            _migration_poison_staging_path(path),
            _migration_attempt_staging_path(path),
            state_path,
            _migration_poison_path(path),
            manifest_path,
        ):
            _migration_unlink_strict_marker(marker_path)
        return "OK_SETTLED_HOUSEKEEPING"
    if not manifest.is_active:
        return "CHECKPOINT_MIGRATION_STATE_INVALID"
    _assert_migration_file_identity_supported()
    temp_dir = _migration_attempt_dir_path(path, manifest.attempt_id)
    source = manifest.source
    backup_path = path.with_name(
        f"{path.name}.pre-v{source.user_version}-migration.bak"
    )
    current = None
    if path.is_file():
        try:
            conn = sqlite3.connect(path)
            try:
                current = _migration_database_fingerprint_sync(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            pass
    if current is None:
        return "CHECKPOINT_MIGRATION_DATABASE_UNREADABLE"
    state_path = path.with_name(path.name + _MIGRATION_STATE_SUFFIX)

    if manifest.status == "preparing":
        # preparing 保证 Popen 从未发生。显式离线确认后只删除确定目录中的
        # planned 普通私有文件，不猜测其他名称或跟随 symlink。
        if _migration_path_entry_exists(temp_dir):
            stat_result = temp_dir.lstat()
            if not stat.S_ISDIR(stat_result.st_mode):
                return "CHECKPOINT_MIGRATION_ATTEMPT_DIR_IDENTITY_MISMATCH"
            if os.name != "nt" and (
                    stat_result.st_uid != os.geteuid()
                    or stat.S_IMODE(stat_result.st_mode) & 0o077
            ):
                return "CHECKPOINT_MIGRATION_ATTEMPT_DIR_IDENTITY_MISMATCH"
            for planned_name in (
                manifest.backup_temp_name,
                manifest.restore_temp_name,
                "child-ready.json",
                "child-ready.staging.json",
            ):
                planned_path = temp_dir / planned_name
                if _migration_path_entry_exists(planned_path):
                    _migration_unlink_strict_marker(planned_path)
            try:
                temp_dir.rmdir()
                _fsync_directory_best_effort(temp_dir.parent)
            except OSError:
                return "CHECKPOINT_MIGRATION_CLEANUP_NOT_CLOSABLE"
        if not _migration_fingerprint_matches(source, current):
            return "CHECKPOINT_MIGRATION_SOURCE_CHANGED"
        ThreadPersistence._validate_source_database_path_sync(path, source)
        state = None
        settled_result = "source"
        settled_database = current
    else:
        try:
            if _migration_path_entry_exists(temp_dir):
                _validate_migration_attempt_directory(temp_dir, manifest.temp_dir_identity)
            state = _parse_migration_state(
                state_path,
                expected_database=path.name,
                expected_attempt_id=manifest.attempt_id,
            )
        except (OSError, ValueError):
            return "CHECKPOINT_MIGRATION_STATE_INVALID"
        if state is not None and (
                state.version != _MIGRATION_STATE_VERSION
                or not _migration_fingerprint_matches(source, state.source)
                or state.backup != backup_path.name
        ):
            return "CHECKPOINT_MIGRATION_STATE_INVALID"
        settled_result = None
        settled_database = None
        if state is None:
            if not _migration_fingerprint_matches(source, current):
                return "CHECKPOINT_MIGRATION_STATE_MISSING"
            ThreadPersistence._validate_source_database_path_sync(path, source)
            settled_result = "source"
            settled_database = current
        elif state.final is not None and _migration_fingerprint_matches(state.final, current):
            ThreadPersistence._validate_final_database_path_sync(path, state.final)
            settled_result = "final"
            settled_database = state.final
        elif _migration_fingerprint_matches(source, current):
            ThreadPersistence._validate_backup_path_sync(backup_path, source)
            ThreadPersistence._validate_source_database_path_sync(path, source)
            settled_result = "source"
            settled_database = current
        else:
            ThreadPersistence._validate_backup_path_sync(backup_path, source)
            ThreadPersistence._restore_backup_path_sync(
                path,
                backup_path,
                source,
                registered_temp=temp_dir / manifest.restore_temp_name,
                registered_identity=manifest.restore_temp_identity,
            )
            ThreadPersistence._validate_source_database_path_sync(path, source)
            settled_result = "source"
            settled_database = source
    if manifest.status == "preparing":
        cleanup_result = None
        cleanup_summary: dict[str, object] = {"offline_preparing": True}
    else:
        cleanup_result = _migration_cleanup_attempt_temps(
            temp_dir, manifest, unlink_fault_prefix="offline",
        )
        if not cleanup_result.closable:
            return "CHECKPOINT_MIGRATION_CLEANUP_NOT_CLOSABLE"
        dir_ok, _ = _migration_remove_empty_attempt_dir(temp_dir, cleanup_result)
        if not dir_ok:
            return "CHECKPOINT_MIGRATION_CLEANUP_NOT_CLOSABLE"
        cleanup_summary = {
            "deleted": list(cleanup_result.deleted),
            "resolved_absent": list(cleanup_result.resolved_absent),
            "foreign_replacements": list(cleanup_result.foreign_replacements),
        }
    for staging_path in (
        _migration_state_staging_path(path),
        _migration_poison_staging_path(path),
        _migration_attempt_staging_path(path),
    ):
        _migration_unlink_strict_marker(staging_path)
    # 封口 manifest。
    settled_payload = _migration_build_attempt_manifest_payload(
        status="settled",
        database=path.name,
        attempt_id=manifest.attempt_id,
        created_at_ms=manifest.created_at_ms,
        source=source,
        temp_dir_name=manifest.temp_dir_name,
        temp_dir_identity=manifest.temp_dir_identity,
        backup_temp_name=manifest.backup_temp_name,
        backup_temp_identity=manifest.backup_temp_identity,
        restore_temp_name=manifest.restore_temp_name,
        restore_temp_identity=manifest.restore_temp_identity,
        settled_result=settled_result,
        settled_database=settled_database,
        child_returncode=None,
        cleanup_summary=cleanup_summary,
    )
    _migration_write_attempt_manifest(
        path, manifest_path, settled_payload, old_status=manifest.status,
    )
    # 清 state、poison、manifest。
    _migration_unlink_strict_marker(state_path)
    _migration_unlink_strict_marker(_migration_poison_path(path))
    _migration_unlink_strict_marker(manifest_path)
    return f"OK_SETTLED_{settled_result}"


def main(argv: Sequence[str] | None = None) -> int:
    """离线恢复命令入口；可直接单测。"""
    parser = argparse.ArgumentParser(
        description="Harness offline migration recovery (ZC-108)",
    )
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--confirm-all-harness-workers-stopped",
        action="store_true",
        help="确认所有 Harness worker 已停止后才执行 apply 模式",
    )
    args = parser.parse_args(argv)
    path = Path(args.database).expanduser().absolute()
    try:
        database_stat = path.lstat()
    except OSError:
        sys.stdout.write("CHECKPOINT_MIGRATION_DATABASE_MISSING\n")
        sys.stdout.flush()
        return 1
    if not stat.S_ISREG(database_stat.st_mode):
        sys.stdout.write("CHECKPOINT_MIGRATION_DATABASE_INVALID\n")
        sys.stdout.flush()
        return 1
    if os.name != "nt" and database_stat.st_uid != os.geteuid():
        sys.stdout.write("CHECKPOINT_MIGRATION_DATABASE_INVALID\n")
        sys.stdout.flush()
        return 1
    # dry-run：只读诊断。
    diagnosis = _diagnose_attempt(path)
    sys.stdout.write(json.dumps(diagnosis, sort_keys=True) + "\n")
    sys.stdout.flush()
    if not args.confirm_all_harness_workers_stopped:
        sys.stdout.write(
            "CHECKPOINT_MIGRATION_OFFLINE_CONFIRMATION_REQUIRED\n"
        )
        sys.stdout.flush()
        return 0
    # apply 模式：获取 migration lock 后收敛。
    lock = _MigrationFileLock(path.with_name(path.name + _MIGRATION_LOCK_SUFFIX))
    try:
        lock.acquire_sync()
    except ThreadPersistenceError as exc:
        sys.stdout.write(f"CHECKPOINT_MIGRATION_LOCK_UNAVAILABLE\n")
        sys.stdout.flush()
        sys.stderr.write(f"lock error: {str(exc).split(':')[0]}\n")
        return 1
    try:
        result = _offline_settle_attempt_sync(path)
        sys.stdout.write(result + "\n")
        sys.stdout.flush()
        return 0 if result.startswith("OK_") else 1
    except ThreadPersistenceError as exc:
        code = str(exc).split(":")[0][:120]
        if not code.startswith("CHECKPOINT_"):
            code = "CHECKPOINT_MIGRATION_RECOVERY_FAILED"
        sys.stdout.write(code + "\n")
        sys.stdout.flush()
        sys.stderr.write(f"recovery error: {str(exc).split(':')[0]}\n")
        return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        sys.stdout.write("CHECKPOINT_MIGRATION_RECOVERY_FAILED\n")
        sys.stdout.flush()
        sys.stderr.write(f"recovery error: {type(exc).__name__}\n")
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
