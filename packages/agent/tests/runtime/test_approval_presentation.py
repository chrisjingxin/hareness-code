"""Run 内审批展示缓存的身份、有界生命周期与降级测试。"""

from __future__ import annotations

from harness_agent.runtime import approval_presentation
from harness_agent.runtime.approval_presentation import ApprovalPresentationStore


def _presentation(path: str) -> dict[str, object]:
    return {
        "kind": "file_diff",
        "operation": "edit",
        "path": path,
        "added_lines": 1,
        "removed_lines": 1,
        "truncated": False,
        "unified_diff": "+new",
    }


def test_store_binds_tool_name_and_complete_args_without_retaining_mutable_input() -> None:
    """改参或改工具名不能命中，返回值也不能修改缓存内部副本。"""
    store = ApprovalPresentationStore()
    args = {"file_path": "/a.py", "new_string": "new"}
    presentation = _presentation("/a.py")
    assert store.remember("edit_file", args, presentation)
    presentation["path"] = "/changed.py"

    found = store.lookup("edit_file", args)
    assert found is not None
    assert found["path"] == "/a.py"
    found["path"] = "/mutated.py"
    assert store.lookup("edit_file", args)["path"] == "/a.py"
    assert store.lookup("edit_file", {**args, "new_string": "changed"}) is None
    assert store.lookup("write_file", args) is None


def test_store_evicts_lru_and_expires_entries(monkeypatch) -> None:
    """数量上限与 TTL 都让旧展示安全降级为缺失。"""
    now = 10.0
    monkeypatch.setattr(approval_presentation.time, "monotonic", lambda: now)
    store = ApprovalPresentationStore(max_entries=2, ttl_seconds=5)
    assert store.remember("edit_file", {"id": 1}, _presentation("/1.py"))
    assert store.remember("edit_file", {"id": 2}, _presentation("/2.py"))
    assert store.remember("edit_file", {"id": 3}, _presentation("/3.py"))
    assert store.lookup("edit_file", {"id": 1}) is None

    now = 16.0
    assert store.lookup("edit_file", {"id": 2}) is None
    assert store.lookup("edit_file", {"id": 3}) is None


def test_store_rejects_non_json_and_clear_releases_entries() -> None:
    """无法稳定编码的参数不登记，Run 收敛可立即清空所有展示。"""
    store = ApprovalPresentationStore()
    assert store.remember("edit_file", {"bad": object()}, _presentation("/a.py")) is False
    assert store.remember("edit_file", {"id": 1}, _presentation("/a.py"))
    store.clear()
    assert store.lookup("edit_file", {"id": 1}) is None
