"""Tests for selecting the MCP transport at the command line."""

from mysql_mcp import server


def test_main_uses_stdio_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main([])

    assert calls == [{"transport": "stdio"}]


def test_main_configures_streamable_http(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--path",
            "/api/mcp",
            "--json-response",
            "--stateless-http",
        ]
    )

    assert calls == [
        {
            "transport": "streamable-http",
            "host": "0.0.0.0",
            "port": 9000,
            "streamable_http_path": "/api/mcp",
            "json_response": True,
            "stateless_http": True,
        }
    ]
