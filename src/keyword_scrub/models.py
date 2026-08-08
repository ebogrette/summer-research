"""The normalized records. This is the contract every adapter satisfies and the only
shape the API emits.

Design rules that live here (see PLAN §3):
- times are always tz-aware UTC and serialize as ISO-8601 with a trailing `Z`
- ids are always `str`, never int
- `None` means "the platform does not expose this"; `0` means "genuinely zero"
- the dedupe key is `f"{source}:{id}"`
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Literal


def _iso_z(dt: datetime) -> str:
    """Serialize a tz-aware datetime as ISO-8601 with a `Z` suffix (UTC)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MediaRef:
    url: str
    kind: str  # "image" | "video" | "thumbnail" | ...
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "thumbnail_url": self.thumbnail_url,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True, slots=True)
class Post:
    # identity
    source: str  # "4chan" | "reddit" | "twitter"
    id: str  # platform-native id, always str
    url: str  # canonical permalink

    # content
    body: str  # plaintext, HTML/markdown stripped, entities decoded

    # timestamps — always tz-aware UTC
    created_at: datetime
    fetched_at: datetime

    # search provenance
    match_field: str  # "title" | "body" | "both" | "native"

    # threading
    thread_id: str | None = None  # root container id (OP no. / submission / conversation)
    parent_id: str | None = None  # direct parent, None if root
    container: str | None = None  # board ("pol"), subreddit ("r/news"), None for tweets

    # content (optional)
    title: str | None = None  # 4chan subject, reddit title, None for tweets/comments
    author: str | None = None  # None or "Anonymous" on 4chan
    lang: str | None = None

    # engagement (None where the platform doesn't expose it)
    score: int | None = None  # upvotes / likes
    reply_count: int | None = None

    # media
    media: list[MediaRef] = field(default_factory=list)

    # search provenance
    matched_keywords: list[str] = field(default_factory=list)

    # escape hatch — omitted from JSON unless include_raw is set
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def key(self) -> str:
        """Globally unique dedupe key across sources."""
        return f"{self.source}:{self.id}"

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source,
            "id": self.id,
            "url": self.url,
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "container": self.container,
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "lang": self.lang,
            "created_at": _iso_z(self.created_at),
            "fetched_at": _iso_z(self.fetched_at),
            "score": self.score,
            "reply_count": self.reply_count,
            "media": [m.to_dict() for m in self.media],
            "matched_keywords": list(self.matched_keywords),
            "match_field": self.match_field,
        }
        if include_raw:
            out["raw"] = self.raw
        return out


@dataclass(frozen=True)
class SearchQuery:
    """The inbound shape shared by every adapter."""

    keywords: list[str]
    mode: Literal["any", "all", "phrase"] = "any"
    sources: list[str] | None = None  # None = all enabled
    containers: list[str] | None = None  # boards / subreddits / None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100  # per-source cap
    include_replies: bool = True
    case_sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "keywords": list(self.keywords),
            "mode": self.mode,
            "sources": self.sources,
            "containers": self.containers,
            "since": _iso_z(self.since) if self.since else None,
            "until": _iso_z(self.until) if self.until else None,
            "limit": self.limit,
            "include_replies": self.include_replies,
            "case_sensitive": self.case_sensitive,
        }


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """Capability + status for a single adapter, surfaced by GET /sources."""

    name: str
    capability: str  # SearchCapability value
    requires_auth: bool
    configured: bool
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "requires_auth": self.requires_auth,
            "configured": self.configured,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Per-source outcome of one search, for the response envelope's `sources` block."""

    status: str  # "ok" | "error" | "unavailable"
    count: int | None = None
    elapsed_ms: int | None = None
    reason: str | None = None  # error code / "not_configured" when not ok

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.count is not None:
            out["count"] = self.count
        if self.elapsed_ms is not None:
            out["elapsed_ms"] = self.elapsed_ms
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class SearchResult:
    posts: list[Post]
    sources: dict[str, SourceStatus]  # keyed by adapter name
    query: SearchQuery
    elapsed_ms: int

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "count": len(self.posts),
            "posts": [p.to_dict(include_raw=include_raw) for p in self.posts],
            "sources": {k: v.to_dict() for k, v in self.sources.items()},
            "elapsed_ms": self.elapsed_ms,
        }


# Field names of Post, handy for tests asserting the contract is stable.
POST_FIELDS = tuple(f.name for f in fields(Post))
