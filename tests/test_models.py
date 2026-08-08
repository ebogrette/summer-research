"""Tests for the normalized record contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from keyword_scrub.models import (
    MediaRef,
    Post,
    SearchQuery,
    SearchResult,
    SourceStatus,
)


def _post(**over) -> Post:
    base = dict(
        source="reddit",
        id="abc123",
        url="https://reddit.com/r/news/comments/abc123",
        body="hello world",
        created_at=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        fetched_at=datetime(2024, 1, 2, 3, 4, 9, tzinfo=timezone.utc),
        match_field="native",
    )
    base.update(over)
    return Post(**base)


def test_key_is_source_colon_id():
    assert _post().key == "reddit:abc123"


def test_timestamps_serialize_as_iso8601_z():
    d = _post().to_dict()
    assert d["created_at"] == "2024-01-02T03:04:05Z"
    assert d["fetched_at"] == "2024-01-02T03:04:09Z"


def test_naive_offset_converted_to_utc_z():
    from datetime import timedelta, timezone as tz

    est = tz(timedelta(hours=-5))
    p = _post(created_at=datetime(2024, 1, 2, 0, 0, 0, tzinfo=est))
    assert p.to_dict()["created_at"] == "2024-01-02T05:00:00Z"


def test_raw_omitted_unless_requested():
    p = _post(raw={"secret": 1})
    assert "raw" not in p.to_dict()
    assert p.to_dict(include_raw=True)["raw"] == {"secret": 1}


def test_round_trips_to_json():
    p = _post(
        media=[MediaRef(url="https://i/1.jpg", kind="image", width=800, height=600)],
        matched_keywords=["world"],
        score=0,
        reply_count=None,
    )
    blob = json.dumps(p.to_dict())
    back = json.loads(blob)
    assert back["media"][0]["width"] == 800
    # None vs 0 preserved through JSON.
    assert back["score"] == 0
    assert back["reply_count"] is None


def test_missing_vs_zero_distinct():
    p = _post(score=0, reply_count=None)
    d = p.to_dict()
    assert d["score"] == 0
    assert d["reply_count"] is None


def test_search_result_envelope_shape():
    result = SearchResult(
        posts=[_post()],
        sources={"reddit": SourceStatus("ok", count=1, elapsed_ms=10)},
        query=SearchQuery(keywords=["world"]),
        elapsed_ms=12,
    )
    d = result.to_dict()
    assert d["count"] == 1
    assert d["sources"]["reddit"] == {"status": "ok", "count": 1, "elapsed_ms": 10}
    assert d["query"]["keywords"] == ["world"]
    assert d["elapsed_ms"] == 12


def test_source_status_unavailable_reports_reason():
    st = SourceStatus("unavailable", reason="not_configured")
    assert st.to_dict() == {"status": "unavailable", "reason": "not_configured"}
