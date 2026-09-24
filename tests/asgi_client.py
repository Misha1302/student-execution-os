from __future__ import annotations

import asyncio

import httpx


class TestClient:
    """Small synchronous ASGI test client compatible with Python 3.14.

    Starlette's blocking portal currently deadlocks in this environment before
    lifespan startup. Product API tests do not need a persistent lifespan, so each
    request is driven directly through HTTPX's async ASGI transport.
    """

    def __init__(self, app) -> None:
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def request(self, method: str, path: str, **kwargs):
        async def run():
            transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=True)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(run())

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def options(self, path: str, **kwargs):
        return self.request("OPTIONS", path, **kwargs)
