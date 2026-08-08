"""Tests for the token bucket, using injected clock/sleep for determinism."""

from __future__ import annotations

import pytest

from keyword_scrub.ratelimit import RateLimiterRegistry, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # Simulate the passage of time without actually blocking.
        self.now += seconds


def test_burst_up_to_capacity_then_blocks():
    clk = FakeClock()
    b = TokenBucket(rate=1.0, capacity=2.0, time_source=clk.time, sleep=clk.sleep)
    assert b.try_acquire()
    assert b.try_acquire()
    # capacity exhausted
    assert not b.try_acquire()


def test_refills_over_time():
    clk = FakeClock()
    b = TokenBucket(rate=1.0, capacity=1.0, time_source=clk.time, sleep=clk.sleep)
    assert b.try_acquire()
    assert not b.try_acquire()
    clk.now += 1.0  # one token regenerated
    assert b.try_acquire()


def test_acquire_blocks_and_reports_wait():
    clk = FakeClock()
    b = TokenBucket(rate=2.0, capacity=1.0, time_source=clk.time, sleep=clk.sleep)
    assert b.acquire() == 0.0  # first is free
    waited = b.acquire()  # must wait 0.5s for the next token at 2/s
    assert waited == pytest.approx(0.5)


def test_requesting_more_than_capacity_errors():
    b = TokenBucket(rate=1.0, capacity=1.0)
    with pytest.raises(ValueError):
        b.acquire(2.0)


def test_zero_rate_rejected():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


def test_registry_returns_none_for_unknown():
    reg = RateLimiterRegistry()
    assert reg.get("nope") is None
    # acquire on unknown source is a no-op that doesn't block
    assert reg.acquire("nope") == 0.0


def test_registry_registers_and_acquires():
    reg = RateLimiterRegistry()
    reg.register("4chan", rate=1.0, capacity=1.0)
    assert reg.get("4chan") is not None
    assert reg.acquire("4chan") == 0.0
