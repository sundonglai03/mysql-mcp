"""Validation for one-shot MySQL credentials.

This module deliberately contains no file I/O. Every MCP tool receives a complete
credential object, validates it, opens one connection, and discards it afterwards.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PORT = 3306
DEFAULT_CHARSET = "utf8mb4"

_STRING_KEYS = ("host", "user", "password", "database", "charset")
_INT_KEYS = ("port", "max_affected_rows")
_BOOL_KEYS = ("read_only",)
_SSL_STRING_KEYS = ("ssl_ca", "ssl_cert", "ssl_key", "ssl_cipher")
_SSL_BOOL_KEYS = ("ssl_disabled", "ssl_verify_cert", "ssl_verify_identity")

ALLOWED_KEYS = frozenset(
    _STRING_KEYS + _INT_KEYS + _BOOL_KEYS + _SSL_STRING_KEYS + _SSL_BOOL_KEYS
)


class CredentialError(ValueError):
    """Credentials are missing or malformed."""


def _string(value: Any, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise CredentialError(f"{field} must be a string, got {type(value).__name__}")


def _boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise CredentialError(f"{field} must be a boolean, got {value!r}")


def _integer(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise CredentialError(f"{field} must be an integer")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise CredentialError(f"{field} must be an integer, got {value!r}") from exc
    if not isinstance(value, int) or not 1 <= value <= maximum:
        raise CredentialError(f"{field} must be between 1 and {maximum}")
    return value


def normalize(raw: Any) -> dict[str, Any]:
    """Validate one credential object and fill safe connection defaults."""
    if not isinstance(raw, dict):
        raise CredentialError(
            f"credentials must be an object, got {type(raw).__name__}"
        )

    unknown = sorted(set(raw) - ALLOWED_KEYS)
    if unknown:
        raise CredentialError(
            f"credentials has unknown keys: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(ALLOWED_KEYS))}"
        )

    host = _string(raw.get("host", ""), "host").strip()
    if not host:
        raise CredentialError("credentials requires a non-empty 'host'")

    result: dict[str, Any] = {
        "host": host,
        "port": _integer(raw.get("port", DEFAULT_PORT), "port", maximum=65535),
        "user": _string(raw.get("user", "root"), "user"),
        "password": _string(raw.get("password", ""), "password"),
        "charset": _string(raw.get("charset", DEFAULT_CHARSET), "charset"),
        "read_only": _boolean(raw.get("read_only", False), "read_only"),
    }

    if raw.get("database") not in (None, ""):
        result["database"] = _string(raw["database"], "database")
    if raw.get("max_affected_rows") is not None:
        result["max_affected_rows"] = _integer(
            raw["max_affected_rows"], "max_affected_rows", maximum=10_000
        )
    for key in _SSL_STRING_KEYS:
        if raw.get(key) is not None:
            result[key] = _string(raw[key], key)
    for key in _SSL_BOOL_KEYS:
        if raw.get(key) is not None:
            result[key] = _boolean(raw[key], key)
    return result
