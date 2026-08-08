"""Tests for the shared HTTP client: retries, backoff, caching, conditional GETs.

Uses httpx's MockTransport so nothing touches the network.
"""

from __future__ import annotations

import random

import httpx
import pytest

from keyword_scrub.errors import RateLimited, SourceError
from keyword_scrub.http import HttpClient


def make_client(handler, **kwargs) -> HttpClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, headers={"User-Agent": "test"})
    # Deterministic backoff and no real sleeping.
    return HttpClient(
        user_agent="test",
        client=inner,
        sleep=lambda s: None,
        rng=random.Random(0),
        **kwargs,
    )


def test_get_returns_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with make_client(handler) as c:
        r = c.get("https://a.4cdn.org/boards.json")
        assert r.json() == {"ok": True}


def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    with make_client(handler, max_retries=3) as c:
        r = c.get("https://x/1")
        assert r.status_code == 200
        assert calls["n"] == 3


def test_429_exhausted_raises_ratelimited_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "7"})

    with make_client(handler, max_retries=1) as c:
        with pytest.raises(RateLimited) as ei:
            c.get("https://x/1", source="reddit")
        assert ei.value.retry_after == 7.0
        assert ei.value.source == "reddit"


def test_persistent_500_raises_sourceerror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with make_client(handler, max_retries=1) as c:
        with pytest.raises(SourceError):
            c.get("https://x/1")


def test_transport_error_translated():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with make_client(handler, max_retries=1) as c:
        with pytest.raises(SourceError):
            c.get("https://x/1", source="4chan")


def test_cache_serves_repeat_get_without_second_call():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"n": calls["n"]})

    with make_client(handler, cache_ttl=1000) as c:
        first = c.get("https://x/cat.json", use_cache=True).json()
        second = c.get("https://x/cat.json", use_cache=True).json()
        assert first == second
        assert calls["n"] == 1


def test_conditional_304_reuses_cached_body():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if "If-Modified-Since" in request.headers or "If-None-Match" in request.headers:
            return httpx.Response(304)
        return httpx.Response(
            200,
            json={"v": 1},
            headers={"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"},
        )

    # ttl 0 so the cache is stale and we go conditional on the second call.
    with make_client(handler, cache_ttl=0) as c:
        first = c.get("https://x/cat.json", use_cache=True)
        assert first.status_code == 200
        second = c.get("https://x/cat.json", use_cache=True, conditional=True)
        # 304 -> reuse cached 200 body
        assert second.status_code == 200
        assert second.json() == {"v": 1}
        assert state["n"] == 2
