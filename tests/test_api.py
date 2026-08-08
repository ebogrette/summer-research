"""HTTP API: routes, request validation, response envelope, auth (PLAN §6.2).

Uses Flask's test client against a `create_app` wired to fake adapters, so the whole
stack — parsing, pipeline, serialization — runs offline. The envelope shape and the
status-code policy (400/401/502/200-with-errors) are the contract under test.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from keyword_scrub.api import create_app
from keyword_scrub.config import Settings
from keyword_scrub.errors import AuthError, NotConfigured
from keyword_scrub.models import Post, SearchQuery, SourceInfo
from keyword_scrub.registry import Registry
from keyword_scrub.sources.base import SearchCapability

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_post(source: str, id: str, *, minutes: int = 0) -> Post:
    return Post(
        source=source,
        id=id,
        url=f"https://example.invalid/{source}/{id}",
        body=f"post {id}",
        created_at=BASE + timedelta(minutes=minutes),
        fetched_at=BASE,
        match_field="native",
        matched_keywords=["x"],
        raw={"secret": id},
    )


class FakePost:
    capability = SearchCapability.NATIVE
    requires_auth = False

    def __init__(self, name, posts=None, *, raises=None, configured=True):
        self.name = name
        self._posts = posts or []
        self._raises = raises
        self._configured = configured
        self.calls: list[SearchQuery] = []

    def is_configured(self):
        return self._configured

    def describe(self):
        return SourceInfo(
            name=self.name,
            capability=str(self.capability),
            requires_auth=self.requires_auth,
            configured=self._configured,
        )

    def search(self, q) -> Iterator[Post]:
        self.calls.append(q)
        if self._raises is not None:
            raise self._raises
        yield from self._posts


def make_client(*adapters, settings=None):
    settings = settings or Settings()
    registry = Registry(http=None, adapters=adapters)  # type: ignore[arg-type]
    app = create_app(settings=settings, registry=registry)
    app.config.update(TESTING=True)
    return app.test_client()


# -- health / sources ------------------------------------------------------------


def test_health():
    client = make_client()
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert "version" in body


def test_sources_lists_adapters():
    client = make_client(
        FakePost("reddit", configured=True), FakePost("twitter", configured=False)
    )
    body = client.get("/sources").get_json()
    names = {s["name"]: s for s in body["sources"]}
    assert names["reddit"]["configured"] is True
    assert names["twitter"]["configured"] is False
    assert names["reddit"]["capability"] == "native"


# -- GET /search -----------------------------------------------------------------


def test_search_returns_envelope():
    a = FakePost("reddit", [make_post("reddit", "1", minutes=0)])
    b = FakePost("4chan", [make_post("4chan", "2", minutes=5)])
    client = make_client(a, b)

    body = client.get("/search?q=foo&sources=reddit,4chan").get_json()
    assert body["count"] == 2
    assert [p["id"] for p in body["posts"]] == ["2", "1"]  # newest first
    assert body["query"]["keywords"] == ["foo"]
    assert body["sources"]["reddit"]["status"] == "ok"
    assert body["sources"]["4chan"]["count"] == 1
    assert "elapsed_ms" in body


def test_search_keywords_comma_and_repeated():
    a = FakePost("reddit", [make_post("reddit", "1")])
    client = make_client(a)

    comma = client.get("/search?q=a,b").get_json()
    assert comma["query"]["keywords"] == ["a", "b"]

    repeated = client.get("/search?q=a&q=b").get_json()
    assert repeated["query"]["keywords"] == ["a", "b"]

    # The query reached the adapter as parsed.
    assert a.calls[-1].keywords == ["a", "b"]


def test_search_missing_q_is_400():
    client = make_client(FakePost("reddit"))
    resp = client.get("/search")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "invalid_request"


def test_search_bad_mode_is_400():
    client = make_client(FakePost("reddit"))
    resp = client.get("/search?q=x&mode=bogus")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["details"] == {"mode": "bogus"}


def test_search_bad_limit_is_400():
    client = make_client(FakePost("reddit"))
    assert client.get("/search?q=x&limit=0").status_code == 400
    assert client.get("/search?q=x&limit=99999").status_code == 400
    assert client.get("/search?q=x&limit=nope").status_code == 400


def test_since_after_until_is_400():
    client = make_client(FakePost("reddit"))
    resp = client.get("/search?q=x&since=2024-02-01&until=2024-01-01")
    assert resp.status_code == 400


def test_include_raw_toggles_raw_field():
    a = FakePost("reddit", [make_post("reddit", "1")])
    client = make_client(a)

    without = client.get("/search?q=x").get_json()
    assert "raw" not in without["posts"][0]

    withraw = client.get("/search?q=x&include_raw=true").get_json()
    assert withraw["posts"][0]["raw"] == {"secret": "1"}


def test_since_until_parsed_to_utc():
    a = FakePost("reddit", [make_post("reddit", "1")])
    client = make_client(a)
    client.get("/search?q=x&since=2024-01-01T00:00:00Z&until=2024-06-01")
    q = a.calls[-1]
    assert q.since == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert q.until.tzinfo is not None


# -- POST /search ----------------------------------------------------------------


def test_post_search_json_body():
    a = FakePost("reddit", [make_post("reddit", "1")])
    client = make_client(a)
    resp = client.post("/search", json={"q": ["a", "b"], "mode": "all", "limit": 10})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["query"]["mode"] == "all"
    assert a.calls[-1].keywords == ["a", "b"]
    assert a.calls[-1].limit == 10


def test_post_search_non_object_body_is_400():
    client = make_client(FakePost("reddit"))
    resp = client.post("/search", json=[1, 2, 3])
    assert resp.status_code == 400


# -- partial failure & status codes ----------------------------------------------


def test_partial_failure_is_200_with_error_block():
    ok = FakePost("reddit", [make_post("reddit", "1")])
    bad = FakePost("4chan", raises=AuthError("boom", source="4chan"))
    client = make_client(ok, bad)

    resp = client.get("/search?q=x")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["sources"]["4chan"]["status"] == "error"
    assert body["sources"]["reddit"]["status"] == "ok"
    assert body["count"] == 1


def test_all_sources_failed_is_502():
    a = FakePost("reddit", raises=AuthError("a", source="reddit"))
    b = FakePost("4chan", raises=AuthError("b", source="4chan"))
    client = make_client(a, b)

    resp = client.get("/search?q=x")
    assert resp.status_code == 502
    assert resp.get_json()["count"] == 0


def test_unavailable_source_does_not_trigger_502():
    unavailable = FakePost("twitter", raises=NotConfigured("no token", source="twitter"))
    client = make_client(unavailable)

    resp = client.get("/search?q=x")
    # Unavailable is not "error", so this is a 200 even though nothing was returned.
    assert resp.status_code == 200
    assert resp.get_json()["sources"]["twitter"]["status"] == "unavailable"


# -- API key auth ----------------------------------------------------------------


def test_api_key_required_when_configured():
    settings = Settings(api_key="s3cret")
    client = make_client(FakePost("reddit", [make_post("reddit", "1")]), settings=settings)

    assert client.get("/search?q=x").status_code == 401
    assert client.get("/sources").status_code == 401
    # Health is always open.
    assert client.get("/health").status_code == 200

    ok = client.get("/search?q=x", headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


def test_api_open_when_no_key_set():
    client = make_client(FakePost("reddit", [make_post("reddit", "1")]))
    assert client.get("/search?q=x").status_code == 200
