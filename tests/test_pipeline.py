"""Pipeline fan-out, merge, dedupe, sort, and partial-failure isolation (PLAN §6.1).

Driven by in-memory fake adapters rather than the network: the pipeline's job is
orchestration, and its contract is legible partial failure, so the tests exercise that
directly without dragging HTTP into it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from keyword_scrub.errors import AuthError, NotConfigured, SourceError
from keyword_scrub.models import Post, SearchQuery, SourceInfo
from keyword_scrub.pipeline import run_search
from keyword_scrub.registry import Registry
from keyword_scrub.sources.base import SearchCapability

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_post(source: str, id: str, *, minutes: int = 0, score: int | None = None) -> Post:
    return Post(
        source=source,
        id=id,
        url=f"https://example.invalid/{source}/{id}",
        body=f"post {id}",
        created_at=BASE + timedelta(minutes=minutes),
        fetched_at=BASE,
        match_field="native",
        score=score,
    )


class FakePost:
    """A stand-in adapter yielding a fixed list of posts (or raising)."""

    capability = SearchCapability.NATIVE
    requires_auth = False

    def __init__(
        self,
        name: str,
        posts: list[Post] | None = None,
        *,
        raises: Exception | None = None,
        delay: float = 0.0,
        configured: bool = True,
    ) -> None:
        self.name = name
        self._posts = posts or []
        self._raises = raises
        self._delay = delay
        self._configured = configured
        self.calls: list[SearchQuery] = []

    def is_configured(self) -> bool:
        return self._configured

    def describe(self) -> SourceInfo:
        return SourceInfo(
            name=self.name,
            capability=str(self.capability),
            requires_auth=self.requires_auth,
            configured=self._configured,
        )

    def search(self, q: SearchQuery) -> Iterator[Post]:
        self.calls.append(q)
        if self._delay:
            time.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        yield from self._posts


def registry_of(*adapters) -> Registry:
    return Registry(http=None, adapters=adapters)  # type: ignore[arg-type]


def query(**kwargs) -> SearchQuery:
    kwargs.setdefault("keywords", ["x"])
    return SearchQuery(**kwargs)


# -- merge / sort / dedupe -------------------------------------------------------


def test_merges_and_sorts_by_created_at_desc():
    a = FakePost("reddit", [make_post("reddit", "1", minutes=0)])
    b = FakePost("4chan", [make_post("4chan", "2", minutes=10)])
    result = run_search(query(), registry_of(a, b))

    assert [p.id for p in result.posts] == ["2", "1"]  # newest first
    assert result.sources["reddit"].status == "ok"
    assert result.sources["4chan"].count == 1


def test_sort_by_score():
    a = FakePost("reddit", [make_post("reddit", "1", minutes=99, score=5)])
    b = FakePost("4chan", [make_post("4chan", "2", minutes=0, score=50)])
    result = run_search(query(), registry_of(a, b), sort="score")
    assert [p.id for p in result.posts] == ["2", "1"]  # highest score first


def test_sort_by_score_sinks_none_below_zero():
    scored = FakePost("reddit", [make_post("reddit", "s", score=0)])
    unscored = FakePost("4chan", [make_post("4chan", "n", score=None)])
    result = run_search(query(), registry_of(scored, unscored), sort="score")
    assert [p.id for p in result.posts] == ["s", "n"]


def test_dedupes_on_source_and_id():
    dup = make_post("reddit", "1")
    a = FakePost("reddit", [dup, dup])
    result = run_search(query(), registry_of(a))
    assert len(result.posts) == 1


def test_same_id_across_sources_is_not_a_collision():
    a = FakePost("reddit", [make_post("reddit", "1")])
    b = FakePost("4chan", [make_post("4chan", "1")])
    result = run_search(query(), registry_of(a, b))
    assert {p.key for p in result.posts} == {"reddit:1", "4chan:1"}


def test_global_limit_applied_after_ranking():
    posts = [make_post("reddit", str(i), minutes=i) for i in range(5)]
    a = FakePost("reddit", posts)
    result = run_search(query(), registry_of(a), global_limit=2)
    assert [p.id for p in result.posts] == ["4", "3"]  # top 2 newest


def test_per_source_limit_caps_the_stream():
    posts = [make_post("reddit", str(i), minutes=i) for i in range(10)]
    a = FakePost("reddit", posts)
    result = run_search(query(limit=3), registry_of(a))
    assert result.sources["reddit"].count == 3


# -- partial failure isolation ---------------------------------------------------


def test_not_configured_reports_unavailable_not_error():
    ok = FakePost("reddit", [make_post("reddit", "1")])
    bad = FakePost("twitter", raises=NotConfigured("no token", source="twitter"))
    result = run_search(query(), registry_of(ok, bad))

    assert result.sources["twitter"].status == "unavailable"
    assert result.sources["twitter"].reason == "not_configured"
    assert result.sources["reddit"].status == "ok"
    assert len(result.posts) == 1  # the good source still contributes


def test_source_error_is_isolated():
    ok = FakePost("reddit", [make_post("reddit", "1")])
    bad = FakePost("4chan", raises=AuthError("boom", source="4chan"))
    result = run_search(query(), registry_of(ok, bad))

    assert result.sources["4chan"].status == "error"
    assert result.sources["4chan"].reason == "auth_error"
    assert [p.id for p in result.posts] == ["1"]


def test_untyped_exception_is_caught_and_labeled():
    bad = FakePost("reddit", raises=RuntimeError("leaked"))
    result = run_search(query(), registry_of(bad))
    assert result.sources["reddit"].status == "error"
    assert result.sources["reddit"].reason == "source_error"


def test_timeout_marks_source_error():
    slow = FakePost("reddit", [make_post("reddit", "1")], delay=0.5)
    fast = FakePost("4chan", [make_post("4chan", "2")])
    result = run_search(query(), registry_of(slow, fast), per_source_timeout=0.05)

    assert result.sources["reddit"].reason == "timeout"
    assert result.sources["4chan"].status == "ok"
    assert [p.id for p in result.posts] == ["2"]


# -- source resolution -----------------------------------------------------------


def test_query_sources_restricts_fan_out():
    a = FakePost("reddit", [make_post("reddit", "1")])
    b = FakePost("4chan", [make_post("4chan", "2")])
    result = run_search(query(sources=["reddit"]), registry_of(a, b))

    assert "4chan" not in result.sources
    assert b.calls == []  # never invoked


def test_no_resolved_sources_yields_empty_result():
    a = FakePost("reddit", [make_post("reddit", "1")])
    result = run_search(query(sources=["nonesuch"]), registry_of(a))
    assert result.posts == []
    assert result.sources == {}


def test_result_echoes_query_and_reports_elapsed():
    a = FakePost("reddit", [make_post("reddit", "1")])
    q = query(mode="all", keywords=["a", "b"])
    result = run_search(q, registry_of(a))
    assert result.query is q
    assert result.elapsed_ms >= 0
