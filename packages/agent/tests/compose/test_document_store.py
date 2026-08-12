"""Compose Workspace Markdown 文档契约测试。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness_agent.compose.models import ComposeDocumentKind


def _document(
    kind: ComposeDocumentKind,
    *,
    work_item_id: str = "work-1",
    revision: int = 1,
    status: str = "draft",
    body: str = "# 文档\n\n用于验证 Work Item 的 Markdown 事实。\n",
) -> str:
    """构造满足 WP5 基础 front matter 契约的 Markdown。"""
    return (
        "---\n"
        f"work_item_id: {work_item_id}\n"
        f"kind: {kind.value}\n"
        f"revision: {revision}\n"
        f"status: {status}\n"
        "updated_at: 1700000000000\n"
        "---\n"
        f"{body}"
    )


@pytest.mark.asyncio
async def test_document_store_commits_canonical_path_and_round_trips_snapshot(
    tmp_path: Path,
) -> None:
    """文档只能写入固定 docs root 和文件名，inspect 返回完整 identity。"""
    from harness_agent.compose.document_store import (
        ComposeDocumentStore,
        DocumentCommit,
    )

    store = ComposeDocumentStore(tmp_path)
    created = await store.commit(
        DocumentCommit(
            work_item_id="work-1",
            slug="checkout-flow",
            kind=ComposeDocumentKind.TASK,
            content=_document(ComposeDocumentKind.TASK),
        )
    )

    assert created.relative_path == "docs/compose/checkout-flow/task.md"
    assert created.work_item_id == "work-1"
    assert created.kind is ComposeDocumentKind.TASK
    assert created.revision == 1
    assert (tmp_path / created.relative_path).is_file()
    assert await store.inspect("work-1", "checkout-flow", ComposeDocumentKind.TASK) == created


@pytest.mark.asyncio
async def test_document_store_uses_snapshot_cas_and_detects_external_edit(
    tmp_path: Path,
) -> None:
    """外部编辑后旧 Snapshot 不得覆盖 Markdown，需重新 inspect 后再提交。"""
    from harness_agent.compose.document_store import (
        ComposeDocumentStore,
        ComposeDocumentStoreError,
        DocumentCommit,
    )

    store = ComposeDocumentStore(tmp_path)
    first = await store.commit(
        DocumentCommit(
            work_item_id="work-1",
            slug="checkout-flow",
            kind=ComposeDocumentKind.TASK,
            content=_document(ComposeDocumentKind.TASK),
        )
    )
    path = tmp_path / first.relative_path
    path.write_text(
        _document(ComposeDocumentKind.TASK, revision=2, body="# 用户手动修改\n"),
        encoding="utf-8",
    )

    with pytest.raises(ComposeDocumentStoreError, match="COMPOSE_DOCUMENT_CONFLICT"):
        await store.commit(
            DocumentCommit(
                work_item_id="work-1",
                slug="checkout-flow",
                kind=ComposeDocumentKind.TASK,
                content=_document(ComposeDocumentKind.TASK, revision=3),
                expected=first,
            )
        )

    current = await store.inspect("work-1", "checkout-flow", ComposeDocumentKind.TASK)
    assert current is not None
    updated = await store.commit(
        DocumentCommit(
            work_item_id="work-1",
            slug="checkout-flow",
            kind=ComposeDocumentKind.TASK,
            content=_document(ComposeDocumentKind.TASK, revision=3),
            expected=current,
        )
    )
    assert updated.revision == 3
    assert updated.digest != first.digest


@pytest.mark.asyncio
async def test_document_store_does_not_overwrite_malformed_current_document(
    tmp_path: Path,
) -> None:
    """CAS 命令的 Snapshot 必须不能绕过现有 Markdown 的 front matter 校验。"""
    from harness_agent.compose.document_store import (
        ComposeDocumentStore,
        ComposeDocumentStoreError,
        DocumentCommit,
    )
    from harness_agent.threads.text_backend import LocalTextMutationBackend

    store = ComposeDocumentStore(tmp_path)
    first = await store.commit(
        DocumentCommit(
            work_item_id="work-1",
            slug="checkout-flow",
            kind=ComposeDocumentKind.TASK,
            content=_document(ComposeDocumentKind.TASK),
        )
    )
    path = tmp_path / first.relative_path
    path.write_text("# 被外部损坏的文档\n", encoding="utf-8")
    raw = LocalTextMutationBackend(tmp_path).read_text_document(
        f"/{first.relative_path}"
    )
    forged = replace(first, identity=raw.identity)

    with pytest.raises(
        ComposeDocumentStoreError,
        match="COMPOSE_DOCUMENT_FRONT_MATTER_INVALID",
    ):
        await store.commit(
            DocumentCommit(
                work_item_id="work-1",
                slug="checkout-flow",
                kind=ComposeDocumentKind.TASK,
                content=_document(ComposeDocumentKind.TASK, revision=2),
                expected=forged,
            )
        )

    assert path.read_text(encoding="utf-8") == "# 被外部损坏的文档\n"


@pytest.mark.asyncio
async def test_document_store_rejects_invalid_root_slug_and_front_matter(
    tmp_path: Path,
) -> None:
    """路径穿越、非固定 slug 和畸形 front matter 都不能写入工作空间。"""
    from harness_agent.compose.document_store import (
        ComposeDocumentStore,
        ComposeDocumentStoreError,
        DocumentCommit,
    )

    for docs_dir in ("../compose", "/tmp/compose", ".harness/compose", "docs/../compose"):
        with pytest.raises(ComposeDocumentStoreError, match="COMPOSE_DOCS_DIR_INVALID"):
            ComposeDocumentStore(tmp_path, docs_dir=docs_dir)

    store = ComposeDocumentStore(tmp_path, docs_dir="engineering/compose")
    with pytest.raises(ComposeDocumentStoreError, match="COMPOSE_DOCUMENT_PATH_INVALID"):
        await store.commit(
            DocumentCommit(
                work_item_id="work-1",
                slug="../escape",
                kind=ComposeDocumentKind.TASK,
                content=_document(ComposeDocumentKind.TASK),
            )
        )
    with pytest.raises(ComposeDocumentStoreError, match="COMPOSE_DOCUMENT_FRONT_MATTER_INVALID"):
        await store.commit(
            DocumentCommit(
                work_item_id="work-1",
                slug="checkout-flow",
                kind=ComposeDocumentKind.TASK,
                content="# 没有 front matter\n",
            )
        )
