from __future__ import annotations

import asyncio
import concurrent.futures
from queue import Queue
from threading import Event, Thread
from time import monotonic
from typing import Any

_CANCEL_POLL_SECONDS = 0.25


class OperationCancelled(RuntimeError):
    """The blocking wait was abandoned because the session cancel event fired."""


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

    def run(
        self,
        awaitable: Any,
        *,
        timeout_seconds: float | None = None,
        cancel_event: Event | None = None,
    ) -> object:
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
        if cancel_event is None:
            try:
                return future.result(timeout=timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise TimeoutError("Timed out waiting for ACP client response.") from exc
        # Poll in short slices so a session cancel can abandon the wait; the
        # pending JSON-RPC request is cancelled on the loop side.
        deadline = None if timeout_seconds is None else monotonic() + timeout_seconds + 1.0
        while True:
            try:
                return future.result(timeout=_CANCEL_POLL_SECONDS)
            except concurrent.futures.TimeoutError as exc:
                if cancel_event.is_set():
                    future.cancel()
                    raise OperationCancelled(
                        "Cancelled while waiting for the ACP client."
                    ) from None
                if deadline is not None and monotonic() >= deadline:
                    future.cancel()
                    raise TimeoutError("Timed out waiting for ACP client response.") from exc
