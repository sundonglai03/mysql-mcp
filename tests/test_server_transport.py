"""Tests for selecting the MCP transport at the command line."""

from starlette.testclient import TestClient

from mysql_mcp import server


def test_main_uses_stdio_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))

    server.main([])

    assert calls == [{"transport": "stdio"}]


def test_main_configures_streamable_http(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_run_http", lambda args: calls.append(args))

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

    assert len(calls) == 1
    args = calls[0]
    assert args.transport == "streamable-http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.path == "/api/mcp"
    assert args.json_response is True
    assert args.stateless_http is True


def test_health_is_public_when_mcp_uses_a_token():
    app = server.create_http_app(
        host="127.0.0.1",
        path="/mcp",
        json_response=True,
        stateless_http=True,
        token="secret",
    )
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/mcp", json={}).status_code == 401
