from __future__ import annotations

import asyncio
import concurrent.futures
import queue as _queue
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


def _run_accepts_timeout(run_method: Any) -> bool:
    """Whether a runner's ``run`` accepts the ``timeout_seconds`` keyword.

    The slicing poll needs it; older single-arg bridges (and test doubles)
    only take the awaitable and are driven with one blocking call instead.
    """
    import inspect as _inspect

    try:
        parameters = _inspect.signature(run_method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == _inspect.Parameter.VAR_KEYWORD
        or param.name == "timeout_seconds"
        for param in parameters
    )


def run_with_cancellation(
    awaitable: Any,
    runner: Any | None,
    cancel_event: Any | None,
    *,
    timeout_seconds: float | None = None,
    slice_seconds: float = 0.2,
) -> object:
    """Run an awaitable via ``runner`` in a single cancellable attempt.

    Submits the awaitable to ``runner`` exactly once (a coroutine object can
    only be awaited once — re-submitting it across slices fails on the second
    run) and polls the blocking runner call in ``slice_seconds`` increments
    so a set ``cancel_event`` or an expired overall deadline aborts promptly.
    Raises ``TimeoutError`` on overall timeout and ``asyncio.CancelledError``
    when cancelled. Never swallows the awaitable's own errors. Keeps the
    existing keep-to-thread pattern (no worker process).
    """
    import time as _time

    def _cancelled() -> bool:
        try:
            return bool(cancel_event is not None and cancel_event.is_set())
        except Exception:
            return False

    def _close_awaitable() -> None:
        try:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    if _cancelled():
        _close_awaitable()
        raise asyncio.CancelledError("Cancelled before ACP client call.")
    deadline = None if timeout_seconds is None else _time.monotonic() + timeout_seconds
    if runner is None:
        # No event-loop runner: blocking run with a pre/post cancel check.
        # Long waits here are bounded by the caller's own timeout.
        remaining = None if deadline is None else max(0.0, deadline - _time.monotonic())
        result = run_awaitable_blocking(awaitable, timeout_seconds=remaining)
        if _cancelled():
            raise asyncio.CancelledError("Cancelled during ACP client call.")
        return result
    run_method = getattr(runner, "run", None)
    if not callable(run_method):
        result = run_awaitable_blocking(awaitable)
        if _cancelled():
            raise asyncio.CancelledError("Cancelled during ACP client call.")
        return result
    if not _run_accepts_timeout(run_method):
        # Runner without slice support (e.g. a bare run(awaitable) bridge):
        # single blocking run with pre/post cancellation checks, since there
        # is no per-slice timeout to poll the cancel event against.
        result = run_method(awaitable)
        if _cancelled():
            raise asyncio.CancelledError("Cancelled during ACP client call.")
        if deadline is not None and _time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for ACP client response.")
        return result
    # Slice-capable runner: single submission, poll the blocking call.
    # The worker thread owns the one runner.run() invocation; this thread
    # only waits on the outcome queue in slices so cancel/deadline abort
    # promptly without ever re-awaiting the same coroutine object.
    outcome: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def _worker() -> None:
        try:
            outcome.put((True, run_method(awaitable, timeout_seconds=timeout_seconds)))
        except BaseException as exc:  # noqa: BLE001
            outcome.put((False, exc))

    worker_thread = Thread(target=_worker, daemon=True)
    worker_thread.start()
    poll_slice = slice_seconds if slice_seconds > 0 else 0.2
    while True:
        if _cancelled():
            _close_awaitable()
            raise asyncio.CancelledError("Cancelled during ACP client call.")
        remaining = None if deadline is None else max(0.0, deadline - _time.monotonic())
        if remaining is not None and remaining <= 0:
            raise TimeoutError("Timed out waiting for ACP client response.")
        wait_timeout = poll_slice if remaining is None else min(poll_slice, remaining)
        try:
            succeeded, value = outcome.get(timeout=wait_timeout)
        except _queue.Empty:
            # Slice expired without a result — re-poll cancel/timeout.
            continue
        if succeeded:
            return value
        raise value
