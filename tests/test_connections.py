"""Unit tests for the connection-profile store."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mysql_mcp import connections


def write_raw(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_connections_file_follows_the_env_override(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "custom.json"
    monkeypatch.setenv("MYSQL_CONNECTIONS_FILE", str(target))

    assert connections.connections_file() == target


def test_connections_file_defaults_under_the_home_directory(monkeypatch):
    monkeypatch.delenv("MYSQL_CONNECTIONS_FILE", raising=False)

    assert (
        connections.connections_file()
        == Path("~/.mysql-mcp/connections.json").expanduser()
    )


def test_a_missing_file_reads_as_no_connections(isolated_connections_file):
    assert not isolated_connections_file.exists()

    assert connections.list_profiles() == []
    with pytest.raises(connections.ProfileError, match="No connections are saved yet"):
        connections.load_profile("anything")


def test_empty_file_reads_as_no_connections(isolated_connections_file):
    write_raw(isolated_connections_file, "   \n")

    assert connections.list_profiles() == []


def test_save_then_load_fills_in_defaults(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5"})

    name, profile = connections.load_profile("demo")

    assert name == "demo"
    assert profile["host"] == "10.0.0.5"
    assert profile["port"] == connections.DEFAULT_PORT
    assert profile["user"] == "root"
    assert profile["charset"] == connections.DEFAULT_CHARSET
    assert profile["password"] == ""


def test_optional_fields_are_omitted_when_not_given(isolated_connections_file):
    connections.save_profile("demo", {"host": "h"})

    _, profile = connections.load_profile("demo")

    assert "database" not in profile
    assert "read_only" not in profile
    assert "max_affected_rows" not in profile
    assert "description" not in profile


def test_saved_file_is_private(isolated_connections_file):
    connections.save_profile("demo", {"host": "h", "password": "s3cret"})

    assert stat.S_IMODE(isolated_connections_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(isolated_connections_file.parent.stat().st_mode) == 0o700


def test_saving_the_same_name_and_server_updates_in_place(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5", "password": "old"})
    connections.save_profile("demo", {"host": "10.0.0.5", "password": "new"})

    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))

    assert list(stored) == ["demo"]
    assert stored["demo"]["password"] == "new"


def test_repointing_a_name_at_another_host_is_refused(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5"})

    with pytest.raises(connections.ProfileConflict) as excinfo:
        connections.save_profile("demo", {"host": "10.0.0.9"})

    assert "overwrite=true" in str(excinfo.value)
    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))
    assert stored["demo"]["host"] == "10.0.0.5"


def test_a_conflict_also_triggers_on_a_different_user(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5", "user": "reader"})

    with pytest.raises(connections.ProfileConflict):
        connections.save_profile("demo", {"host": "10.0.0.5", "user": "admin"})


def test_overwrite_true_repoints_deliberately(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5"})
    connections.save_profile("demo", {"host": "10.0.0.9"}, overwrite=True)

    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))

    assert stored["demo"]["host"] == "10.0.0.9"


def test_a_re_save_keeps_policy_keys_it_does_not_mention(isolated_connections_file):
    connections.save_profile(
        "demo", {"host": "10.0.0.5", "read_only": True, "max_affected_rows": 5}
    )
    connections.save_profile("demo", {"host": "10.0.0.5", "password": "rotated"})

    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))

    assert stored["demo"]["read_only"] is True
    assert stored["demo"]["max_affected_rows"] == 5
    assert stored["demo"]["password"] == "rotated"


def test_read_only_can_still_be_lifted_explicitly(isolated_connections_file):
    connections.save_profile("demo", {"host": "10.0.0.5", "read_only": True})
    connections.save_profile("demo", {"host": "10.0.0.5", "read_only": False})

    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))

    assert stored["demo"]["read_only"] is False


def test_list_profiles_never_exposes_passwords(isolated_connections_file):
    connections.save_profile("demo", {"host": "h", "password": "s3cret"})

    entry = connections.list_profiles()[0]

    assert entry["name"] == "demo"
    assert entry["has_password"] is True
    assert "password" not in entry


def test_list_profiles_reports_tls_usage_without_paths(isolated_connections_file):
    connections.save_profile("demo", {"host": "h", "ssl_ca": "/etc/ca.pem"})

    entry = connections.list_profiles()[0]

    assert entry["uses_tls"] is True
    assert "ssl_ca" not in entry


def test_entries_are_sorted_by_name(isolated_connections_file):
    for name in ("zeta", "alpha", "mid"):
        connections.save_profile(name, {"host": "h"})

    assert [entry["name"] for entry in connections.list_profiles()] == [
        "alpha",
        "mid",
        "zeta",
    ]


def test_delete_removes_the_entry(isolated_connections_file):
    connections.save_profile("demo", {"host": "h"})

    removed = connections.delete_profile("demo")

    assert removed["host"] == "h"
    assert connections.list_profiles() == []


def test_delete_of_an_unknown_name_is_rejected(isolated_connections_file):
    connections.save_profile("demo", {"host": "h"})

    with pytest.raises(connections.ProfileError, match="Unknown connection"):
        connections.delete_profile("ghost")


def test_unknown_name_lists_the_available_connections(isolated_connections_file):
    connections.save_profile("mydb", {"host": "h"})
    connections.save_profile("warehouse", {"host": "h"})

    with pytest.raises(connections.ProfileError) as excinfo:
        connections.load_profile("missing")

    message = str(excinfo.value)
    assert "mydb" in message
    assert "warehouse" in message


def test_underscore_keys_are_treated_as_comments(isolated_connections_file):
    write_raw(
        isolated_connections_file,
        {
            "_comment": "ignore me",
            "demo": {"host": "h"},
        },
    )

    assert [entry["name"] for entry in connections.list_profiles()] == ["demo"]


def test_invalid_json_is_reported_with_the_path(isolated_connections_file):
    write_raw(isolated_connections_file, "{not json")

    with pytest.raises(connections.ProfileError, match="not valid JSON"):
        connections.list_profiles()


def test_a_non_object_document_is_rejected(isolated_connections_file):
    write_raw(isolated_connections_file, '["demo"]')

    with pytest.raises(connections.ProfileError, match="JSON object"):
        connections.list_profiles()


def test_list_marks_invalid_entries_instead_of_failing(isolated_connections_file):
    write_raw(
        isolated_connections_file,
        {"broken": {"port": 123}, "ok": {"host": "h"}},
    )

    entries = {entry["name"]: entry for entry in connections.list_profiles()}

    assert entries["broken"]["valid"] is False
    assert "host" in entries["broken"]["error"]
    assert entries["ok"]["valid"] is True


def test_an_invalid_stored_entry_fails_loudly_on_use(isolated_connections_file):
    write_raw(isolated_connections_file, {"broken": {"port": 123}})

    with pytest.raises(connections.ProfileError, match="requires a 'host'"):
        connections.load_profile("broken")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "_hidden",
        "with space",
        "slash/name",
        "a" * 65,
        "has:colon",
        123,
        None,
    ],
)
def test_invalid_names_are_rejected(name):
    with pytest.raises(connections.ProfileError):
        connections.validate_name(name)


@pytest.mark.parametrize("name", ["mydb", "my-db", "eu.west", "A1_2", "x"])
def test_valid_names_are_accepted(name):
    assert connections.validate_name(name) == name


def test_names_are_trimmed():
    assert connections.validate_name("  mydb  ") == "mydb"


def test_unknown_keys_are_rejected(isolated_connections_file):
    with pytest.raises(connections.ProfileError, match="unknown keys"):
        connections.save_profile("demo", {"host": "h", "usernme": "typo"})


def test_host_is_required(isolated_connections_file):
    with pytest.raises(connections.ProfileError, match="requires a 'host'"):
        connections.save_profile("demo", {})


def test_blank_host_is_rejected(isolated_connections_file):
    with pytest.raises(connections.ProfileError, match="non-empty 'host'"):
        connections.save_profile("demo", {"host": "   "})


@pytest.mark.parametrize("port", [0, -1, 70000, "abc", True])
def test_invalid_ports_are_rejected(isolated_connections_file, port):
    with pytest.raises(connections.ProfileError, match="port"):
        connections.save_profile("demo", {"host": "h", "port": port})


def test_numeric_strings_are_accepted_for_numeric_fields():
    profile = connections.normalize_profile(
        "demo", {"host": "h", "port": "3307", "max_affected_rows": "42"}
    )

    assert profile["port"] == 3307
    assert profile["max_affected_rows"] == 42


def test_a_numeric_password_is_coerced_to_a_string():
    profile = connections.normalize_profile("demo", {"host": "h", "password": 987654})

    assert profile["password"] == "987654"


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", True])
def test_read_only_accepts_truthy_forms(value):
    profile = connections.normalize_profile("demo", {"host": "h", "read_only": value})

    assert profile["read_only"] is True


@pytest.mark.parametrize("value", ["false", "no", "off", "0", False])
def test_read_only_accepts_falsy_forms(value):
    profile = connections.normalize_profile("demo", {"host": "h", "read_only": value})

    assert profile["read_only"] is False


def test_nonsense_read_only_is_rejected(isolated_connections_file):
    with pytest.raises(connections.ProfileError, match="read_only"):
        connections.save_profile("demo", {"host": "h", "read_only": "maybe"})


@pytest.mark.parametrize("value", [0, -3, "zero"])
def test_max_affected_rows_must_be_positive(isolated_connections_file, value):
    with pytest.raises(connections.ProfileError, match="max_affected_rows"):
        connections.save_profile("demo", {"host": "h", "max_affected_rows": value})


def test_any_host_can_be_saved(isolated_connections_file):
    """No allowlist any more: saving and loading a host are unrestricted."""
    connections.save_profile("remote", {"host": "attacker.example"})

    assert connections.load_profile("remote")[1]["host"] == "attacker.example"


def test_saving_keeps_unrelated_entries(isolated_connections_file):
    connections.save_profile("one", {"host": "h1"})
    connections.save_profile("two", {"host": "h2"})

    stored = json.loads(isolated_connections_file.read_text(encoding="utf-8"))

    assert sorted(stored) == ["one", "two"]
