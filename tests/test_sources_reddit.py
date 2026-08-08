"""Fixture-driven tests for the Reddit adapter (official OAuth2 API). Zero network —
every token grant and search page is served from captured payloads via httpx's
MockTransport (PLAN §5.2, §7 testing).
"""

from __future__ import annotations

import base64
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from keyword_scrub.errors import AuthError, NotConfigured, SourceError
from keyword_scrub.http import HttpClient
from keyword_scrub.models import SearchQuery
from keyword_scrub.sources.base import SearchCapability, SourceAdapter
from keyword_scrub.sources.reddit import RedditAdapter

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2024, 1, 5, tzinfo=timezone.utc)


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


class _FakeClock:
    """Mutable monotonic-ish clock for driving token expiry deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def make_adapter(
    *,
    calls: list[dict] | None = None,
    token_status: int = 200,
    search_status: int = 200,
    unauthorized_first: bool = False,
    clock: _FakeClock | None = None,
    token_expires_in: int | None = None,
    configured: bool = True,
    **kwargs,
) -> RedditAdapter:
    """Adapter wired to a MockTransport serving the token grant and search pages.

    Search pages are keyed by the `after` cursor: none -> p1, `t3_p2cursor` -> p2,
    anything else -> empty. `calls` records each request as {method, path, params}.
    """
    token_payload = dict(_load("reddit_token.json"))
    if token_expires_in is not None:
        token_payload["expires_in"] = token_expires_in
    pages = {
        None: _load("reddit_search_p1.json"),
        "t3_p2cursor": _load("reddit_search_p2.json"),
    }
    empty = _load("reddit_search_empty.json")
    state = {"search_seen": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if calls is not None:
            calls.append(
                {"method": request.method, "path": request.url.path, "params": params}
            )
        if request.url.path.endswith("/api/v1/access_token"):
            return httpx.Response(token_status, json=token_payload)
        # Search endpoint.
        if unauthorized_first and state["search_seen"] == 0:
            state["search_seen"] += 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        state["search_seen"] += 1
        if search_status != 200:
            return httpx.Response(search_status, json={"message": "nope"})
        after = params.get("after")
        return httpx.Response(200, json=pages.get(after, empty))

    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"})
    http = HttpClient(
        user_agent="t",
        client=inner,
        sleep=lambda s: None,
        rng=random.Random(0),
        cache_ttl=kwargs.pop("cache_ttl", 1000),
        max_retries=kwargs.pop("max_retries", 3),
    )
    return RedditAdapter(
        http,
        client_id="cid" if configured else None,
        client_secret="secret" if configured else None,
        user_agent="keyword-scrub/test by u/tester",
        clock=lambda: FIXED_NOW,
        time_source=clock or _FakeClock(),
        **kwargs,
    )


def query(**kwargs) -> SearchQuery:
    kwargs.setdefault("keywords", ["rust"])
    kwargs.setdefault("limit", 100)
    return SearchQuery(**kwargs)


# -- contract / capability -------------------------------------------------------


def test_satisfies_adapter_protocol():
    adapter = make_adapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.capability is SearchCapability.NATIVE
    assert adapter.requires_auth is True
    assert adapter.is_configured() is True


def test_describe_reports_native_capability_and_auth():
    info = make_adapter().describe()
    assert info.name == "reddit"
    assert info.capability == "native"
    assert info.requires_auth is True
    assert info.configured is True


def test_unconfigured_reports_not_configured():
    adapter = make_adapter(configured=False)
    assert adapter.is_configured() is False
    assert adapter.describe().configured is False
    with pytest.raises(NotConfigured):
        list(adapter.search(query()))


# -- OAuth -----------------------------------------------------------------------


def test_token_requested_with_basic_auth_and_grant():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query()))
    token_call = next(c for c in calls if c["path"].endswith("/access_token"))
    assert token_call["method"] == "POST"


def test_token_sends_expected_authorization_header():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            seen["auth"] = request.headers.get("Authorization", "")
            seen["body"] = request.content.decode()
            seen["ua"] = request.headers.get("User-Agent", "")
            return httpx.Response(200, json=_load("reddit_token.json"))
        return httpx.Response(200, json=_load("reddit_search_empty.json"))

    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"})
    http = HttpClient(user_agent="t", client=inner, sleep=lambda s: None, rng=random.Random(0))
    adapter = RedditAdapter(
        http,
        client_id="myid",
        client_secret="mysecret",
        user_agent="keyword-scrub/test by u/tester",
        clock=lambda: FIXED_NOW,
    )
    list(adapter.search(query()))

    expected = "Basic " + base64.b64encode(b"myid:mysecret").decode()
    assert seen["auth"] == expected
    assert "grant_type=client_credentials" in seen["body"]
    assert seen["ua"] == "keyword-scrub/test by u/tester"


def test_token_cached_across_requests():
    calls: list[dict] = []
    adapter = make_adapter(calls=calls)
    list(adapter.search(query()))
    token_posts = sum(1 for c in calls if c["path"].endswith("/access_token"))
    # Multiple pages fetched, but the token is minted exactly once.
    assert token_posts == 1
    assert sum(1 for c in calls if c["path"].endswith("/search")) >= 2


def test_token_refreshed_after_expiry():
    calls: list[dict] = []
    clock = _FakeClock(start=1000.0)
    adapter = make_adapter(calls=calls, clock=clock, token_expires_in=3600, cache_ttl=0)
    list(adapter.search(query()))
    # Advance past the token's lifetime; the next search must re-mint it.
    clock.value += 4000
    list(adapter.search(query()))
    token_posts = sum(1 for c in calls if c["path"].endswith("/access_token"))
    assert token_posts == 2


def test_bearer_token_sent_on_search():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json=_load("reddit_token.json"))
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, json=_load("reddit_search_empty.json"))

    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"})
    http = HttpClient(user_agent="t", client=inner, sleep=lambda s: None, rng=random.Random(0))
    adapter = RedditAdapter(
        http, client_id="i", client_secret="s", user_agent="ua", clock=lambda: FIXED_NOW
    )
    list(adapter.search(query()))
    assert seen and seen[0] == "bearer test-bearer-token-abc"


def test_reauth_once_on_401():
    calls: list[dict] = []
    posts = list(make_adapter(calls=calls, unauthorized_first=True).search(query()))
    # The first search 401s; the adapter drops the token, re-mints it, and retries.
    token_posts = sum(1 for c in calls if c["path"].endswith("/access_token"))
    assert token_posts == 2
    assert [p.id for p in posts] == ["sub001", "sub002", "com003", "sub004", "sub005"]


def test_bad_credentials_raise_auth_error():
    with pytest.raises(AuthError):
        list(make_adapter(token_status=401).search(query()))


# -- server-side search results --------------------------------------------------


def test_search_returns_all_results_across_pages():
    posts = list(make_adapter().search(query()))
    assert [p.id for p in posts] == ["sub001", "sub002", "com003", "sub004", "sub005"]


def test_submission_normalized_fields():
    posts = {p.id: p for p in make_adapter().search(query())}
    sub = posts["sub001"]
    assert sub.source == "reddit"
    assert sub.url == "https://www.reddit.com/r/news/comments/sub001/rust_lang_hits_milestone/"
    assert sub.thread_id == "sub001"
    assert sub.parent_id is None
    assert sub.container == "r/news"
    assert sub.title == "Rust language hits a major milestone"
    assert sub.author == "carol"
    assert sub.match_field == "native"
    assert sub.matched_keywords == ["rust"]
    assert sub.score == 4210
    assert sub.reply_count == 512
    assert sub.lang is None
    assert sub.created_at == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert sub.created_at.tzinfo is not None
    assert sub.fetched_at == FIXED_NOW
    # HTML entity in selftext is decoded.
    assert sub.body == "The rust project announced > big news today about memory safety."


def test_comment_normalized_fields():
    posts = {p.id: p for p in make_adapter().search(query())}
    com = posts["com003"]
    assert com.title is None
    assert com.thread_id == "sub001"  # link_id t3_sub001 -> sub001
    assert com.parent_id == "sub001"  # parent_id t3_sub001 -> sub001
    assert com.reply_count is None
    assert com.body == "Honestly rust changed how I think about ownership."
    assert com.score == 27


def test_media_extracted_from_preview():
    sub = {p.id: p for p in make_adapter().search(query())}["sub001"]
    assert len(sub.media) == 1
    media = sub.media[0]
    assert media.url == "https://preview.redd.it/abc.png?width=1200"
    assert media.kind == "image"
    assert media.thumbnail_url == "https://preview.redd.it/abc.png?width=108"
    assert media.width == 1200
    assert media.height == 630


def test_post_without_preview_has_no_media():
    sub = {p.id: p for p in make_adapter().search(query())}["sub002"]
    assert sub.media == []


def test_post_round_trips_to_json():
    sub = {p.id: p for p in make_adapter().search(query())}["sub001"]
    d = sub.to_dict()
    assert d["created_at"] == "2024-01-01T00:00:00Z"
    assert d["container"] == "r/news"
    assert "raw" not in d
    assert json.loads(json.dumps(d))["id"] == "sub001"


# -- query translation -----------------------------------------------------------


def test_any_mode_joins_keywords_with_or():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(keywords=["rust", "go"])))
    search = next(c for c in calls if c["path"].endswith("/search"))
    assert search["params"]["q"] == "rust OR go"


def test_all_mode_joins_keywords_with_and():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(keywords=["rust", "go"], mode="all")))
    search = next(c for c in calls if c["path"].endswith("/search"))
    assert search["params"]["q"] == "rust AND go"


def test_phrase_mode_quotes_the_text():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(keywords=["rust", "lang"], mode="phrase")))
    search = next(c for c in calls if c["path"].endswith("/search"))
    assert search["params"]["q"] == '"rust lang"'


def test_all_mode_enforced_locally():
    # Only posts containing BOTH keywords survive; Reddit's AND can match fields we
    # don't store, so the local matcher re-checks title+body.
    posts = list(make_adapter().search(query(keywords=["rust", "go"], mode="all")))
    assert [p.id for p in posts] == ["sub002", "sub004"]


def test_sitewide_search_when_no_containers():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(containers=None)))
    search = next(c for c in calls if "/search" in c["path"])
    assert search["path"] == "/search"
    assert "restrict_sr" not in search["params"]


def test_scoped_search_when_container_given():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(containers=["r/news"])))
    search = next(c for c in calls if "/search" in c["path"])
    assert search["path"] == "/r/news/search"
    assert search["params"]["restrict_sr"] == "1"


# -- date filtering --------------------------------------------------------------


def test_since_until_filtered_locally():
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2024, 1, 31, tzinfo=timezone.utc)
    posts = list(make_adapter().search(query(since=since, until=until)))
    # sub005 (2015) is dropped; everything else is inside the window.
    assert "sub005" not in [p.id for p in posts]
    assert "sub001" in [p.id for p in posts]


def test_since_maps_to_coarse_t_bucket():
    calls: list[dict] = []
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)  # 4 days before FIXED_NOW
    list(make_adapter(calls=calls).search(query(since=since)))
    search = next(c for c in calls if c["path"].endswith("/search"))
    assert search["params"]["t"] == "week"


def test_no_since_uses_t_all():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query()))
    search = next(c for c in calls if c["path"].endswith("/search"))
    assert search["params"]["t"] == "all"


# -- limit / pagination ----------------------------------------------------------


def test_limit_caps_total_results():
    posts = list(make_adapter().search(query(limit=2)))
    assert [p.id for p in posts] == ["sub001", "sub002"]


def test_limit_stops_before_fetching_further_pages():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(limit=2)))
    searches = [c for c in calls if c["path"].endswith("/search")]
    # The cap is reached inside page 1; page 2 (after cursor) is never requested.
    assert len(searches) == 1


def test_pagination_follows_after_cursor():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query()))
    searches = [c for c in calls if c["path"].endswith("/search")]
    # p1 has no cursor, p2 uses the cursor from p1, then after is null and paging stops.
    afters = [c["params"].get("after") for c in searches]
    assert afters == [None, "t3_p2cursor"]


# -- resilience ------------------------------------------------------------------


def test_no_keywords_raises_source_error():
    with pytest.raises(SourceError):
        list(make_adapter().search(query(keywords=[])))


def test_search_404_yields_nothing():
    assert list(make_adapter(search_status=404).search(query())) == []


def test_search_403_raises_auth_error():
    with pytest.raises(AuthError):
        list(make_adapter(search_status=403, max_retries=0).search(query()))
