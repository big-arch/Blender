"""MCP server that drives a running Blender instance."""

from .connection import (
    BlenderCommandError,
    BlenderConnection,
    BlenderConnectionError,
    get_connection,
    reset_connection,
    send_command,
)

__version__ = "1.0.0"

__all__ = [
    "BlenderCommandError",
    "BlenderConnection",
    "BlenderConnectionError",
    "get_connection",
    "reset_connection",
    "send_command",
    "__version__",
]
