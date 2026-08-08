"""Env-backed settings. Plain dataclass over `os.environ` — no pydantic dependency
until there's a reason for one.

`Settings.from_env()` reads the process environment (optionally after loading a
`.env` file). Per-source `is_configured` helpers let adapters decide availability
without reaching into raw env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str | os.PathLike[str]) -> None:
    """Minimal `.env` loader: `KEY=VALUE` lines, `#` comments, no interpolation.

    Only sets keys not already present in the environment, so real env vars win.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Reddit
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "keyword-scrub/0.1"

    # Twitter
    twitter_bearer_token: str | None = None

    # 4chan — must be boards the 4plebs archive actually covers (see sources/fourchan.py)
    fourchan_default_boards: list[str] = field(default_factory=lambda: ["pol", "tv"])

    # HTTP / caching
    http_timeout: float = 20.0
    cache_ttl: float = 60.0
    http_user_agent: str = "keyword-scrub/0.1 (+https://example.invalid/keyword-scrub)"

    # Persistence
    database_url: str = "sqlite:///keyword_scrub.db"

    # API auth
    api_key: str | None = None

    @classmethod
    def from_env(cls, *, dotenv_path: str | os.PathLike[str] | None = ".env") -> "Settings":
        if dotenv_path is not None:
            _load_dotenv(dotenv_path)

        def _get(key: str) -> str | None:
            val = os.environ.get(key)
            return val if val else None

        boards_raw = os.environ.get("FOURCHAN_DEFAULT_BOARDS")
        boards = _split_csv(boards_raw) if boards_raw else ["pol", "tv"]

        return cls(
            reddit_client_id=_get("REDDIT_CLIENT_ID"),
            reddit_client_secret=_get("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=_get("REDDIT_USER_AGENT") or "keyword-scrub/0.1",
            twitter_bearer_token=_get("TWITTER_BEARER_TOKEN"),
            fourchan_default_boards=boards,
            http_timeout=float(os.environ.get("HTTP_TIMEOUT", "20") or "20"),
            cache_ttl=float(os.environ.get("CACHE_TTL", "60") or "60"),
            http_user_agent=(
                _get("HTTP_USER_AGENT")
                or "keyword-scrub/0.1 (+https://example.invalid/keyword-scrub)"
            ),
            database_url=_get("DATABASE_URL") or "sqlite:///keyword_scrub.db",
            api_key=_get("API_KEY"),
        )

    # --- per-source availability -------------------------------------------------

    @property
    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def twitter_configured(self) -> bool:
        return bool(self.twitter_bearer_token)
