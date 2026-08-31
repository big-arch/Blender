"""Shared fixtures: a fake Blender endpoint and the real add-on module."""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

ADDON_PATH = Path(__file__).resolve().parents[1] / "addon" / "blender_mcp_addon.py"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeBlender:
    """Minimal stand-in that speaks the add-on's newline-delimited JSON protocol."""

    def __init__(
        self,
        responder: Callable[[Dict[str, Any]], Dict[str, Any]],
        close_after: Optional[int] = None,
    ):
        self.responder = responder
        self.close_after = close_after
        self.received: List[Dict[str, Any]] = []
        self.port = free_port()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", self.port))
        self._socket.listen(5)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while self._running:
            try:
                client, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        buffer = bytearray()
        served = 0
        with client:
            while self._running:
                try:
                    chunk = client.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = bytes(buffer).partition(b"\n")
                    buffer.clear()
                    buffer.extend(rest)
                    message = json.loads(line.decode("utf-8"))
                    self.received.append(message)
                    response = self.responder(message)
                    client.sendall(json.dumps(response).encode("utf-8") + b"\n")
                    served += 1
                    if self.close_after is not None and served >= self.close_after:
                        return


@pytest.fixture
def fake_blender():
    """Factory for FakeBlender instances that are torn down after the test."""
    servers: List[FakeBlender] = []

    def make(responder=None, close_after=None) -> FakeBlender:
        if responder is None:

            def responder(message):  # noqa: ANN001 - test helper
                return {"status": "success", "result": {"echo": message}}

        server = FakeBlender(responder, close_after=close_after)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture(scope="session")
def addon():
    """Load the Blender add-on against a real ``bpy``, or skip the test."""
    pytest.importorskip("bpy", reason="install the 'bpy' package to test the add-on itself")

    spec = importlib.util.spec_from_file_location("blender_mcp_addon", ADDON_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.register()
    yield module
    module.unregister()
