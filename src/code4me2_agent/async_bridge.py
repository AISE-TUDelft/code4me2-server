from __future__ import annotations

import asyncio
import concurrent.futures
from queue import Queue
from threading import Thread
from typing import Any


def run_awaitable_blocking(awaitable: Any, *, timeout_seconds: float | None = None) -> object:
    wrapped = awaitable if timeout_seconds is None else asyncio.wait_for(awaitable, timeout_seconds)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(wrapped)
    return _run_awaitable_in_worker_thread(wrapped)


def _run_awaitable_in_worker_thread(awaitable: Any) -> object:
    outcome: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            outcome.put((True, asyncio.run(awaitable)))
        except BaseException as exc:  # noqa: BLE001
            outcome.put((False, exc))

    thread = Thread(target=worker, daemon=True)
    thread.start()
    thread.join()
    succeeded, value = outcome.get()
    if succeeded:
        return value
    raise value


class EventLoopAsyncRunner:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def run(self, awaitable: Any, *, timeout_seconds: float | None = None) -> object:
        if self._loop.is_closed() or not self._loop.is_running():
            return run_awaitable_blocking(awaitable, timeout_seconds=timeout_seconds)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            raise RuntimeError("Cannot block on an ACP client call from the ACP event loop.")
        wrapped = awaitable if timeout_seconds is None else asyncio.wait_for(awaitable, timeout_seconds)
        future = asyncio.run_coroutine_threadsafe(wrapped, self._loop)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError("Timed out waiting for ACP client response.") from exc
