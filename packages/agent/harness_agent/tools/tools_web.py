"""网络工具：提供 web_search 和 web_fetch 能力，供 Agent 获取实时网络信息。"""

from __future__ import annotations

import asyncio
import os
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

try:
    import aiohttp

    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

_DEFAULT_SEARCH_API_URL = "https://api.search.example.com/v1/search"
_MAX_CONTENT_LENGTH = 100_000
_SEARCH_TIMEOUT = 10
_FETCH_TIMEOUT = 30


@dataclass
class SearchResult:
    """单条搜索结果。"""

    title: str
    url: str
    snippet: str


@dataclass
class SearchResponse:
    """网络搜索响应结构。"""

    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None


@dataclass
class FetchResponse:
    """网页抓取响应结构。"""

    content: str = ""
    url: str = ""
    format: str = "markdown"
    error: str | None = None


def _strip_html_tags(html: str) -> str:
    """去除 HTML 标签，返回纯文本。"""
    text = re.sub(r"<[^>]+>", "", html)
    # 合并多余空白行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _html_to_markdown(html: str) -> str:
    """简单 HTML 到 Markdown 转换：去除 script/style，保留基本结构。"""
    # 去除 script 和 style 块
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 标题转换
    for level in range(1, 7):
        text = re.sub(
            rf"<h{level}[^>]*>(.*?)</h{level}>",
            lambda m, lv=level: f"\n{'#' * lv} {m.group(1).strip()}\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # 链接转换
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', r"[\2](\1)", text, flags=re.DOTALL | re.IGNORECASE)
    # 段落转换
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    # 去除剩余标签
    text = re.sub(r"<[^>]+>", "", text)
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def web_search(query: str, num_results: int = 5) -> dict[str, Any]:
    """执行网络搜索，返回结构化结果。

    Args:
        query: 搜索关键词。
        num_results: 返回结果数量，默认 5，最大 10。

    Returns:
        {"results": [{"title": str, "url": str, "snippet": str}]}
    """
    num_results = max(1, min(num_results, 10))

    api_url = os.environ.get("HARNESS_SEARCH_API_URL", "")
    api_key = os.environ.get("HARNESS_SEARCH_API_KEY", "")

    if not api_url or not api_key:
        return {
            "results": [],
            "error": "搜索服务未配置，请设置 HARNESS_SEARCH_API_URL 和 HARNESS_SEARCH_API_KEY 环境变量",
        }

    params = f"?q={urllib.request.quote(query)}&num={num_results}"
    request_url = f"{api_url}{params}"

    try:
        if _HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {api_key}"}
                async with session.get(
                    request_url, headers=headers, timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT)
                ) as resp:
                    data = await resp.json()
        else:
            # 回退到 urllib.request，在线程池中执行以避免阻塞事件循环
            def _sync_request() -> dict[str, Any]:
                req = urllib.request.Request(request_url, headers={"Authorization": f"Bearer {api_key}"})
                with urllib.request.urlopen(req, timeout=_SEARCH_TIMEOUT) as response:
                    import json

                    return json.loads(response.read().decode("utf-8"))

            data = await asyncio.to_thread(_sync_request)

        results = [
            {"title": item.get("title", ""), "url": item.get("url", ""), "snippet": item.get("snippet", "")}
            for item in data.get("results", [])[:num_results]
        ]
        return {"results": results}
    except Exception as exc:
        return {"results": [], "error": str(exc)}


async def web_fetch(url: str, format: str = "markdown") -> dict[str, Any]:
    """获取指定 URL 的内容。

    Args:
        url: 目标 URL（必须为 http/https）。
        format: 输出格式（text/markdown/html），默认 markdown。

    Returns:
        {"content": str, "url": str, "format": str}
    """
    if not url.startswith(("http://", "https://")):
        return {"content": "", "url": url, "format": format, "error": "URL 必须以 http:// 或 https:// 开头"}

    try:
        if _HAS_AIOHTTP:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)) as resp:
                    html = await resp.text()
        else:
            def _sync_fetch() -> str:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as response:
                    return response.read().decode("utf-8", errors="replace")

            html = await asyncio.to_thread(_sync_fetch)

        if format == "html":
            content = html
        elif format == "text":
            content = _strip_html_tags(html)
        else:
            content = _html_to_markdown(html)

        # 内容截断
        content = content[:_MAX_CONTENT_LENGTH]
        return {"content": content, "url": url, "format": format}
    except Exception as exc:
        return {"content": "", "url": url, "format": format, "error": str(exc)}
