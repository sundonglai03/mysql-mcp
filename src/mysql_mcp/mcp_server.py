"""MCP transport entrypoint for mysql-mcp.

Tool implementations remain in :mod:`mysql_mcp.server`; this thin module makes
the transport boundary explicit and mirrors the SSH project's entrypoint.
"""

from .server import main, mcp

__all__ = ["main", "mcp"]


if __name__ == "__main__":
    main()
