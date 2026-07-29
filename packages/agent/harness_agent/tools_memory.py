"""跨会话记忆工具：提供记忆的保存和检索能力，支持 Agent 在多轮对话间保持上下文。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _memory_dir() -> Path:
    """返回记忆存储目录 ~/.harness/memory/，不存在时自动创建。"""
    d = Path.home() / ".harness" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitize_key(key: str) -> str:
    """对 key 做安全过滤，只保留字母、数字、连字符、下划线。"""
    return re.sub(r"[^a-zA-Z0-9\-_]", "_", key)


def memory_save(key: str, content: str) -> dict[str, Any]:
    """保存一条记忆。

    Args:
        key: 记忆标识（用作文件名，会做安全过滤）。
        content: 记忆内容。

    Returns:
        {"success": True, "key": str} 或错误。
    """
    try:
        safe_key = _sanitize_key(key)
        record = {
            "key": safe_key,
            "content": content,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        path = _memory_dir() / f"{safe_key}.json"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return {"success": True, "key": safe_key}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def memory_search(query: str) -> dict[str, Any]:
    """搜索已保存的记忆。

    Args:
        query: 搜索关键词（大小写不敏感子串匹配）。

    Returns:
        {"results": [{"key": str, "content": str, "saved_at": str}]}
    """
    memory_dir = Path.home() / ".harness" / "memory"
    if not memory_dir.exists():
        return {"results": []}

    results: list[dict[str, Any]] = []
    query_lower = query.lower()

    for path in sorted(memory_dir.glob("*.json")):
        if len(results) >= 10:
            break
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        key = record.get("key", "")
        content = record.get("content", "")
        if query_lower in key.lower() or query_lower in content.lower():
            results.append({
                "key": key,
                "content": content,
                "saved_at": record.get("saved_at", ""),
            })

    return {"results": results}
