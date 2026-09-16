from .server import mcp


def main(argv=None):
    """Lazy compatibility wrapper for the MCP transport entrypoint."""
    from .mcp_server import main as _main

    return _main(argv)


__all__ = ["main", "mcp"]
