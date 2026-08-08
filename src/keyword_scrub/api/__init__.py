"""Flask application factory (PLAN §6.2).

`create_app()` builds the JSON API: it resolves `Settings`, constructs the source
`Registry` (shared `HttpClient`, rate limiters, adapters), stashes both on
`app.extensions["keyword_scrub"]`, and registers the routes blueprint.

Tests inject their own `settings`/`registry` to run fully offline; production calls
`create_app()` with no arguments and lets it read the environment.
"""

from __future__ import annotations

from flask import Flask

from ..config import Settings
from ..registry import Registry
from .routes import bp


def create_app(
    *, settings: Settings | None = None, registry: Registry | None = None
) -> Flask:
    settings = settings or Settings.from_env()
    registry = registry or Registry.from_settings(settings)

    app = Flask(__name__)
    app.extensions["keyword_scrub"] = {"settings": settings, "registry": registry}
    app.register_blueprint(bp)
    return app


__all__ = ["create_app"]
