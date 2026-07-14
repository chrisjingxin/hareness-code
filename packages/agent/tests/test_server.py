"""Tests for the JSON-RPC stdio server."""
import json

import pytest


async def test_initialize_handshake():
    """Server responds to initialize with server_info."""
    from za38_agent.server import JsonRpcServer

    server = JsonRpcServer()

    # Simulate stdin with an initialize request
    init_msg = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {"client_info": {"name": "test", "version": "0.1.0"}},
        "id": 1,
    }

    responses = []

    async def mock_send(msg):
        responses.append(msg)

    server.send = mock_send
    await server.dispatch(init_msg)

    assert len(responses) == 1
    assert responses[0]["jsonrpc"] == "2.0"
    assert responses[0]["id"] == 1
    result = responses[0]["result"]
    assert result["server_info"]["name"] == "za38-agent"
    assert result["capabilities"]["streaming"] is True


async def test_echo_query():
    """Server accepts a query and sends stream/done."""
    from za38_agent.server import JsonRpcServer

    server = JsonRpcServer()
    notifications = []

    async def mock_send_notification(method, params):
        notifications.append({"method": method, "params": params})

    async def mock_send(msg):
        pass  # responses handled separately

    server.send = mock_send
    server.send_notification = mock_send_notification

    query_msg = {
        "jsonrpc": "2.0",
        "method": "query",
        "params": {"message": "hello"},
        "id": 2,
    }
    await server.dispatch(query_msg)

    # Should have received stream/done notification
    done_notifications = [n for n in notifications if n["method"] == "stream/done"]
    assert len(done_notifications) == 1
    assert "thread_id" in done_notifications[0]["params"]
