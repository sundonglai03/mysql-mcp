import os
import re
from typing import Any

import mysql.connector
from mysql.connector import Error as MySQLError
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("mysql-mcp")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def get_connection_config() -> dict[str, Any]:
    db_name = os.getenv("MYSQL_DATABASE") or os.getenv("MYSQL_DB")
    if not db_name:
        raise ValueError(
            "MYSQL_DATABASE (or MYSQL_DB) is required. Set it before starting the service."
        )

    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": db_name,
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "autocommit": True,
    }


def get_connection():
    return mysql.connector.connect(**get_connection_config())


def _safe_identifier(value: str, field_name: str) -> str:
    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    candidate = value.strip()
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise ValueError(f"{field_name} must be a valid SQL identifier: {value!r}")
    return candidate


@mcp.tool()
def health_check() -> dict[str, Any]:
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        return {
            "ok": True,
            "database": conn.database,
            "result": result,
        }
    except (MySQLError, ValueError) as exc:
        return {
            "ok": False,
            "error": str(exc),
        }
    finally:
        if conn is not None and conn.is_connected():
            conn.close()


@mcp.tool()
def list_tables() -> list[str]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW TABLES")
        rows = cursor.fetchall()
        return [str(row[0]) if isinstance(row, (tuple, list)) else str(row) for row in rows]
    finally:
        conn.close()


@mcp.tool()
def get_databases() -> list[str]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SHOW DATABASES")
        rows = cursor.fetchall()
        return [str(row[0]) if isinstance(row, (tuple, list)) else str(row) for row in rows]
    finally:
        conn.close()


@mcp.tool()
def fetch_table(table_name: str, limit: int = 50, where: str | None = None, order_by: str | None = None) -> dict[str, Any]:
    safe_table = _safe_identifier(table_name, "table_name")
    sql = f"SELECT * FROM `{safe_table}`"
    params: list[Any] = []

    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    sql += " LIMIT %s"
    params.append(limit)

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        first_row = rows[0] if rows else {}
        columns = list(first_row.keys()) if isinstance(first_row, dict) else []
        return {
            "table": safe_table,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }
    finally:
        conn.close()


@mcp.tool()
def count_rows(table_name: str, where: str | None = None) -> dict[str, Any]:
    safe_table = _safe_identifier(table_name, "table_name")
    sql = f"SELECT COUNT(*) AS total FROM `{safe_table}`"
    params: list[Any] = []
    if where:
        sql += f" WHERE {where}"

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, params)
        result = cursor.fetchone()
        if isinstance(result, dict):
            total = result.get("total", 0)
        elif isinstance(result, (tuple, list)):
            total = result[0] if result else 0
        else:
            total = 0
        return {"table": safe_table, "total": total}
    finally:
        conn.close()


@mcp.tool()
def insert_row(table_name: str, row: dict[str, Any]) -> dict[str, Any]:
    if not row:
        raise ValueError("row cannot be empty")

    safe_table = _safe_identifier(table_name, "table_name")
    columns = [
        _safe_identifier(str(column_name), "column_name")
        for column_name in row.keys()
    ]
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(f"`{column}`" for column in columns)
    sql = f"INSERT INTO `{safe_table}` ({column_sql}) VALUES ({placeholders})"

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, list(row.values()))
        conn.commit()
        return {
            "table": safe_table,
            "inserted": cursor.rowcount,
            "columns": columns,
        }
    finally:
        conn.close()


@mcp.tool()
def update_rows(table_name: str, updates: dict[str, Any], where: str, where_params: list[Any] | None = None) -> dict[str, Any]:
    if not updates:
        raise ValueError("updates cannot be empty")
    if not where or not where.strip():
        raise ValueError("where cannot be empty")

    safe_table = _safe_identifier(table_name, "table_name")
    set_clause = ", ".join(f"`{_safe_identifier(str(column), 'column_name')}` = %s" for column in updates.keys())
    params = list(updates.values()) + (where_params or [])
    sql = f"UPDATE `{safe_table}` SET {set_clause} WHERE {where}"

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return {"table": safe_table, "updated": cursor.rowcount}
    finally:
        conn.close()


@mcp.tool()
def delete_rows(table_name: str, where: str, where_params: list[Any] | None = None) -> dict[str, Any]:
    if not where or not where.strip():
        raise ValueError("where cannot be empty")

    safe_table = _safe_identifier(table_name, "table_name")
    sql = f"DELETE FROM `{safe_table}` WHERE {where}"
    params = where_params or []

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        return {"table": safe_table, "deleted": cursor.rowcount}
    finally:
        conn.close()


@mcp.tool()
def execute_sql(sql: str, limit: int = 50, read_only: bool = True) -> dict[str, Any]:
    return execute_query(sql=sql, limit=limit, read_only=read_only)


@mcp.tool()
def describe_table(table_name: str) -> list[dict[str, Any]]:
    if not table_name or not table_name.strip():
        raise ValueError("table_name cannot be empty")

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT COLUMN_NAME AS column_name,
                   DATA_TYPE AS data_type,
                   IS_NULLABLE AS is_nullable,
                   COLUMN_DEFAULT AS column_default,
                   EXTRA AS extra
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ORDINAL_POSITION
            """,
            (conn.database, table_name),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows if isinstance(row, dict)]
    finally:
        conn.close()


@mcp.tool()
def execute_query(sql: str, limit: int = 50, read_only: bool = True) -> dict[str, Any]:
    statement = (sql or "").strip()
    if not statement:
        raise ValueError("sql cannot be empty")

    normalized = statement.lstrip().upper()
    if read_only and not any(
        normalized.startswith(prefix) for prefix in ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN")
    ):
        raise ValueError("read_only mode only allows SELECT/SHOW/DESCRIBE/EXPLAIN queries")

    conn = get_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(statement)

        if cursor.description:
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchmany(limit)
            return {
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "read_only": read_only,
            }

        return {
            "rows_affected": cursor.rowcount,
            "read_only": read_only,
        }
    finally:
        conn.close()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
