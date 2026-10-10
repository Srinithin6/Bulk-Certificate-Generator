"""API-key authentication and the request-size limit."""

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Security
from fastapi.security import APIKeyHeader

from app import config
from app.errors import UnauthorizedError, error_body

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(_api_key_header)) -> None:
    """If API_KEY is configured, require it on the request. Disabled when unset (local dev)."""
    expected = config.API_KEY
    if expected is None:
        return
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        raise UnauthorizedError("Missing or invalid API key")


class RequestSizeLimitMiddleware:
    """Rejects bodies larger than MAX_REQUEST_BYTES with 413. Checks the Content-Length
    header first, and also counts bytes as they stream in (chunked uploads have no header).

    When the limit is hit mid-stream we send the 413 ourselves, tell the app the client
    disconnected (so it stops reading), and drop anything the app tries to send afterwards.
    (Raising an exception instead would be turned into a generic 400 by FastAPI.)"""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = config.MAX_REQUEST_BYTES
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._reject(send, limit)
            return

        received = 0
        rejected = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    rejected = True
                    await self._reject(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict[str, Any]) -> None:
            if not rejected:
                await send(message)

        await self.app(scope, limited_receive, guarded_send)

    @staticmethod
    async def _reject(send: Callable[[dict[str, Any]], Awaitable[None]], limit: int) -> None:
        body = json.dumps(error_body("payload_too_large", f"Request body exceeds the {limit} byte limit")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
