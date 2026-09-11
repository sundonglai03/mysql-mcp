"""Small optional Bearer-token middleware for the HTTP transport."""


class BearerTokenMiddleware:
    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        authorization = next(
            (value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"authorization"),
            None,
        )
        if authorization != self.expected:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": b"Unauthorized"})
            return

        await self.app(scope, receive, send)
