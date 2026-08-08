"""Shared HTTP client. Every outbound request goes through here.

Provides: connection pooling (one `httpx.Client`), a default User-Agent, exponential
backoff with jitter on 5xx/429, `Retry-After` honored, a hard timeout, and a small
TTL response cache (the big win being 4chan catalogs on repeat polls).

Adapters call `client.get(...)` and get back an `httpx.Response`, or a typed
`RateLimited` when retries are exhausted on a 429. Other transport errors are
translated to `SourceError` so nothing raw escapes the adapter boundary.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

from .errors import RateLimited, SourceError


@dataclass
class _CacheEntry:
    response: httpx.Response
    stored_at: float
    # Value of ETag / Last-Modified for conditional revalidation.
    etag: str | None
    last_modified: str | None


class ResponseCache:
    """Tiny thread-safe TTL cache keyed on method+URL.

    Also retains validators (ETag / Last-Modified) so callers can send conditional
    requests and treat a 304 as "reuse the cached body".
    """

    def __init__(self, ttl: float, *, time_source: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self._time = time_source
        self._store: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(method: str, url: str) -> str:
        return f"{method.upper()} {url}"

    def get_fresh(self, method: str, url: str) -> httpx.Response | None:
        with self._lock:
            entry = self._store.get(self._key(method, url))
            if entry is None:
                return None
            if self._time() - entry.stored_at <= self.ttl:
                return entry.response
            return None

    def get_entry(self, method: str, url: str) -> _CacheEntry | None:
        with self._lock:
            return self._store.get(self._key(method, url))

    def store(self, method: str, url: str, response: httpx.Response) -> None:
        with self._lock:
            self._store[self._key(method, url)] = _CacheEntry(
                response=response,
                stored_at=self._time(),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )


class HttpClient:
    """Wrapper around `httpx.Client` with retries, backoff, and caching."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        cache_ttl: float = 60.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_retries = max_retries
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.cache = ResponseCache(cache_ttl)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        # Exponential backoff with full jitter.
        base = min(2.0**attempt, 30.0)
        return self._rng.uniform(0, base)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            # HTTP-date form is rare here; ignore and fall back to backoff.
            return None

    def get(
        self,
        url: str,
        *,
        source: str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        use_cache: bool = True,
        conditional: bool = False,
    ) -> httpx.Response:
        return self.request(
            "GET",
            url,
            source=source,
            headers=headers,
            params=params,
            use_cache=use_cache,
            conditional=conditional,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        source: str | None = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        data: object = None,
        json: object = None,
        use_cache: bool = False,
        conditional: bool = False,
    ) -> httpx.Response:
        method = method.upper()
        cacheable = use_cache and method == "GET"

        if cacheable:
            fresh = self.cache.get_fresh(method, url)
            if fresh is not None:
                return fresh

        req_headers = dict(headers or {})
        # Conditional revalidation: attach validators from a prior cached response.
        if conditional and method == "GET":
            entry = self.cache.get_entry(method, url)
            if entry is not None:
                if entry.etag:
                    req_headers.setdefault("If-None-Match", entry.etag)
                if entry.last_modified:
                    req_headers.setdefault("If-Modified-Since", entry.last_modified)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=req_headers,
                    params=params,
                    data=data,
                    json=json,
                )
            except httpx.HTTPError as exc:
                # Network/transport error: retry, then translate. Never let httpx escape.
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt, None))
                    continue
                raise SourceError(f"transport error for {url}: {exc}", source=source) from exc

            # 304: caller asked to revalidate and the cached copy is still good.
            if response.status_code == 304 and conditional:
                entry = self.cache.get_entry(method, url)
                if entry is not None:
                    return entry.response

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    retry_after = self._parse_retry_after(response)
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                if response.status_code == 429:
                    raise RateLimited(
                        f"rate limited on {url}",
                        source=source,
                        retry_after=self._parse_retry_after(response),
                    )
                raise SourceError(
                    f"server error {response.status_code} on {url}", source=source
                )

            if cacheable and response.status_code == 200:
                self.cache.store(method, url, response)
            return response

        # Unreachable: loop either returns or raises. Guard for type-checkers.
        raise SourceError(
            f"request to {url} failed: {last_exc}", source=source
        )
