"""Thread 记录模型拆分：保持旧入口兼容并隔离 SQLite owner。"""

from __future__ import annotations

import inspect


def test_thread_record_contracts_are_extracted_from_persistence_owner() -> None:
    """记录数据与纯转换逻辑独立成模块，旧导入路径仍保持同一对象。"""
    from harness_agent.threads import thread_persistence, thread_records

    assert thread_persistence.ThreadSummary is thread_records.ThreadSummary
    assert thread_persistence.TranscriptRecord is thread_records.TranscriptRecord
    assert thread_persistence._transcript_record is thread_records._transcript_record
    assert thread_persistence._normalize_message is thread_records._normalize_message
    assert (
        thread_persistence._set_legacy_tool_call_arguments
        is thread_records._set_legacy_tool_call_arguments
    )

    persistence_source = inspect.getsource(thread_persistence)
    records_source = inspect.getsource(thread_records)
    assert "class ThreadSummary:" not in persistence_source
    assert "def _transcript_record(" not in persistence_source
    assert "aiosqlite" not in records_source
    assert "sqlite3" not in records_source
