"""Request validation and response shaping for the HTTP API (PLAN §6.2).

Parsing lives here, not in `routes.py`, so both the query-string (`GET /search`) and
JSON-body (`POST /search`) paths share one set of rules and one error type. A bad
request raises `ValidationError`, which the route layer renders as a 400 with the
consistent `{"error": {code, message, details}}` envelope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..models import SearchQuery

_MODES = {"any", "all", "phrase"}
_SORTS = {"created_at", "score"}
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 100


class ValidationError(Exception):
    """A request couldn't be turned into a `SearchQuery`. Rendered as HTTP 400."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ParsedSearch:
    """A validated search request: the query plus response-shaping options."""

    query: SearchQuery
    sort: str
    include_raw: bool


def _as_list(value: Any) -> list[str]:
    """Normalize a scalar, comma string, or sequence into a list of trimmed tokens."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence):
        out: list[str] = []
        for item in value:
            # Each element may itself be comma-joined (repeated `?q=a,b&q=c`).
            out.extend(_as_list(item) if isinstance(item, str) else [str(item)])
        return out
    return [str(value)]


def _parse_bool(value: Any, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValidationError(f"{field} must be a boolean", details={field: value})


def _parse_int(value: Any, *, field: str, default: int, lo: int, hi: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an integer", details={field: value}) from exc
    if parsed < lo or parsed > hi:
        raise ValidationError(
            f"{field} must be between {lo} and {hi}", details={field: parsed}
        )
    return parsed


def _parse_dt(value: Any, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        # Accept a trailing `Z` (ISO-8601 UTC), which fromisoformat rejects pre-3.11
        # semantics; normalize it to an explicit offset.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(
                f"{field} must be ISO-8601", details={field: value}
            ) from exc
    # Naive input is interpreted as UTC; everything downstream is tz-aware UTC.
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_choice(value: Any, *, field: str, choices: set[str], default: str) -> str:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text not in choices:
        raise ValidationError(
            f"{field} must be one of {sorted(choices)}", details={field: value}
        )
    return text


def parse_search(raw: Mapping[str, Any], *, multi: Mapping[str, list[str]] | None = None) -> ParsedSearch:
    """Build a `ParsedSearch` from request data.

    `raw` is the flat mapping (JSON body or `request.args`). `multi` optionally carries
    repeated query-string keys (e.g. `?q=a&q=b`) so `q`, `sources`, and `containers` can
    arrive either repeated or comma-joined.
    """
    def _field(key: str) -> Any:
        if multi is not None and key in multi and len(multi.get(key, [])) > 1:
            return multi[key]
        return raw.get(key)

    keywords = _as_list(_field("q") if raw.get("keywords") is None else raw.get("keywords"))
    if not keywords:
        raise ValidationError("at least one keyword is required", details={"q": None})

    sources = _as_list(_field("sources")) or None
    containers = _as_list(_field("containers")) or None

    query = SearchQuery(
        keywords=keywords,
        mode=_parse_choice(raw.get("mode"), field="mode", choices=_MODES, default="any"),
        sources=sources,
        containers=containers,
        since=_parse_dt(raw.get("since"), field="since"),
        until=_parse_dt(raw.get("until"), field="until"),
        limit=_parse_int(
            raw.get("limit"), field="limit", default=_DEFAULT_LIMIT, lo=1, hi=_MAX_LIMIT
        ),
        include_replies=_parse_bool(
            raw.get("include_replies"), field="include_replies", default=True
        ),
        case_sensitive=_parse_bool(
            raw.get("case_sensitive"), field="case_sensitive", default=False
        ),
    )

    if query.since and query.until and query.since > query.until:
        raise ValidationError(
            "since must not be after until",
            details={"since": raw.get("since"), "until": raw.get("until")},
        )

    return ParsedSearch(
        query=query,
        sort=_parse_choice(raw.get("sort"), field="sort", choices=_SORTS, default="created_at"),
        include_raw=_parse_bool(raw.get("include_raw"), field="include_raw", default=False),
    )
