"""Socket client that talks to the Blender MCP add-on.

The add-on speaks newline-delimited JSON over a localhost TCP socket and
processes one command at a time on Blender's main thread, so this client keeps
a single connection and serialises access to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from typing import Any, Dict, Optional

DEFAULT_HOST = os.environ.get("BLENDER_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("BLENDER_MCP_PORT", "9876"))
DEFAULT_TIMEOUT = float(os.environ.get("BLENDER_MCP_TIMEOUT", "60"))
RECV_CHUNK = 65536

_NOT_RUNNING_HINT = (
    "Make sure Blender is running, the 'Blender MCP' add-on is enabled, and the "
    "server was started in the 3D viewport sidebar (press N) > MCP tab > 'Start server'."
)


class BlenderConnectionError(RuntimeError):
    """Blender could not be reached, or the connection broke mid-command."""


class BlenderCommandError(RuntimeError):
    """Blender received the command and reported a failure."""

    def __init__(self, message: str, blender_traceback: Optional[str] = None):
        super().__init__(message)
        self.blender_traceback = blender_traceback


class BlenderConnection:
    """Blocking, thread-safe client for a single Blender instance."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._buffer = bytearray()
        self._lock = threading.Lock()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        self._buffer.clear()

    def execute(
        self,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Send one command and return the ``result`` payload from Blender.

        Raises:
            BlenderConnectionError: Blender is unreachable or dropped the link.
            BlenderCommandError: Blender ran the command and it failed.
        """
        with self._lock:
            reused = self._socket is not None
            try:
                return self._roundtrip(command, params or {}, timeout or self.timeout)
            except socket.timeout as exc:
                self.close()
                raise BlenderConnectionError(
                    f"Blender did not answer '{command}' within {timeout or self.timeout:.0f}s. "
                    "Long renders can exceed the timeout — raise it with the tool's timeout "
                    "argument or the BLENDER_MCP_TIMEOUT environment variable."
                ) from exc
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
                # A socket kept alive between calls can go stale when Blender
                # restarts; that failure happens before anything was delivered,
                # so retrying it once cannot double-apply the command.
                self.close()
                if not reused:
                    raise BlenderConnectionError(
                        f"Connection to Blender at {self.address} broke while sending "
                        f"'{command}': {exc}. {_NOT_RUNNING_HINT}"
                    ) from exc
                return self._retry_once(command, params or {}, timeout or self.timeout)
            except ConnectionRefusedError as exc:
                self.close()
                raise BlenderConnectionError(
                    f"No Blender MCP server is listening on {self.address}. {_NOT_RUNNING_HINT}"
                ) from exc
            except OSError as exc:
                self.close()
                raise BlenderConnectionError(
                    f"Socket error while talking to Blender at {self.address}: {exc}"
                ) from exc

    # -- internals ---------------------------------------------------------

    def _retry_once(self, command: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        try:
            return self._roundtrip(command, params, timeout)
        except OSError as exc:
            self.close()
            raise BlenderConnectionError(
                f"Lost the connection to Blender at {self.address} while sending "
                f"'{command}': {exc}. {_NOT_RUNNING_HINT}"
            ) from exc

    def _ensure_socket(self, timeout: float) -> socket.socket:
        if self._socket is None:
            self._buffer.clear()
            self._socket = socket.create_connection((self.host, self.port), timeout=timeout)
        self._socket.settimeout(timeout)
        return self._socket

    def _roundtrip(self, command: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        sock = self._ensure_socket(timeout)
        request = json.dumps({"type": command, "params": params}).encode("utf-8") + b"\n"
        sock.sendall(request)

        while b"\n" not in self._buffer:
            chunk = sock.recv(RECV_CHUNK)
            if not chunk:
                raise ConnectionResetError("Blender closed the connection")
            self._buffer.extend(chunk)

        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer.clear()
        self._buffer.extend(rest)

        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.close()
            raise BlenderConnectionError(f"Malformed response from Blender: {exc}") from exc

        if response.get("status") != "success":
            raise BlenderCommandError(
                response.get("message", "Blender reported an unspecified error"),
                response.get("traceback"),
            )
        return response.get("result", {})


_connection: Optional[BlenderConnection] = None
_async_lock: Optional[asyncio.Lock] = None


def get_connection() -> BlenderConnection:
    """Return the process-wide connection, creating it on first use."""
    global _connection
    if _connection is None:
        _connection = BlenderConnection()
    return _connection


def reset_connection() -> None:
    """Drop the cached connection (used by tests and on fatal errors)."""
    global _connection
    if _connection is not None:
        _connection.close()
    _connection = None


async def send_command(
    command: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Async wrapper: run one blocking command off the event loop.

    Blender handles a single command at a time, so calls are serialised.
    """
    global _async_lock
    if _async_lock is None:
        _async_lock = asyncio.Lock()
    connection = get_connection()
    async with _async_lock:
        return await asyncio.to_thread(connection.execute, command, params, timeout)
