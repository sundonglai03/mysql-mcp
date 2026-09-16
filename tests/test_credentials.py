from __future__ import annotations

import pytest

from mysql_mcp import credentials


def test_defaults_are_filled_without_persistence():
    result = credentials.normalize({"host": "db.internal"})

    assert result == {
        "host": "db.internal",
        "port": 3306,
        "user": "root",
        "password": "",
        "charset": "utf8mb4",
        "read_only": False,
    }


def test_all_connection_and_policy_fields_are_validated():
    result = credentials.normalize(
        {
            "host": "db.internal",
            "port": "3307",
            "user": "reader",
            "password": 123456,
            "database": "app",
            "read_only": "true",
            "max_affected_rows": "25",
            "ssl_ca": "/certs/ca.pem",
            "ssl_verify_cert": True,
        }
    )

    assert result["port"] == 3307
    assert result["password"] == "123456"
    assert result["read_only"] is True
    assert result["max_affected_rows"] == 25
    assert result["ssl_ca"] == "/certs/ca.pem"


@pytest.mark.parametrize("value", [None, [], "db", 1])
def test_credentials_must_be_an_object(value):
    with pytest.raises(credentials.CredentialError, match="object"):
        credentials.normalize(value)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_host_is_required(value):
    with pytest.raises(credentials.CredentialError, match="host"):
        credentials.normalize({"host": value})


@pytest.mark.parametrize("port", [0, 65536, "bad", True])
def test_invalid_ports_are_rejected(port):
    with pytest.raises(credentials.CredentialError, match="port"):
        credentials.normalize({"host": "db", "port": port})


def test_unknown_keys_are_rejected():
    with pytest.raises(credentials.CredentialError, match="unknown keys"):
        credentials.normalize({"host": "db", "remember": True})


def test_module_has_no_profile_or_file_api():
    for name in (
        "save_profile",
        "load_profile",
        "list_profiles",
        "delete_profile",
        "connections_file",
        "remember",
    ):
        assert not hasattr(credentials, name)
