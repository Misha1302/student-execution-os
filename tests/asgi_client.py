from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sys
import threading

import fastapi.dependencies.utils
import fastapi.routing
import httpx
import starlette.concurrency
import starlette.routing


if sys.version_info >= (3, 14):
    # Some CPython 3.14 builds do not wake an asyncio selector when a delayed
    # executor future completes. This test client already exists specifically for
    # 3.14 compatibility; keep the workaround here rather than in production code.
    _EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="test-asgi")
    _SLOTS = threading.BoundedSemaphore(32)

    async def _run_in_threadpool(func, *args, **kwargs):
        while not _SLOTS.acquire(blocking=False):
            await asyncio.sleep(0.001)

        def invoke():
            try:
                return func(*args, **kwargs)
            finally:
                _SLOTS.release()

        try:
            future = _EXECUTOR.submit(invoke)
        except BaseException:
            _SLOTS.release()
            raise
        while not future.done():
            await asyncio.sleep(0.001)
        return future.result()

    fastapi.routing.run_in_threadpool = _run_in_threadpool
    fastapi.dependencies.utils.run_in_threadpool = _run_in_threadpool
    starlette.concurrency.run_in_threadpool = _run_in_threadpool
    starlette.routing.run_in_threadpool = _run_in_threadpool

    # app.py imports the helper directly, so replace that module-local reference too.
    import student_execution_os.web.app as _web_app
    _web_app.run_in_threadpool = _run_in_threadpool


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

    def put(self, path: str, **kwargs):
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    def options(self, path: str, **kwargs):
        return self.request("OPTIONS", path, **kwargs)
