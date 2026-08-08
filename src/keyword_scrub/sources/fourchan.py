"""4chan adapter — capability: NATIVE, via the 4plebs archive (PLAN §5.1).

4chan itself offers no search, so this adapter reads from the 4plebs archive, which
runs a FoolFuuka instance with a real full-text index. Web search URLs look like
`https://archive.4plebs.org/_/search/text/hitler/`; we hit the equivalent JSON API
(`/_/api/chan/search/`) rather than scraping the HTML.

The archive selects results server-side. The local matcher still runs over the returned
text — but only to populate `matched_keywords` (why a result matched) and, under `all`
mode, to enforce the AND that FoolFuuka's single `text` field can't express. Every
request goes through the shared `HttpClient` (backoff, `Retry-After`, short-TTL cache)
and is sequenced through an optional token bucket; we never parallelize this source.
"""

from __future__ import annotations

import html
import os.path
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

from ..errors import SourceError
from ..http import HttpClient
from ..matching import Matcher
from ..models import MediaRef, Post, SearchQuery, SourceInfo
from ..ratelimit import TokenBucket
from .base import SearchCapability

_SEARCH_URL = "https://archive.4plebs.org/_/api/chan/search"
_SITE_HOST = "https://archive.4plebs.org"
_VIDEO_EXT = {".webm", ".mp4"}

# Boards 4plebs actually archives. A request for anything outside this set contributes
# no coverage from this source (rather than being an error).
DEFAULT_ARCHIVED_BOARDS = frozenset(
    {"adv", "f", "hr", "o", "pol", "s4s", "sp", "tg", "trash", "tv", "x"}
)

