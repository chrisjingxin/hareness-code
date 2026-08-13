"""ZC-108 离线迁移恢复命令的确认门禁、幂等与错误分支测试。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import harness_agent.threads.migration_recovery as recovery
import harness_agent.threads.thread_persistence as persistence
from harness_agent.threads.thread_persistence import ThreadPersistenceError


def _preparing_attempt(tmp_path: Path) -> tuple[Path, Path]:
    """创建主库未改变、尚未 spawn child 的 preparing attempt。"""
    database = tmp_path / "threads.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE source_record(value TEXT NOT NULL)")
        connection.execute("INSERT INTO source_record(value) VALUES ('source')")
        connection.commit()
        source = persistence._migration_database_fingerprint_sync(connection)
    finally:
        connection.close()
    attempt_id = "a" * 32
    manifest_path = persistence._migration_attempt_manifest_path(database)
    payload = persistence._migration_build_attempt_manifest_payload(
        status="preparing",
        database=database.name,
        attempt_id=attempt_id,
        created_at_ms=1,
        source=source,
        temp_dir_name=persistence._migration_attempt_dir_name(
            database.name, attempt_id,
        ),
        temp_dir_identity=None,
        backup_temp_name=persistence._MIGRATION_ATTEMPT_TEMP_BACKUP_NAME,
        backup_temp_identity=None,
        restore_temp_name=persistence._MIGRATION_ATTEMPT_TEMP_RESTORE_NAME,
        restore_temp_identity=None,
    )
    persistence._migration_write_attempt_manifest(
        database,
        manifest_path,
        payload,
        old_status=None,
    )
    return database, manifest_path


def _prepared_attempt(tmp_path: Path) -> tuple[Path, Path, Path]:
    """创建登记完目录和两个 temp、尚未 spawn child 的 prepared attempt。"""
    database, manifest_path = _preparing_attempt(tmp_path)
    preparing = persistence._parse_migration_attempt_manifest(
        manifest_path, expected_database=database.name,
    )
    assert preparing is not None
    temp_dir = persistence._migration_attempt_dir_path(
        database, preparing.attempt_id,
    )
    temp_dir.mkdir(mode=0o700)
    backup_temp = persistence._migration_attempt_backup_temp_path(temp_dir)
    restore_temp = persistence._migration_attempt_restore_temp_path(temp_dir)
    for temp in (backup_temp, restore_temp):
        temp.write_bytes(b"")
        os.chmod(temp, 0o600)
    payload = persistence._migration_build_attempt_manifest_payload(
        status="prepared",
        database=database.name,
        attempt_id=preparing.attempt_id,
        created_at_ms=preparing.created_at_ms,
        source=preparing.source,
        temp_dir_name=temp_dir.name,
        temp_dir_identity=persistence._migration_file_identity_from_stat(
            temp_dir.lstat(),
        ),
        backup_temp_name=preparing.backup_temp_name,
        backup_temp_identity=persistence._migration_file_identity_from_path_lstat(
            backup_temp,
        ),
        restore_temp_name=preparing.restore_temp_name,
        restore_temp_identity=persistence._migration_file_identity_from_path_lstat(
            restore_temp,
        ),
    )
    persistence._migration_write_attempt_manifest(
        database, manifest_path, payload, old_status="preparing",
    )
    return database, manifest_path, temp_dir


def test_migration_recovery_without_confirmation_is_read_only(
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """缺少显式确认时只输出诊断，任何 marker 字节都不改变。"""
    database, manifest_path = _preparing_attempt(tmp_path)
    before = manifest_path.read_bytes()
    result = recovery.main(["--database", str(database)])
    output = capsys.readouterr().out
    assert result == 0
    assert "CHECKPOINT_MIGRATION_OFFLINE_CONFIRMATION_REQUIRED" in output
    assert manifest_path.read_bytes() == before


def test_migration_recovery_preparing_attempt_is_idempotent(
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """确认所有 worker 已停止后可收敛 preparing，并可安全重复执行。"""
    database, manifest_path = _preparing_attempt(tmp_path)
    argv = [
        "--database", str(database),
        "--confirm-all-harness-workers-stopped",
    ]
    assert recovery.main(argv) == 0
    first_output = capsys.readouterr().out
    assert "OK_SETTLED_source" in first_output
    assert not manifest_path.exists()

    assert recovery.main(argv) == 0
    second_output = capsys.readouterr().out
    assert "OK_NO_ATTEMPT" in second_output


def test_migration_recovery_accepts_already_removed_registered_directory(
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """目录已删、manifest 未封口的崩溃窗口可离线幂等收敛。"""
    database, manifest_path, temp_dir = _prepared_attempt(tmp_path)
    for temp in tuple(temp_dir.iterdir()):
        temp.unlink()
    temp_dir.rmdir()

    argv = [
        "--database", str(database),
        "--confirm-all-harness-workers-stopped",
    ]
    assert recovery.main(argv) == 0
    assert "OK_SETTLED_source" in capsys.readouterr().out
    assert not manifest_path.exists()

    assert recovery.main(argv) == 0
    assert "OK_NO_ATTEMPT" in capsys.readouterr().out


def test_migration_recovery_lock_error_returns_stable_code(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """lock 获取失败不能因异常变量未绑定而变成 NameError。"""
    database, _manifest_path = _preparing_attempt(tmp_path)

    def fail_lock(_self: object) -> None:
        raise ThreadPersistenceError("CHECKPOINT_MIGRATION_LOCK_UNAVAILABLE")

    monkeypatch.setattr(recovery._MigrationFileLock, "acquire_sync", fail_lock)
    result = recovery.main([
        "--database", str(database),
        "--confirm-all-harness-workers-stopped",
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert "CHECKPOINT_MIGRATION_LOCK_UNAVAILABLE" in captured.out
    assert "NameError" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_migration_recovery_rejects_database_symlink(
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
) -> None:
    """离线命令不得 resolve 后跟随用户提供的数据库 symlink。"""
    database, manifest_path = _preparing_attempt(tmp_path)
    link = tmp_path / "database-link.sqlite3"
    link.symlink_to(database)
    before = manifest_path.read_bytes()

    result = recovery.main([
        "--database", str(link),
        "--confirm-all-harness-workers-stopped",
    ])

    assert result == 1
    assert "CHECKPOINT_MIGRATION_DATABASE_INVALID" in capsys.readouterr().out
    assert manifest_path.read_bytes() == before
