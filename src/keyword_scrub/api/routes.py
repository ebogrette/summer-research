"""HTTP routes (PLAN §6.2).

A Flask blueprint carrying the read endpoints. The app factory in `__init__.py` wires a
`Registry` and `Settings` onto `app.extensions["keyword_scrub"]`; handlers read from
there so the same blueprint works against a real registry in production and a fixture
one in tests.

Every response — success or error — uses the envelopes defined in PLAN §6.2:
`SearchResult.to_dict()` for search, and `{"error": {code, message, details}}` for
failures, with the status codes the plan specifies (400 validation, 401 auth, 502 when
every source failed, 200 with a populated per-source block when only some did).
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .. import __version__
from ..pipeline import run_search
from ..registry import Registry
from .schemas import ParsedSearch, ValidationError, parse_search

bp = Blueprint("keyword_scrub", __name__)


def _ctx() -> dict[str, Any]:
    return current_app.extensions["keyword_scrub"]


def _registry() -> Registry:
    return _ctx()["registry"]


def _error(code: str, message: str, status: int, *, details: Any = None):
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return jsonify(body), status


# -- auth ----------------------------------------------------------------------------


def _check_api_key() -> tuple[Any, int] | None:
    """Enforce the optional static API key (PLAN §9.4).

    When `settings.api_key` is unset the API is open (fine for localhost); when set,
    every request must present it in `X-API-Key`. Returns an error response to short-
    circuit the request, or `None` to allow it.
    """
    expected = _ctx()["settings"].api_key
    if not expected:
        return None
    if request.headers.get("X-API-Key") != expected:
        return _error("unauthorized", "missing or invalid API key", 401)
    return None


# -- endpoints -----------------------------------------------------------------------


@bp.get("/health")
def health():
    return jsonify({"status": "ok", "version": __version__})


@bp.get("/sources")
def sources():
    guard = _check_api_key()
    if guard is not None:
        return guard
    infos = _registry().describe_all()
    return jsonify({"sources": [info.to_dict() for info in infos]})


@bp.get("/search")
def search_get():
    guard = _check_api_key()
    if guard is not None:
        return guard
    try:
        parsed = parse_search(request.args, multi=request.args.to_dict(flat=False))
    except ValidationError as exc:
        return _error("invalid_request", exc.message, 400, details=exc.details)
    return _run(parsed)


@bp.post("/search")
def search_post():
    guard = _check_api_key()
    if guard is not None:
        return guard
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("invalid_request", "request body must be a JSON object", 400)
    try:
        parsed = parse_search(payload)
    except ValidationError as exc:
        return _error("invalid_request", exc.message, 400, details=exc.details)
    return _run(parsed)


def _run(parsed: ParsedSearch):
    result = run_search(parsed.query, _registry(), sort=parsed.sort)
    body = result.to_dict(include_raw=parsed.include_raw)

    # 502 only when every source that ran failed outright; a partial failure is a 200
    # with the per-source block telling the caller what happened (PLAN §6.2).
    statuses = result.sources
    if statuses and all(s.status == "error" for s in statuses.values()):
        return jsonify(body), 502
    return jsonify(body), 200
