"""Rate limiter keying behind a reverse proxy (production-readiness B-02).

With every participant reaching the API through one nginx, the limiter used to
key on the proxy's socket address and roughly nine active participants shared
one hourly quota. The key must follow ``X-Forwarded-For`` when (and only when)
the peer is a trusted proxy, and the research-plane paths every participant
polls continuously must get the high per-client floor.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.requests import Request

import main
from main import (
    METERED_INFERENCE_DEFAULT_RATE_PER_HOUR,
    SimpleRateLimiter,
    resolve_client_address,
)


def _request(path: str, peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": (peer, 40000),
        "server": ("127.0.0.1", 8008),
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
    }
    return Request(scope)


def _limiter(trusted: str) -> SimpleRateLimiter:
    return SimpleRateLimiter(FastAPI(), trusted_proxies=trusted, start_reset_thread=False)


def test_two_clients_behind_one_trusted_proxy_get_distinct_keys():
    limiter = _limiter("172.18.0.5")
    first = limiter._get_client_key(
        _request("/api/research/sessions/heartbeat", "172.18.0.5", "203.0.113.10")
    )
    second = limiter._get_client_key(
        _request("/api/research/sessions/heartbeat", "172.18.0.5", "203.0.113.11")
    )
    assert first != second
    assert first.startswith("203.0.113.10:")
    assert second.startswith("203.0.113.11:")


def test_an_untrusted_peer_cannot_spoof_its_address_with_the_header():
    limiter = _limiter("172.18.0.5")
    key = limiter._get_client_key(
        _request("/api/research/sessions/heartbeat", "198.51.100.7", "203.0.113.10")
    )
    assert key.startswith("198.51.100.7:")


def test_client_supplied_hops_left_of_the_proxy_entry_are_ignored():
    # nginx appends the address it saw; anything the client sent before it is
    # forgeable and must not become the key.
    address = resolve_client_address(
        "172.18.0.5",
        "1.2.3.4, 203.0.113.10",
        trust_all=False,
        trusted=frozenset({"172.18.0.5"}),
    )
    assert address == "203.0.113.10"


def test_a_proxy_network_can_be_trusted_by_cidr():
    address = resolve_client_address(
        "172.18.0.9",
        "203.0.113.10",
        trust_all=False,
        trusted=frozenset({"172.18.0.0/16"}),
    )
    assert address == "203.0.113.10"


def test_wildcard_trusts_every_peer_but_never_a_client_supplied_leading_hop():
    # nginx appends the address it saw; with "*" the appended (rightmost) hop is
    # the only one that is not client-controlled.
    address = resolve_client_address(
        "10.0.0.1", "9.9.9.9, 203.0.113.10", trust_all=True, trusted=frozenset()
    )
    assert address == "203.0.113.10"
    single = resolve_client_address("10.0.0.1", "203.0.113.10", trust_all=True, trusted=frozenset())
    assert single == "203.0.113.10"


def test_forged_header_cannot_choose_the_key_under_wildcard_trust():
    limiter = SimpleRateLimiter(FastAPI(), trusted_proxies="*", start_reset_thread=False)
    forged = limiter._get_client_key(
        _request("/api/research/sessions/heartbeat", "172.18.0.5", "9.9.9.9, 203.0.113.10")
    )
    honest = limiter._get_client_key(
        _request("/api/research/sessions/heartbeat", "172.18.0.5", "203.0.113.10")
    )
    assert forged == honest
    assert forged.startswith("203.0.113.10:")


def test_loopback_is_the_only_default_trusted_proxy():
    limiter = SimpleRateLimiter(FastAPI(), trusted_proxies="127.0.0.1", start_reset_thread=False)
    assert limiter.client_address(_request("/api/ping", "127.0.0.1", "203.0.113.10")) == "203.0.113.10"
    assert limiter.client_address(_request("/api/ping", "10.1.1.1", "203.0.113.10")) == "10.1.1.1"


def test_research_participant_paths_get_the_high_floor():
    limiter = _limiter("127.0.0.1")
    default = main.config.default_max_request_rate_per_hour
    floor = max(default, METERED_INFERENCE_DEFAULT_RATE_PER_HOUR)
    for path in (
        "/api/research/sessions/heartbeat",
        "/api/research/sessions/activity",
        "/api/research/sessions/",
        "/api/research/telemetry/batches",
        "/api/research/bootstrap/research-sessions",
        "/api/research/inference/v1/chat/completions",
    ):
        assert limiter._get_rate_limit(path) == floor, path
    # An ordinary, unconfigured path keeps the default quota.
    unconfigured = "/api/rate-limiter-test/unconfigured-path"
    assert unconfigured not in main.config.max_request_rate_per_hour_config
    assert limiter._get_rate_limit(unconfigured) == default


def test_reset_clears_counters_under_the_registry_lock():
    limiter = _limiter("127.0.0.1")
    limiter.request_counts["203.0.113.10:/api/ping"] = 5
    limiter.locks["203.0.113.10:/api/ping"] = object()  # type: ignore[assignment]
    limiter.reset_counts()
    assert limiter.request_counts == {}
    assert limiter.locks == {}
