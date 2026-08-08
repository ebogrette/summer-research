"""Tests for env-backed settings."""

from __future__ import annotations

from keyword_scrub.config import Settings


def test_defaults_when_env_empty(monkeypatch):
    for key in (
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "TWITTER_BEARER_TOKEN",
        "FOURCHAN_DEFAULT_BOARDS",
        "HTTP_TIMEOUT",
        "API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env(dotenv_path=None)
    assert not s.reddit_configured
    assert not s.twitter_configured
    assert s.fourchan_default_boards == ["pol", "tv"]
    assert s.http_timeout == 20.0


def test_reddit_configured_requires_both(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    assert not Settings.from_env(dotenv_path=None).reddit_configured

    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    assert Settings.from_env(dotenv_path=None).reddit_configured


def test_twitter_configured_from_token(monkeypatch):
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "abc")
    assert Settings.from_env(dotenv_path=None).twitter_configured


def test_boards_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("FOURCHAN_DEFAULT_BOARDS", "pol, g ,news")
    s = Settings.from_env(dotenv_path=None)
    assert s.fourchan_default_boards == ["pol", "g", "news"]


def test_dotenv_loaded_but_real_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('REDDIT_CLIENT_ID=fromfile\nHTTP_TIMEOUT="30"\n# comment\n')
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.setenv("HTTP_TIMEOUT", "99")  # real env should win over file
    s = Settings.from_env(dotenv_path=env)
    assert s.reddit_client_id == "fromfile"
    assert s.http_timeout == 99.0
