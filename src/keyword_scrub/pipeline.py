"""Fan-out, merge, dedupe, sort, paginate (PLAN §6.1).

`run_search` resolves the requested sources, runs each adapter's `search()` on its own
thread (these are I/O-bound HTTP calls — threads are the right tool and keep Flask
simple), then merges the streams into one ranked, de-duplicated list.

The load-bearing property is partial-failure isolation: a source that times out, is
unconfigured, or raises still contributes an entry to the per-source status block
instead of failing the whole request. That status block is what makes a partial result
legible rather than mysterious.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Literal

from .errors import NotConfigured, SourceError
from .models import Post, SearchQuery, SearchResult, SourceStatus
from .registry import Registry
from .sources.base import SourceAdapter

SortKey = Literal["created_at", "score"]

# Fallback per-source wall-clock budget when the caller doesn't specify one.
_DEFAULT_TIMEOUT = 30.0


def run_search(
    q: SearchQuery,
    registry: Registry,
    *,
    sort: SortKey = "created_at",
    per_source_timeout: float = _DEFAULT_TIMEOUT,
    global_limit: int | None = None,
) -> SearchResult:
    """Run `q` across the requested sources and return a merged `SearchResult`.

    - `sort`: rank the merged posts by newest `created_at` (default) or highest `score`.
    - `per_source_timeout`: how long any single source may take before it's recorded as
      an error and skipped.
    - `global_limit`: optional cap on the merged result count, applied after ranking.
      `None` leaves the per-source `q.limit` as the only bound.
    """
    started = time.monotonic()
    adapters = registry.resolve(q.sources)

    statuses: dict[str, SourceStatus] = {}
    collected: list[Post] = []

    if adapters:
        # One worker per source; the calls are I/O-bound so threads suffice.
        with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
            futures = {pool.submit(_run_one, a, q): a for a in adapters}
            for future, adapter in futures.items():
                name = adapter.name
                try:
                    posts, elapsed_ms = future.result(timeout=per_source_timeout)
                except FutureTimeout:
                    statuses[name] = SourceStatus(status="error", reason="timeout")
                    continue
                except NotConfigured as exc:
                    statuses[name] = SourceStatus(
                        status="unavailable", reason=exc.code, count=0
                    )
                    continue
                except SourceError as exc:
                    statuses[name] = SourceStatus(status="error", reason=exc.code)
                    continue
                except Exception:  # noqa: BLE001 — an adapter leaked a non-typed error
                    statuses[name] = SourceStatus(status="error", reason="source_error")
                    continue

                collected.extend(posts)
                statuses[name] = SourceStatus(
                    status="ok", count=len(posts), elapsed_ms=elapsed_ms
                )

    posts = _merge(collected, sort=sort, limit=global_limit)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return SearchResult(posts=posts, sources=statuses, query=q, elapsed_ms=elapsed_ms)


def _run_one(adapter: SourceAdapter, q: SearchQuery) -> tuple[list[Post], int]:
    """Drain one adapter's stream to a list, honoring the per-source cap.

    Runs on a worker thread. Exceptions propagate to the future and are classified by
    the caller; the adapter contract guarantees only typed `SourceError`s escape, but we
    defend against the rest there too.
    """
    started = time.monotonic()
    posts: list[Post] = []
    for post in adapter.search(q):
        posts.append(post)
        if len(posts) >= q.limit:
            break
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return posts, elapsed_ms


def _merge(posts: list[Post], *, sort: SortKey, limit: int | None) -> list[Post]:
    """Dedupe on `source:id`, rank, and apply the optional global cap."""
    unique: dict[str, Post] = {}
    for post in posts:
        # First writer wins; sources are independent so a genuine cross-source clash is
        # impossible — this only guards a source repeating an id across pages.
        unique.setdefault(post.key, post)

    if sort == "score":
        # Missing score sinks to the bottom; ties fall back to recency.
        ordered = sorted(
            unique.values(),
            key=lambda p: (p.score if p.score is not None else -1, p.created_at),
            reverse=True,
        )
    else:
        ordered = sorted(unique.values(), key=lambda p: p.created_at, reverse=True)

    if limit is not None:
        return ordered[:limit]
    return ordered
