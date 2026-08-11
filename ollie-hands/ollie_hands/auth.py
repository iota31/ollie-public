"""Small ASGI auth boundary shared by MCP and owner-consent routes."""

from __future__ import annotations

import hmac


class BearerMiddleware:
    """Route-scoped credentials: approval authority is not MCP authority."""

    def __init__(self, app, token: str, approval_token: str, audit=None) -> None:
        self.app = app
        self.expected = f"Bearer {token}"
        self.approval_expected = f"Bearer {approval_token}"
        self.audit = audit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        expected = (self.approval_expected
                    if scope.get("path", "").rstrip("/") in {"/consent", "/mode"}
                    else self.expected)
        if not hmac.compare_digest(auth, expected):
            client = scope.get("client") or ("?", 0)
            if self.audit is not None:
                self.audit.event("auth", status="denied",
                                 detail=f"bad/missing bearer from {client[0]}")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)
