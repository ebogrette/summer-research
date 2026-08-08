"""The adapter contract every source satisfies (PLAN §4).

An adapter turns a `SearchQuery` into a stream of normalized `Post`s. `search()`
*yields* rather than returning a list so the pipeline can apply a global cap and stop
early — this matters enormously for 4chan, where a naive implementation walks every
thread on a board.

Adapters raise only the typed errors in `errors.py`; raw httpx exceptions never escape
a module. That's what keeps one flaky source from taking down the whole query.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ..models import Post, SearchQuery, SourceInfo


class SearchCapability(StrEnum):
    NATIVE = "native"  # platform searches server-side (reddit, twitter)
    CRAWL_FILTER = "crawl"  # we fetch broadly and filter locally (4chan)


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    capability: SearchCapability
    requires_auth: bool

    def is_configured(self) -> bool: ...

    def describe(self) -> SourceInfo: ...  # for GET /sources

    def search(self, q: SearchQuery) -> Iterator[Post]: ...
