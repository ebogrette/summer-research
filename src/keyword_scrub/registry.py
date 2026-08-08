"""Adapter discovery + capability reporting (PLAN §4, §6).

The `Registry` owns the shared `HttpClient`, the per-source rate limiters, and one
instance of each source adapter. It is the single place that knows *which* sources
exist and how they were constructed, so both the pipeline (fan-out) and the API
(`GET /sources`) ask it rather than reaching for adapters directly.

Construction is centralized in `from_settings()` so credentials flow from config into
adapters in exactly one place. Sources that aren't built yet (Twitter, Phase 5) simply
aren't registered; the rest of the system works without them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .config import Settings
from .http import HttpClient
from .models import SourceInfo
from .ratelimit import TokenBucket
from .sources.base import SourceAdapter
from .sources.fourchan import FourChanAdapter
from .sources.reddit import RedditAdapter

# Per-source rate budgets (PLAN §7). 4chan: ~1 rps, single connection. Reddit: ~90/min
# (under the 100 ceiling) with a small burst.
_FOURCHAN_RATE = 1.0
_REDDIT_RATE = 1.5
_REDDIT_BURST = 5.0


class Registry:
    """Holds the live adapters and the resources they share."""

    def __init__(self, http: HttpClient, adapters: Iterable[SourceAdapter]) -> None:
        self.http = http
        # Preserve registration order; key by adapter name for lookup.
        self._adapters: dict[str, SourceAdapter] = {a.name: a for a in adapters}

    # -- construction ----------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings, *, http: HttpClient | None = None) -> "Registry":
        http = http or HttpClient(
            user_agent=settings.http_user_agent,
            timeout=settings.http_timeout,
            cache_ttl=settings.cache_ttl,
        )

        fourchan = FourChanAdapter(
            http,
            default_boards=settings.fourchan_default_boards,
            rate_limiter=TokenBucket(_FOURCHAN_RATE),
        )
        reddit = RedditAdapter.from_settings(
            settings,
            http,
            rate_limiter=TokenBucket(_REDDIT_RATE, _REDDIT_BURST),
        )
        return cls(http, [fourchan, reddit])

    # -- lookup ----------------------------------------------------------------------

    def get(self, name: str) -> SourceAdapter | None:
        return self._adapters.get(name)

    def all(self) -> list[SourceAdapter]:
        """Every registered adapter, in registration order."""
        return list(self._adapters.values())

    def names(self) -> list[str]:
        return list(self._adapters)

    def resolve(self, requested: Iterable[str] | None) -> list[SourceAdapter]:
        """Adapters to run for a query.

        `None` means "all registered"; an explicit list is filtered to names we know,
        preserving the caller's order. Unknown names are dropped silently — the
        per-source status block is where their absence would otherwise be reported, and
        a name we've never heard of has no status to report.
        """
        if requested is None:
            return self.all()
        out: list[SourceAdapter] = []
        seen: set[str] = set()
        for name in requested:
            adapter = self._adapters.get(name)
            if adapter is not None and adapter.name not in seen:
                out.append(adapter)
                seen.add(adapter.name)
        return out

    def describe_all(self) -> list[SourceInfo]:
        """Capability + configured status per adapter, for `GET /sources`."""
        return [a.describe() for a in self._adapters.values()]

    # -- lifecycle -------------------------------------------------------------------

    def close(self) -> None:
        self.http.close()

    def __iter__(self) -> Iterator[SourceAdapter]:
        return iter(self._adapters.values())
