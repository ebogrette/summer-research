"""Per-source token bucket rate limiting.

The pipeline fans out over sources with threads, so buckets must be thread-safe.
`acquire()` blocks until a token is available; `try_acquire()` is the non-blocking
variant. A `time_source`/`sleep` seam keeps the tests deterministic and offline.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Classic token bucket.

    `rate` tokens are added per second up to `capacity`. Ask for one token per
    request. 4chan gets rate=1 capacity=1; Reddit ~1.5/s (90/min) with a small burst.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._time = time_source
        self._sleep = sleep
        self._last = self._time()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = self._time()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take `tokens` if available right now; return whether it succeeded."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until `tokens` are available. Return the seconds spent waiting."""
        if tokens > self.capacity:
            raise ValueError("requested tokens exceed bucket capacity")
        waited = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            self._sleep(wait)
            waited += wait


class RateLimiterRegistry:
    """Named buckets, one per source. Thread-safe lazy creation."""

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def register(self, name: str, rate: float, capacity: float | None = None) -> TokenBucket:
        with self._lock:
            bucket = TokenBucket(rate, capacity)
            self._buckets[name] = bucket
            return bucket

    def get(self, name: str) -> TokenBucket | None:
        with self._lock:
            return self._buckets.get(name)

    def acquire(self, name: str, tokens: float = 1.0) -> float:
        bucket = self.get(name)
        if bucket is None:
            return 0.0
        return bucket.acquire(tokens)
