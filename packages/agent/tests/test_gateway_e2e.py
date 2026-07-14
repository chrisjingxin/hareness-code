"""Loopback end-to-end coverage for the OpenAI-compatible streaming adapter."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from za38_agent.agent import create_za38_agent
from za38_agent.config import ModelSettings
from za38_agent.providers.za38_gateway import create_openai_compatible_model


class _OpenAIStreamingHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.requests.append(payload)
        chunks = [
            {
                "id": "mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "mocked response"}, "finish_reason": None}],
            },
            {
                "id": "mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        ]
        encoded = ("".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_: object) -> None:
        pass


@pytest.mark.e2e
async def test_openai_compatible_agent_streams_against_mock_gateway(monkeypatch: pytest.MonkeyPatch):
    if os.environ.get("ZA38_RUN_LOOPBACK_E2E") != "1":
        pytest.skip("Set ZA38_RUN_LOOPBACK_E2E=1 to run loopback gateway coverage")
    _OpenAIStreamingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIStreamingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ZA38_TEST_KEY", "test-key")
    try:
        model = create_openai_compatible_model(
            ModelSettings(
                name="mock",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key_env="ZA38_TEST_KEY",
            )
        )
        agent = create_za38_agent(
            model,
            auto_approve=True,
            enable_ask_user=False,
            enable_interpreter=False,
            enable_memory=False,
            enable_skills=False,
        )
        events = [
            event
            async for event in agent.astream(
                {"messages": [HumanMessage(content="reply with the mock text")]},
                config={"configurable": {"thread_id": "gateway-e2e"}},
                stream_mode=["messages", "updates"],
            )
        ]
    finally:
        server.shutdown()
        server.server_close()

    assert _OpenAIStreamingHandler.requests[0]["stream"] is True
    assert any("mocked response" in str(event) for event in events)
