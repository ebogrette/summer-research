"""Reddit adapter — capability: NATIVE (PLAN §5.2).

Reddit's own search does the selecting. We authenticate as an OAuth2 "script" app
(client-credentials grant), then hit `oauth.reddit.com`'s search endpoints and normalize
the resulting Listing into `Post`s. The local matcher still runs over each result — not
to *select* it (Reddit already did) but to populate `matched_keywords`, so the API can
tell the caller *why* a result matched.

Raw HTTP via the shared `HttpClient` rather than PRAW: PRAW brings its own session, rate
limiter, and lazy-loading model that fight the pipeline's. The endpoints are simple
enough that the dependency isn't worth it.

Auth model:
- POST client id/secret (HTTP Basic) to `.../api/v1/access_token` with
  `grant_type=client_credentials`. Cache the bearer token; refresh shortly before it
  expires, and once reactively if a call comes back 401.
- Send a descriptive `User-Agent` on every call — Reddit throttles generic ones hard.
"""

from __future__ import annotations

import base64
import html
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from ..errors import AuthError, NotConfigured, SourceError
from ..http import HttpClient
from ..matching import Matcher
from ..models import MediaRef, Post, SearchQuery, SourceInfo
from ..ratelimit import TokenBucket
from .base import SearchCapability

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_HOST = "https://oauth.reddit.com"
_SITE_HOST = "https://www.reddit.com"

# Reddit caps a listing page at 100 items and a full walk at ~1000 (the listing ceiling);
# 10 pages of 100 is the practical wall. This is the safety valve against runaway paging.
_MAX_PAGES = 10
_PAGE_SIZE = 100

# Refresh the token this many seconds before its stated expiry, to avoid racing it.
_TOKEN_SKEW = 60.0

# Reddit's coarse recency buckets, longest window last. `search()` picks the smallest
# bucket that still covers `q.since`; precise `since`/`until` filtering happens locally.
_T_BUCKETS: tuple[tuple[timedelta, str], ...] = (
    (timedelta(hours=1), "hour"),
    (timedelta(days=1), "day"),
    (timedelta(days=7), "week"),
    (timedelta(days=31), "month"),
    (timedelta(days=366), "year"),
)


def _clean(raw: str | None) -> str:
    """Reddit bodies are markdown, not HTML — keep the text, just decode entities.

    We request with `raw_json=1` so entities usually arrive already decoded, but unescape
    defensively and trim surrounding whitespace.
    """
    if not raw:
        return ""
    return html.unescape(raw).strip()


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_fullname(fullname: Any) -> str | None:
    """`t3_abc` / `t1_xyz` -> `abc` / `xyz`; pass through a bare id; None -> None."""
    if not fullname:
        return None
    text = str(fullname)
    return text.split("_", 1)[1] if "_" in text else text


