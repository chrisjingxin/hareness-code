"""网络工具模块：验证 web_search/web_fetch 的配置检查、URL 校验、内容处理和参数限制。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from harness_agent.tools.tools_web import _strip_html_tags, web_fetch, web_search


@pytest.mark.asyncio
async def test_web_search_no_config_returns_error():
    """未配置搜索 API 环境变量时应返回错误信息。"""
    with patch.dict("os.environ", {}, clear=True):
        result = await web_search("test query")

    assert result["results"] == []
    assert "搜索服务未配置" in result["error"]


@pytest.mark.asyncio
async def test_web_fetch_invalid_url():
    """非 http/https 协议的 URL 应返回错误。"""
    result = await web_fetch("ftp://example.com/file.txt")

    assert result["content"] == ""
    assert "error" in result
    assert "http://" in result["error"]


@pytest.mark.asyncio
async def test_web_fetch_success_mock():
    """mock HTTP 响应后应正确返回格式化内容。"""
    mock_html = "<html><body><h1>标题</h1><p>正文内容</p></body></html>"

    with patch("harness_agent.tools.tools_web._HAS_AIOHTTP", False):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = mock_html.encode("utf-8")
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_response

            result = await web_fetch("https://example.com", format="text")

    assert result["url"] == "https://example.com"
    assert result["format"] == "text"
    assert "标题" in result["content"]
    assert "正文内容" in result["content"]
    assert "<" not in result["content"]
    assert "error" not in result


def test_strip_html_tags():
    """HTML 标签去除应保留纯文本内容。"""
    html = '<div class="main"><p>段落一</p><span>段落二</span><br/><a href="#">链接</a></div>'
    text = _strip_html_tags(html)

    assert "段落一" in text
    assert "段落二" in text
    assert "链接" in text
    assert "<div" not in text
    assert "<p>" not in text
    assert "<span>" not in text
    assert "<a " not in text


@pytest.mark.asyncio
async def test_web_search_num_results_clamped():
    """num_results 超过 10 时应被限制为 10。"""
    mock_data = {"results": [{"title": f"r{i}", "url": f"http://e.com/{i}", "snippet": f"s{i}"} for i in range(15)]}

    with patch.dict(
        "os.environ",
        {"HARNESS_SEARCH_API_URL": "https://api.test.com/search", "HARNESS_SEARCH_API_KEY": "key123"},
    ):
        with patch("harness_agent.tools.tools_web._HAS_AIOHTTP", False):
            with patch("urllib.request.urlopen") as mock_urlopen:
                import json

                mock_response = MagicMock()
                mock_response.read.return_value = json.dumps(mock_data).encode("utf-8")
                mock_response.__enter__ = MagicMock(return_value=mock_response)
                mock_response.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_response

                result = await web_search("query", num_results=20)

    # 即使请求了 20 条，返回结果最多 10 条
    assert len(result["results"]) <= 10
