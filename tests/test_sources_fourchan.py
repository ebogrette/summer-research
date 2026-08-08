"""Fixture-driven tests for the 4chan adapter (4plebs archive search). Zero network —
every request is served from captured FoolFuuka payloads via httpx's MockTransport
(PLAN §5.1, §7 testing).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from keyword_scrub.errors import SourceError
from keyword_scrub.http import HttpClient
from keyword_scrub.models import SearchQuery
from keyword_scrub.sources.base import SearchCapability, SourceAdapter
from keyword_scrub.sources.fourchan import FourChanAdapter

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2020, 9, 14, tzinfo=timezone.utc)


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def make_adapter(*, calls: list[dict] | None = None, **kwargs) -> FourChanAdapter:
    """Adapter wired to a MockTransport that serves search pages by their `page` param.

    page 1 -> fixture p1, page 2 -> fixture p2, page >=3 -> "no results". Requests to
    a board with no fixture return the empty result too. `calls` records each request's
    parsed query params.
    """
    pages = {
        "1": _load("fourchan_search_pol_p1.json"),
        "2": _load("fourchan_search_pol_p2.json"),
    }
    empty = _load("fourchan_search_empty.json")

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if calls is not None:
            calls.append(params)
        if "pol" not in params.get("boards", ""):
            return httpx.Response(200, json=empty)
        return httpx.Response(200, json=pages.get(params.get("page", "1"), empty))

    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "test"})
    http = HttpClient(
        user_agent="test",
        client=inner,
        sleep=lambda s: None,
        rng=random.Random(0),
        cache_ttl=kwargs.pop("cache_ttl", 1000),
    )
    return FourChanAdapter(
        http,
        default_boards=kwargs.pop("default_boards", ["pol"]),
        clock=lambda: FIXED_NOW,
        **kwargs,
    )


def query(**kwargs) -> SearchQuery:
    kwargs.setdefault("keywords", ["hitler"])
    kwargs.setdefault("containers", ["pol"])
    kwargs.setdefault("limit", 100)
    return SearchQuery(**kwargs)


# -- contract / capability -------------------------------------------------------


def test_satisfies_adapter_protocol():
    adapter = make_adapter()
    assert isinstance(adapter, SourceAdapter)
    assert adapter.capability is SearchCapability.NATIVE
    assert adapter.requires_auth is False
    assert adapter.is_configured() is True


def test_describe_reports_native_capability():
    info = make_adapter().describe()
    assert info.name == "4chan"
    assert info.capability == "native"
    assert info.configured is True


# -- server-side search results --------------------------------------------------


def test_search_returns_all_archived_results_across_pages():
    posts = list(make_adapter().search(query()))
    # p1 has 4 posts, p2 has 1, p3 is empty -> 5 total, all yielded.
    assert [p.id for p in posts] == ["12000", "12005", "12006", "13000", "14000"]


def test_native_results_included_even_without_local_keyword():
    # Post 12006 ("der Führer …") has no literal "hitler"; the archive matched it, so
    # under `any` it's still returned, with match_field=native and no matched_keywords.
    post = {p.id: p for p in make_adapter().search(query())}["12006"]
    assert post.match_field == "native"
    assert post.matched_keywords == []
    assert post.body == "der Führer gave a speech"  # entity decoded


def test_normalized_fields():
    posts = {p.id: p for p in make_adapter().search(query())}

    op = posts["12000"]
    assert op.source == "4chan"
    assert op.url == "https://archive.4plebs.org/pol/thread/12000/#12000"
    assert op.thread_id == "12000"
    assert op.parent_id is None
    assert op.container == "pol"
    assert op.title == "WW2 general"
    assert op.author == "Anonymous"
    assert op.match_field == "native"
    assert op.matched_keywords == ["hitler"]
    assert op.score is None
    assert op.reply_count == 88
    assert op.created_at == datetime(2020, 9, 13, 12, 26, 40, tzinfo=timezone.utc)
    assert op.created_at.tzinfo is not None
    assert op.fetched_at == FIXED_NOW

    reply = posts["12005"]
    assert reply.parent_id == "12000"
    assert reply.thread_id == "12000"
    assert reply.title is None
    assert reply.reply_count is None


def test_html_stripped_and_quotes_preserved():
    op = {p.id: p for p in make_adapter().search(query())}["12000"]
    # Big <span…>&gt;history</span><br>thread on Hitler
    assert op.body == "Big >history\nthread on Hitler"


def test_media_uses_archive_links_directly():
    posts = {p.id: p for p in make_adapter().search(query())}

    image = posts["12000"].media[0]
    assert image.url == "https://i.4pcdn.org/pol/1600000000000.jpg"
    assert image.thumbnail_url == "https://i.4pcdn.org/pol/1600000000000s.jpg"
    assert image.kind == "image"
    assert image.width == 1024
    assert image.height == 768

    # .webm attachment is classified as video from its extension.
    assert posts["12005"].media[0].kind == "video"

    # A post with no media object has an empty list.
    assert posts["12006"].media == []


def test_post_round_trips_to_json():
    op = {p.id: p for p in make_adapter().search(query())}["12000"]
    d = op.to_dict()
    assert d["created_at"] == "2020-09-13T12:26:40Z"
    assert d["media"][0]["url"] == "https://i.4pcdn.org/pol/1600000000000.jpg"
    assert "raw" not in d
    assert json.loads(json.dumps(d))["id"] == "12000"


# -- query translation -----------------------------------------------------------


def test_search_text_and_boards_sent_to_archive():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(keywords=["hitler", "stalin"])))
    assert calls[0]["text"] == "hitler stalin"
    assert calls[0]["boards"] == "pol"
    assert calls[0]["order"] == "desc"


def test_phrase_mode_quotes_the_text():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(keywords=["adolf", "hitler"], mode="phrase")))
    assert calls[0]["text"] == '"adolf hitler"'


def test_all_mode_enforced_locally():
    # Only post 13000 ("Hitler and Stalin …") contains both keywords; the rest are
    # dropped even though the archive returned them.
    posts = list(make_adapter().search(query(keywords=["hitler", "stalin"], mode="all")))
    assert [p.id for p in posts] == ["13000"]


def test_since_until_passed_as_date_bounds():
    calls: list[dict] = []
    since = datetime(2020, 1, 2, tzinfo=timezone.utc)
    until = datetime(2020, 3, 4, tzinfo=timezone.utc)
    list(make_adapter(calls=calls).search(query(since=since, until=until)))
    assert calls[0]["start"] == "2020-01-02"
    assert calls[0]["end"] == "2020-03-04"


# -- boards / archive coverage ---------------------------------------------------


def test_default_boards_used_when_no_containers():
    posts = list(make_adapter(default_boards=["pol"]).search(query(containers=None)))
    assert len(posts) == 5


def test_unarchived_board_yields_nothing_and_is_not_queried():
    calls: list[dict] = []
    # /g/ is not archived by 4plebs -> no coverage, and no request is even made.
    posts = list(make_adapter(calls=calls).search(query(containers=["g"])))
    assert posts == []
    assert calls == []


def test_unarchived_boards_filtered_out_of_multi_board_query():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(containers=["g", "pol", "news"])))
    assert calls[0]["boards"] == "pol"


# -- limit / pagination ----------------------------------------------------------


def test_limit_caps_total_results():
    posts = list(make_adapter().search(query(limit=2)))
    assert [p.id for p in posts] == ["12000", "12005"]


def test_limit_stops_before_fetching_further_pages():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query(limit=2)))
    # The cap is hit inside page 1, so page 2 is never requested.
    assert [c["page"] for c in calls] == ["1"]


def test_pagination_stops_on_empty_page():
    calls: list[dict] = []
    list(make_adapter(calls=calls).search(query()))
    # p1, p2 have results; p3 is empty and stops the walk.
    assert [c["page"] for c in calls] == ["1", "2", "3"]


# -- caching / resilience --------------------------------------------------------


def test_pages_cached_across_repeated_searches():
    calls: list[dict] = []
    adapter = make_adapter(calls=calls, cache_ttl=1000)
    list(adapter.search(query()))
    first_round = len(calls)
    list(adapter.search(query()))
    # Identical search: every page served from cache, no new upstream calls.
    assert len(calls) == first_round


def test_no_keywords_raises_source_error():
    adapter = make_adapter()
    with pytest.raises(SourceError):
        list(adapter.search(query(keywords=[])))


def _adapter_with_status(status: int) -> FourChanAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    inner = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "t"})
    http = HttpClient(
        user_agent="t", client=inner, sleep=lambda s: None, rng=random.Random(0), max_retries=0
    )
    return FourChanAdapter(http, default_boards=["pol"], clock=lambda: FIXED_NOW)


def test_404_page_is_skipped_not_fatal():
    # A missing endpoint (e.g. archive down for a board) yields no results rather than
    # raising — the pipeline records this source as simply empty.
    assert list(_adapter_with_status(404).search(query())) == []


def test_persistent_server_error_propagates_as_source_error():
    # A 5xx that survives retries surfaces as the typed SourceError the pipeline expects.
    with pytest.raises(SourceError):
        list(_adapter_with_status(503).search(query()))