class RedditAdapter:
    """Server-side keyword search over Reddit via the official OAuth2 API."""

    name = "reddit"
    capability = SearchCapability.NATIVE
    requires_auth = True

    def __init__(
        self,
        http: HttpClient,
        *,
        client_id: str | None,
        client_secret: str | None,
        user_agent: str,
        rate_limiter: TokenBucket | None = None,
        sort: str = "new",
        clock: Callable[[], datetime] | None = None,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self._rate = rate_limiter
        self.sort = sort
        self._now = clock or (lambda: datetime.now(timezone.utc))
        self._time = time_source

        self._token: str | None = None
        self._token_expiry: float = 0.0  # in `time_source` units

    # -- construction ------------------------------------------------------------

    @classmethod
    def from_settings(
        cls, settings: Any, http: HttpClient, *, rate_limiter: TokenBucket | None = None
    ) -> "RedditAdapter":
        return cls(
            http,
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
            rate_limiter=rate_limiter,
        )

    # -- contract ----------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def describe(self) -> SourceInfo:
        return SourceInfo(
            name=self.name,
            capability=str(self.capability),
            requires_auth=self.requires_auth,
            configured=self.is_configured(),
            note="server-side search via the official Reddit OAuth2 API",
        )

    def search(self, q: SearchQuery) -> Iterator[Post]:
        matcher = self._build_matcher(q)
        if not self.is_configured():
            raise NotConfigured("reddit client credentials not set", source=self.name)

        qtext = self._query_text(q)
        t = self._coarse_t(q.since)
        enforce_all = q.mode == "all"
        # None means a sitewide search; a subreddit name means a scoped one.
        scopes = self._resolve_subreddits(q) or [None]

        yielded = 0
        for scope in scopes:
            for post in self._search_scope(matcher, qtext, t, scope, q, enforce_all):
                yield post
                yielded += 1
                if yielded >= q.limit:
                    return

    # -- OAuth -------------------------------------------------------------------

    def _ensure_token(self) -> str:
        token = self._token
        if token is not None and self._time() < self._token_expiry:
            return token
        return self._fetch_token()

    def _fetch_token(self) -> str:
        if not self.is_configured():
            raise NotConfigured("reddit client credentials not set", source=self.name)

        acquired_at = self._time()
        creds = f"{self.client_id}:{self.client_secret}".encode()
        headers = {
            "Authorization": "Basic " + base64.b64encode(creds).decode(),
            "User-Agent": self.user_agent,
        }
        if self._rate is not None:
            self._rate.acquire()
        response = self.http.request(
            "POST",
            _TOKEN_URL,
            source=self.name,
            headers=headers,
            data={"grant_type": "client_credentials"},
            use_cache=False,
        )
        if response.status_code in (401, 403):
            raise AuthError("reddit rejected client credentials", source=self.name)
        if response.status_code != 200:
            raise SourceError(
                f"reddit token request failed ({response.status_code})", source=self.name
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"invalid token response: {exc}", source=self.name) from exc

        token = data.get("access_token")
        if not token:
            raise AuthError("reddit token response had no access_token", source=self.name)
        expires_in = _to_int(data.get("expires_in")) or 3600

        self._token = str(token)
        self._token_expiry = acquired_at + max(expires_in - _TOKEN_SKEW, 0.0)
        return self._token

    # -- request plumbing --------------------------------------------------------

    def _get(self, url: str, *, allow_reauth: bool = True) -> Any:
        token = self._ensure_token()
        if self._rate is not None:
            self._rate.acquire()
        headers = {"Authorization": f"bearer {token}", "User-Agent": self.user_agent}
        response = self.http.get(
            url, source=self.name, headers=headers, use_cache=True, conditional=True
        )
        if response.status_code == 401 and allow_reauth:
            # Token expired or was revoked mid-flight: drop it, re-auth once, retry.
            self._token = None
            return self._get(url, allow_reauth=False)
        return response

    def _search_url(
        self, qtext: str, t: str, subreddit: str | None, after: str | None, q: SearchQuery
    ) -> str:
        params: dict[str, str] = {
            "q": qtext,
            "sort": self.sort,
            "t": t,
            "limit": str(min(_PAGE_SIZE, max(q.limit, 1))),
            "type": "link",
            "raw_json": "1",
        }
        if subreddit is not None:
            params["restrict_sr"] = "1"
            base = f"{_OAUTH_HOST}/r/{subreddit}/search"
        else:
            base = f"{_OAUTH_HOST}/search"
        if after:
            params["after"] = after
        # Encode into the URL so the shared cache (which keys on the URL) distinguishes
        # pages and scopes.
        return f"{base}?{urlencode(params)}"

    def _fetch_listing(
        self, qtext: str, t: str, subreddit: str | None, after: str | None, q: SearchQuery
    ) -> dict[str, Any]:
        url = self._search_url(qtext, t, subreddit, after, q)
        response = self._get(url)
        if response.status_code in (401, 403):
            raise AuthError(
                f"reddit refused search ({response.status_code})", source=self.name
            )
        if response.status_code != 200:
            # 404 or other non-fatal status: this scope simply contributes nothing.
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from {url}: {exc}", source=self.name) from exc
        if not isinstance(data, dict):
            return {}
        listing = data.get("data")
        return listing if isinstance(listing, dict) else {}

    def _search_scope(
        self,
        matcher: Matcher,
        qtext: str,
        t: str,
        subreddit: str | None,
        q: SearchQuery,
        enforce_all: bool,
    ) -> Iterator[Post]:
        after: str | None = None
        for _ in range(_MAX_PAGES):
            listing = self._fetch_listing(qtext, t, subreddit, after, q)
            children = listing.get("children")
            if not isinstance(children, list) or not children:
                break
            for child in children:
                post = self._to_post(matcher, child, q, enforce_all=enforce_all)
                if post is not None:
                    yield post
            after = listing.get("after")
            if not after:
                break

    # -- normalization -----------------------------------------------------------

    def _to_post(
        self, matcher: Matcher, child: Any, q: SearchQuery, *, enforce_all: bool
    ) -> Post | None:
        if not isinstance(child, dict):
            return None
        kind = child.get("kind")
        data = child.get("data")
        if not isinstance(data, dict):
            return None
        pid = data.get("id")
        if not pid:
            return None
        pid = str(pid)

        is_comment = kind == "t1"
        title = None if is_comment else (_clean(data.get("title")) or None)
        body = _clean(data.get("body") if is_comment else data.get("selftext"))

        created_at = self._created_at(data)
        # Reddit's `t` bucket is coarse; enforce the precise window locally.
        if q.since is not None and created_at < q.since:
            return None
        if q.until is not None and created_at > q.until:
            return None

        # Reddit already selected this result; the matcher only says *why* it matched and
        # enforces `all` (Reddit honors AND, but a term can match a field we don't store,
        # so we re-check against title+body ourselves).
        _, keywords, _ = matcher.search_fields(title=title, body=body)
        if enforce_all and len(keywords) < len(matcher.keywords):
            return None

        if is_comment:
            thread_id = _strip_fullname(data.get("link_id"))
            parent_id = _strip_fullname(data.get("parent_id"))
        else:
            thread_id = pid
            parent_id = None

        return Post(
            source=self.name,
            id=pid,
            url=self._permalink(data),
            body=body,
            created_at=created_at,
            fetched_at=self._now(),
            match_field="native",  # Reddit did the matching
            thread_id=thread_id,
            parent_id=parent_id,
            container=self._container(data),
            title=title,
            author=self._author(data),
            lang=None,  # Reddit exposes no per-post language
            score=_to_int(data.get("score")),
            reply_count=None if is_comment else _to_int(data.get("num_comments")),
            media=self._media(data),
            matched_keywords=keywords,
            raw=data,
        )

    @staticmethod
    def _created_at(data: dict[str, Any]) -> datetime:
        created = data.get("created_utc")
        try:
            ts = float(created)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ts = 0.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    @staticmethod
    def _permalink(data: dict[str, Any]) -> str:
        permalink = data.get("permalink")
        if isinstance(permalink, str) and permalink:
            if permalink.startswith("http"):
                return permalink
            return _SITE_HOST + permalink
        return _SITE_HOST

    @staticmethod
    def _container(data: dict[str, Any]) -> str | None:
        prefixed = data.get("subreddit_name_prefixed")
        if isinstance(prefixed, str) and prefixed:
            return prefixed
        sub = data.get("subreddit")
        return f"r/{sub}" if sub else None

    @staticmethod
    def _author(data: dict[str, Any]) -> str | None:
        author = data.get("author")
        return str(author) if author else None

    def _media(self, data: dict[str, Any]) -> list[MediaRef]:
        preview = data.get("preview")
        if not isinstance(preview, dict):
            return []
        images = preview.get("images")
        if not isinstance(images, list):
            return []
        out: list[MediaRef] = []
        for image in images:
            if not isinstance(image, dict):
                continue
            source = image.get("source")
            if not isinstance(source, dict) or not source.get("url"):
                continue
            thumb: str | None = None
            resolutions = image.get("resolutions")
            if isinstance(resolutions, list) and resolutions:
                first = resolutions[0]
                if isinstance(first, dict):
                    thumb = first.get("url")
            out.append(
                MediaRef(
                    url=str(source["url"]),
                    kind="image",
                    thumbnail_url=thumb,
                    width=_to_int(source.get("width")),
                    height=_to_int(source.get("height")),
                )
            )
        return out

    # -- query translation -------------------------------------------------------

    def _build_matcher(self, q: SearchQuery) -> Matcher:
        try:
            return Matcher(q.keywords, mode=q.mode, case_sensitive=q.case_sensitive)
        except ValueError as exc:
            raise SourceError(str(exc), source=self.name) from exc

    @staticmethod
    def _query_text(q: SearchQuery) -> str:
        parts = [kw.strip() for kw in q.keywords if kw.strip()]
        if q.mode == "phrase":
            return '"' + " ".join(parts) + '"'
        joiner = " AND " if q.mode == "all" else " OR "
        return joiner.join(parts)

    def _coarse_t(self, since: datetime | None) -> str:
        if since is None:
            return "all"
        window = self._now() - since
        for span, label in _T_BUCKETS:
            if window <= span:
                return label
        return "all"

    @staticmethod
    def _resolve_subreddits(q: SearchQuery) -> list[str]:
        if not q.containers:
            return []
        subs: list[str] = []
        for raw in q.containers:
            # Accept "news" or "r/news"; de-dupe, preserve order.
            slug = raw.strip().strip("/")
            if slug.lower().startswith("r/"):
                slug = slug[2:]
            if slug and slug not in subs:
                subs.append(slug)
        return subs
