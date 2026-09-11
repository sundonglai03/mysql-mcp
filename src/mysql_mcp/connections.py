"""Named connection profiles.

Profiles live in one JSON file — ``~/.mysql-mcp/connections.json`` by default,
overridable with ``MYSQL_CONNECTIONS_FILE`` — so a single server process can serve
several databases while the model only ever passes a *name*, never credentials.

The file is writable by the model through the ``save_connection`` tool, and also
written automatically when a tool is called with inline ``credentials`` for a name
that has no profile yet: the connection is dialled first and only stored once it
succeeded. Either way the file is treated as untrusted input and each profile is
validated both on load and on use.

The only environment variable read here is ``MYSQL_CONNECTIONS_FILE``, which moves
the file itself; connection details never come from the environment.

File shape (``_``-prefixed keys are ignored, so the file can carry comments)::

    {
      "_comment": "connection profiles for mysql-mcp",
      "mydb": {
        "host": "db.example.com",
        "port": 3306,
        "user": "root",
        "password": "…",
        "database": "mydb",
        "read_only": true,
        "description": "read-only replica"
      }
    }
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_CONNECTIONS_FILE = "~/.mysql-mcp/connections.json"
DEFAULT_PORT = 3306
DEFAULT_CHARSET = "utf8mb4"

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

_TRUTHY = {"1", "true", "yes", "y", "on"}
_FALSY = {"0", "false", "no", "n", "off"}

_STRING_KEYS = ("host", "user", "password", "database", "charset", "description")
_INT_KEYS = ("port", "max_affected_rows")
_BOOL_KEYS = ("read_only",)
_SSL_STRING_KEYS = ("ssl_ca", "ssl_cert", "ssl_key", "ssl_cipher")
_SSL_BOOL_KEYS = ("ssl_disabled", "ssl_verify_cert", "ssl_verify_identity")

ALLOWED_KEYS = frozenset(
    _STRING_KEYS + _INT_KEYS + _BOOL_KEYS + _SSL_STRING_KEYS + _SSL_BOOL_KEYS
)
SSL_KEYS = frozenset(_SSL_STRING_KEYS + _SSL_BOOL_KEYS)


class ProfileError(ValueError):
    """A connection profile is missing, malformed or not permitted."""


class ProfileConflict(ProfileError):
    """A name already points at a different server, so it will not be repointed."""


def identity(profile: dict[str, Any]) -> tuple[str, int, str]:
    """The part of a profile that says *which server* it addresses."""
    return (
        profile["host"].lower(),
        profile.get("port", DEFAULT_PORT),
        profile.get("user", "root"),
    )


def describe_identity(profile: dict[str, Any]) -> str:
    host, port, user = identity(profile)
    return f"{host}:{port} as user {user!r}"


def connections_file() -> Path:
    raw = os.getenv("MYSQL_CONNECTIONS_FILE") or DEFAULT_CONNECTIONS_FILE
    return Path(raw).expanduser()


def validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ProfileError(
            f"connection name must be a string, got {type(name).__name__}"
        )
    candidate = name.strip()
    if candidate.startswith("_"):
        raise ProfileError("connection names cannot start with '_' (reserved)")
    if not PROFILE_NAME_RE.match(candidate):
        raise ProfileError(
            "connection name must start with a letter, digit or underscore and may "
            "only contain letters, digits, '_', '-' and '.', max 64 characters"
        )
    return candidate


def _coerce_str(value: Any, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ProfileError(f"{field} must be a string, got {type(value).__name__}")


def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
    raise ProfileError(f"{field} must be a boolean (true/false), got {value!r}")


def _coerce_port(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProfileError(f"{field} must be an integer, got a boolean")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise ProfileError(f"{field} must be an integer, got {value!r}") from exc
    if not isinstance(value, int):
        raise ProfileError(f"{field} must be an integer, got {type(value).__name__}")
    if not 1 <= value <= 65535:
        raise ProfileError(f"{field} must be between 1 and 65535, got {value}")
    return value


def _coerce_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProfileError(f"{field} must be a positive integer, got a boolean")
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as exc:
            raise ProfileError(
                f"{field} must be a positive integer, got {value!r}"
            ) from exc
    if not isinstance(value, int):
        raise ProfileError(
            f"{field} must be a positive integer, got {type(value).__name__}"
        )
    if value < 1:
        raise ProfileError(f"{field} must be a positive integer, got {value}")
    return value


def normalize_profile(name: str, raw: Any) -> dict[str, Any]:
    """Validate one raw profile object and return it with defaults filled in."""
    if not isinstance(raw, dict):
        raise ProfileError(f"Connection {name!r} must be a JSON object")

    unknown = sorted(set(raw) - ALLOWED_KEYS - {"_comment"})
    if unknown:
        raise ProfileError(
            f"Connection {name!r} has unknown keys: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(ALLOWED_KEYS))}"
        )

    if "host" not in raw:
        raise ProfileError(f"Connection {name!r} requires a 'host'")
    host = _coerce_str(raw["host"], "host").strip()
    if not host:
        raise ProfileError(f"Connection {name!r} requires a non-empty 'host'")

    profile: dict[str, Any] = {
        "host": host,
        "port": _coerce_port(raw.get("port", DEFAULT_PORT), "port"),
        "user": _coerce_str(raw.get("user", "root"), "user"),
        "password": _coerce_str(raw.get("password", ""), "password"),
        "charset": _coerce_str(raw.get("charset", DEFAULT_CHARSET), "charset"),
    }

    if raw.get("database") is not None:
        database = _coerce_str(raw["database"], "database").strip()
        if database:
            profile["database"] = database
    if raw.get("description") is not None:
        profile["description"] = _coerce_str(raw["description"], "description")
    if raw.get("read_only") is not None:
        profile["read_only"] = _coerce_bool(raw["read_only"], "read_only")
    if raw.get("max_affected_rows") is not None:
        profile["max_affected_rows"] = _coerce_positive_int(
            raw["max_affected_rows"], "max_affected_rows"
        )

    for key in _SSL_STRING_KEYS:
        if raw.get(key) is not None:
            profile[key] = _coerce_str(raw[key], key)
    for key in _SSL_BOOL_KEYS:
        if raw.get(key) is not None:
            profile[key] = _coerce_bool(raw[key], key)

    return profile


def _read_raw() -> dict[str, Any]:
    path = connections_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ProfileError(f"Cannot read {path}: {exc}") from exc

    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"{path} must contain a JSON object of name -> profile")
    return data


def _write_raw(data: dict[str, Any]) -> None:
    path = connections_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".connections-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def list_profiles() -> list[dict[str, Any]]:
    """Return every stored profile, without passwords, plus per-entry validity."""
    raw = _read_raw()
    results: list[dict[str, Any]] = []
    for name in sorted(key for key in raw if not key.startswith("_")):
        try:
            profile = normalize_profile(name, raw[name])
        except ProfileError as exc:
            results.append({"name": name, "valid": False, "error": str(exc)})
            continue
        entry: dict[str, Any] = {
            key: value
            for key, value in profile.items()
            if key not in {"password"} and key not in SSL_KEYS
        }
        entry["name"] = name
        entry["valid"] = True
        entry["has_password"] = bool(profile.get("password"))
        entry["uses_tls"] = any(key in profile for key in SSL_KEYS)
        results.append(entry)
    return results


def load_profile(name: Any) -> tuple[str, dict[str, Any]]:
    """Validate and return ``(name, profile)`` for one stored connection."""
    clean = validate_name(name)
    raw = _read_raw()
    if clean not in raw:
        available = sorted(key for key in raw if not key.startswith("_"))
        if available:
            hint = f"Available connections: {', '.join(available)}."
        else:
            hint = "No connections are saved yet; create one with save_connection."
        raise ProfileError(f"Unknown connection {clean!r}. {hint}")
    profile = normalize_profile(clean, raw[clean])
    return clean, profile


def find_profile(name: Any) -> dict[str, Any] | None:
    """Like :func:`load_profile` but returns ``None`` instead of raising when absent."""
    clean = validate_name(name)
    raw = _read_raw()
    if clean not in raw:
        return None
    profile = normalize_profile(clean, raw[clean])
    return profile


# Policy keys a re-save must not silently drop: when new fields do not mention
# them, whatever the stored entry already says wins.
_STICKY_KEYS = ("read_only", "max_affected_rows", "description")


def save_profile(
    name: Any, fields: dict[str, Any], *, overwrite: bool = False
) -> dict[str, Any]:
    """Validate and persist one profile.

    An existing name is only replaced when the new entry addresses the same
    server (host/port/user) or when ``overwrite`` is set. Quietly repointing a
    name at another host is how a saved profile gets hijacked — every later call
    using that name would silently query the wrong database — so it is refused.
    """
    clean = validate_name(name)
    candidate = {key: value for key, value in fields.items() if value is not None}
    profile = normalize_profile(clean, candidate)

    data = _read_raw()
    stored = data.get(clean)
    if stored is not None and not overwrite:
        try:
            current = normalize_profile(clean, stored)
        except ProfileError as exc:
            raise ProfileConflict(
                f"Connection {clean!r} exists in {connections_file()} but is not valid "
                f"({exc}). Pass overwrite=true to replace it."
            ) from exc
        if identity(current) != identity(profile):
            raise ProfileConflict(
                f"Connection {clean!r} already points at {describe_identity(current)}; "
                f"refusing to repoint it at {describe_identity(profile)}. Pass overwrite=true "
                "if that is really intended, or save it under a different name."
            )
        for key in _STICKY_KEYS:
            if key not in candidate and key in current:
                profile[key] = current[key]

    data[clean] = profile
    _write_raw(data)
    return profile


def delete_profile(name: Any) -> dict[str, Any]:
    """Remove one profile; fails when the name is unknown."""
    clean = validate_name(name)
    data = _read_raw()
    if clean not in data:
        available = sorted(key for key in data if not key.startswith("_"))
        hint = f"Available connections: {', '.join(available)}." if available else ""
        raise ProfileError(f"Unknown connection {clean!r}. {hint}".strip())
    removed = normalize_profile(clean, data[clean])
    del data[clean]
    _write_raw(data)
    return removed
