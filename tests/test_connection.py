"""Client-side protocol tests — these run without Blender installed."""

from __future__ import annotations

import pytest

from blender_mcp import connection as conn
from blender_mcp.connection import (
    BlenderCommandError,
    BlenderConnection,
    BlenderConnectionError,
)


def test_roundtrip_sends_framed_command(fake_blender):
    server = fake_blender()
    client = BlenderConnection(port=server.port, timeout=5)

    result = client.execute("get_scene_info", {"limit": 3})

    assert result == {"echo": {"type": "get_scene_info", "params": {"limit": 3}}}
    assert server.received[0]["type"] == "get_scene_info"
    client.close()


def test_connection_is_reused_across_commands(fake_blender):
    server = fake_blender()
    client = BlenderConnection(port=server.port, timeout=5)

    client.execute("ping")
    client.execute("ping")

    assert len(server.received) == 2
    client.close()


def test_error_response_raises_with_traceback(fake_blender):
    server = fake_blender(
        responder=lambda message: {
            "status": "error",
            "message": "KeyError: object 'Nope' not found",
            "traceback": "Traceback (most recent call last): ...",
        }
    )
    client = BlenderConnection(port=server.port, timeout=5)

    with pytest.raises(BlenderCommandError) as excinfo:
        client.execute("get_object_info", {"name": "Nope"})

    assert "not found" in str(excinfo.value)
    assert excinfo.value.blender_traceback.startswith("Traceback")
    client.close()


def test_refused_connection_explains_how_to_fix():
    from tests.conftest import free_port

    client = BlenderConnection(port=free_port(), timeout=2)

    with pytest.raises(BlenderConnectionError) as excinfo:
        client.execute("ping")

    message = str(excinfo.value)
    assert "No Blender MCP server is listening" in message
    assert "Start server" in message


def test_stale_connection_is_reopened(fake_blender):
    # The fake hangs up after answering once, like Blender restarting between
    # two tool calls; the client must reconnect instead of failing.
    server = fake_blender(close_after=1)
    client = BlenderConnection(port=server.port, timeout=5)

    assert client.execute("ping")["echo"]["type"] == "ping"
    assert client.execute("ping")["echo"]["type"] == "ping"

    assert len(server.received) == 2
    client.close()


def test_large_response_is_reassembled(fake_blender):
    payload = "x" * 400_000
    server = fake_blender(responder=lambda message: {"status": "success", "result": {"blob": payload}})
    client = BlenderConnection(port=server.port, timeout=10)

    assert client.execute("execute_python")["blob"] == payload
    client.close()


def test_malformed_response_is_reported(fake_blender):
    server = fake_blender(responder=lambda message: {"status": "success", "result": {"ok": True}})
    client = BlenderConnection(port=server.port, timeout=5)
    client.execute("ping")

    # Corrupt the buffer the way a truncated/garbled frame would.
    client._buffer.extend(b"not json\n")
    with pytest.raises(BlenderConnectionError) as excinfo:
        client.execute("ping")
    assert "Malformed response" in str(excinfo.value)
    client.close()


async def test_send_command_wrapper_uses_shared_connection(fake_blender):
    server = fake_blender()
    conn.reset_connection()
    conn._connection = BlenderConnection(port=server.port, timeout=5)
    try:
        result = await conn.send_command("ping", {"a": 1})
        assert result["echo"]["params"] == {"a": 1}
    finally:
        conn.reset_connection()


async def test_tool_layer_returns_actionable_error_when_blender_is_down():
    from blender_mcp.server import blender_ping, PingInput
    from tests.conftest import free_port

    conn.reset_connection()
    conn._connection = BlenderConnection(port=free_port(), timeout=2)
    try:
        answer = await blender_ping(PingInput())
        assert answer.startswith("Error: No Blender MCP server is listening")
    finally:
        conn.reset_connection()
