"""Per-emitter sequences stay unique when several threads number events.

The research proxy numbers events from both of its forwarding threads and, when
it stops, from its main thread (the chat's end report).
"""

from __future__ import annotations

import sys
import threading

from research.telemetry.builder import SequenceAllocator


def test_concurrent_threads_never_share_a_sequence():
    allocator = SequenceAllocator()
    seen: list[int] = []
    seen_lock = threading.Lock()

    def number() -> None:
        values = [allocator.next("acp-proxy:1") for _ in range(5000)]
        with seen_lock:
            seen.extend(values)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible
    try:
        threads = [threading.Thread(target=number) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert sorted(seen) == list(range(1, 20001))
    assert allocator.current("acp-proxy:1") == 20000
    # Emitters keep independent sequences.
    assert allocator.next("acp-proxy:2") == 1
