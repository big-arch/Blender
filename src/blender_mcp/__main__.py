"""Allow ``python -m blender_mcp`` to start the stdio MCP server."""

from .server import main

if __name__ == "__main__":
    main()