# Safety valve: FoolFuuka pages ~25 results at a time; never walk more than this many.
_MAX_PAGES = 40

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(raw: str | None) -> str:
    """Strip archive comment markup to plaintext, preserving `>quote` lines (PLAN §3)."""
    if not raw:
        return ""
    text = _BR_RE.sub("\n", raw)
    text = text.replace("<wbr>", "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _to_int(value: Any) -> int | None:
    """FoolFuuka returns numbers as strings as often as not; coerce, tolerating junk."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class FourChanAdapter:
    """Server-side keyword search over 4chan history via the 4plebs FoolFuuka API."""

    name = "4chan"
    capability = SearchCapability.NATIVE
    requires_auth = False

    def __init__(
        self,
        http: HttpClient,
        *,
        default_boards: list[str] | None = None,
        archived_boards: frozenset[str] | set[str] | None = None,
        rate_limiter: TokenBucket | None = None,
        order: str = "desc",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.http = http
        self.default_boards = list(default_boards) if default_boards else []
        self.archived_boards = (
            frozenset(archived_boards)
            if archived_boards is not None
            else DEFAULT_ARCHIVED_BOARDS
        )
        self._rate = rate_limiter
        self.order = order
        self._now = clock or (lambda: datetime.now(timezone.utc))

    # -- contract ----------------------------------------------------------------

    def is_configured(self) -> bool:
        return True  # no auth required

    def describe(self) -> SourceInfo:
        return SourceInfo(
            name=self.name,
            capability=str(self.capability),
            requires_auth=self.requires_auth,
            configured=self.is_configured(),
            note="server-side search via the 4plebs archive (subset of boards)",
        )

    def search(self, q: SearchQuery) -> Iterator[Post]:
        matcher = self._build_matcher(q)
        boards = self._resolve_boards(q)
        if not boards:
            # No requested board is archived — this source has no coverage, not an error.
            return

        text = self._search_text(q)
        enforce_all = q.mode == "all"
        yielded = 0

        for page in range(1, _MAX_PAGES + 1):
            posts = self._fetch_page(text, boards, page, q)
            if not posts:
                break
            for raw in posts:
                post = self._to_post(matcher, raw, enforce_all=enforce_all)
                if post is None:
                    continue
                yield post
                yielded += 1
                if yielded >= q.limit:
                    return

    # -- request plumbing --------------------------------------------------------

    def _search_url(self, text: str, boards: list[str], page: int, q: SearchQuery) -> str:
        params: dict[str, str] = {
            "text": text,
            "boards": ".".join(boards),
            "order": self.order,
            "page": str(page),
        }
        if q.since is not None:
            params["start"] = q.since.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if q.until is not None:
            params["end"] = q.until.astimezone(timezone.utc).strftime("%Y-%m-%d")
        # Encode into the URL rather than passing params separately: the shared cache
        # keys on the URL string, so distinct pages must produce distinct URLs.
        return f"{_SEARCH_URL}/?{urlencode(params)}"

    def _fetch_page(
        self, text: str, boards: list[str], page: int, q: SearchQuery
    ) -> list[dict[str, Any]]:
        if self._rate is not None:
            self._rate.acquire()
        url = self._search_url(text, boards, page, q)
        response = self.http.get(url, source=self.name, use_cache=True, conditional=True)
        if response.status_code != 200:
            return []
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from {url}: {exc}", source=self.name) from exc
        return self._extract_posts(data)

    @staticmethod
    def _extract_posts(data: Any) -> list[dict[str, Any]]:
        # FoolFuuka wraps each page as {"0": {"posts": [...]}, ...}; "No results found"
        # comes back as {"error": "..."}.
        if not isinstance(data, dict) or "error" in data:
            return []
        posts: list[dict[str, Any]] = []
        for value in data.values():
            if isinstance(value, dict) and isinstance(value.get("posts"), list):
                posts.extend(value["posts"])
        return posts

    # -- normalization -----------------------------------------------------------

    def _to_post(
        self, matcher: Matcher, raw: dict[str, Any], *, enforce_all: bool
    ) -> Post | None:
        num = raw.get("num")
        if num is None:
            return None
        num = str(num)

        title = _clean(raw.get("title")) or None
        body = _clean(raw.get("comment_sanitized") or raw.get("comment"))

        # The archive already selected this result; we re-run the matcher only to say
        # *why* it matched, and to enforce `all` (FoolFuuka has no native AND).
        _, keywords, _ = matcher.search_fields(title=title, body=body)
        if enforce_all and len(keywords) < len(matcher.keywords):
            return None

        is_op = raw.get("op") in (1, "1", True)
        thread_num = str(raw.get("thread_num") or num)
        thread_id = num if is_op else thread_num
        board = self._board_of(raw)

        timestamp = _to_int(raw.get("timestamp")) or 0
        return Post(
            source=self.name,
            id=num,
            url=f"{_SITE_HOST}/{board}/thread/{thread_id}/#{num}",
            body=body,
            created_at=datetime.fromtimestamp(timestamp, tz=timezone.utc),
            fetched_at=self._now(),
            match_field="native",  # the archive did the matching
            thread_id=thread_id,
            parent_id=None if is_op else thread_num,
            container=board,
            title=title,
            author=raw.get("name"),
            lang=None,
            score=None,  # 4chan exposes no score
            reply_count=_to_int(raw.get("nreplies")) if is_op else None,
            media=self._media(raw),
            matched_keywords=keywords,
            raw=raw,
        )

    @staticmethod
    def _board_of(raw: dict[str, Any]) -> str | None:
        board = raw.get("board")
        if isinstance(board, dict):
            return board.get("shortname")
        return board if isinstance(board, str) else None

    def _media(self, raw: dict[str, Any]) -> list[MediaRef]:
        media = raw.get("media")
        if not isinstance(media, dict):
            return []
        link = media.get("media_link") or media.get("remote_media_link")
        if not link:
            return []
        # Use the archive's absolute links directly — the original 4chan file is often
        # gone but the archived copy remains.
        ext = os.path.splitext(link)[1].lower()
        kind = "video" if ext in _VIDEO_EXT else "image"
        return [
            MediaRef(
                url=link,
                kind=kind,
                thumbnail_url=media.get("thumb_link"),
                width=_to_int(media.get("media_w")),
                height=_to_int(media.get("media_h")),
            )
        ]

    # -- query translation -------------------------------------------------------

    def _build_matcher(self, q: SearchQuery) -> Matcher:
        try:
            return Matcher(q.keywords, mode=q.mode, case_sensitive=q.case_sensitive)
        except ValueError as exc:
            raise SourceError(str(exc), source=self.name) from exc

    @staticmethod
    def _search_text(q: SearchQuery) -> str:
        joined = " ".join(kw.strip() for kw in q.keywords if kw.strip())
        # `phrase` must match as a contiguous string; quote it for FoolFuuka.
        return f'"{joined}"' if q.mode == "phrase" else joined

    def _resolve_boards(self, q: SearchQuery) -> list[str]:
        candidates = q.containers if q.containers else self.default_boards
        boards: list[str] = []
        for board in candidates:
            slug = board.strip().strip("/")
            # Silently drop boards 4plebs doesn't archive; keep order, de-dupe.
            if slug and slug in self.archived_boards and slug not in boards:
                boards.append(slug)
        return boards
