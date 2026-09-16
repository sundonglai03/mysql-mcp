"""Low-level MySQL transport helpers.

This module deliberately knows nothing about MCP tools or credential storage;
tool policies.  It is the database equivalent of ``ssh_mcp.server.RemoteClient``:
the MCP layer decides *what* is allowed, while this module only opens, configures,
and closes a MySQL connection.
"""

from typing import Any

import mysql.connector
from mysql.connector import Error as MySQLError


class MySQLClient:
    """Small lifecycle wrapper around ``mysql.connector``."""

    def __init__(self, config: dict[str, Any]):
        self.config = dict(config)
        self.connection: Any | None = None

    def connect(self) -> Any:
        if self.connection is None:
            self.connection = mysql.connector.connect(**self.config)
        return self.connection

    def apply_query_timeout(self, timeout_ms: int) -> None:
        """Best-effort MySQL query timeout for this connection."""
        self.apply_query_timeout_to(self.connect(), timeout_ms)

    @staticmethod
    def apply_query_timeout_to(conn: Any, timeout_ms: int) -> None:
        """Apply a timeout to an already-open raw connection."""
        cursor = conn.cursor()
        try:
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
        except MySQLError:
            # MariaDB and some MySQL-compatible servers use another variable.
            pass
        finally:
            try:
                cursor.close()
            except MySQLError:
                pass

    def close(self) -> None:
        if self.connection is None:
            return
        try:
            self._close_raw(self.connection)
        finally:
            self.connection = None

    @staticmethod
    def _close_raw(conn: Any) -> None:
        try:
            conn.close()
        except MySQLError:
            pass


__all__ = ["MySQLClient", "MySQLError"]
