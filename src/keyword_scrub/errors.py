"""Typed errors. Adapters raise only these; raw httpx/requests exceptions never escape.

Keeping this contract narrow is what stops one flaky source from taking down a query:
the pipeline catches `SourceError` and records it per-source instead of failing.
"""

from __future__ import annotations


class SourceError(Exception):
    """Base for every adapter-raised error.

    `source` is the adapter name so the pipeline can attribute the failure. `code`
    is a short machine-readable slug surfaced in the API's per-source status block.
    """

    code: str = "source_error"

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.source = source


class NotConfigured(SourceError):
    """The adapter is missing required credentials/config and cannot run.

    This is not a failure of a request — the pipeline reports the source as
    `unavailable`, not `error`.
    """

    code = "not_configured"


class AuthError(SourceError):
    """Authentication with the upstream platform failed (bad token, refused grant)."""

    code = "auth_error"


class RateLimited(SourceError):
    """The upstream platform rate-limited us.

    `retry_after` carries the server's advice (seconds) when available.
    """

    code = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, source=source)
        self.retry_after = retry_after
