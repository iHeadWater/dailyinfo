"""Tests for ``scripts/run_pipelines.py``."""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from conftest import FIXTURES_DIR, FakeResponse, read_fixture


def test_save_writes_under_briefings_dir(monkeypatch):
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    full = rp.save("papers", "demo.md", "hello world")

    path = BRIEFINGS_DIR / "papers" / "demo.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "hello world"
    assert full == str(path)


def test_already_pushed_within_detects_recent_file():
    import run_pipelines as rp
    from paths import PUSHED_DIR

    category_dir = PUSHED_DIR / "papers"
    category_dir.mkdir(parents=True, exist_ok=True)
    target = category_dir / "foo_briefing_2024-01-01.md"
    target.write_text("content", encoding="utf-8")

    assert rp._already_pushed_within("foo", "papers", lookback_hours=48) is True


def test_already_pushed_within_false_for_old_file():
    import run_pipelines as rp
    from paths import PUSHED_DIR

    category_dir = PUSHED_DIR / "papers"
    category_dir.mkdir(parents=True, exist_ok=True)
    target = category_dir / "foo_briefing_old.md"
    target.write_text("content", encoding="utf-8")

    # Backdate mtime past the lookback window.
    long_ago = time.time() - 72 * 3600
    import os

    os.utime(target, (long_ago, long_ago))

    assert rp._already_pushed_within("foo", "papers", lookback_hours=24) is False


def test_already_pushed_within_false_when_dir_missing():
    import run_pipelines as rp

    assert rp._already_pushed_within("ghost", "nope", lookback_hours=24) is False


def test_already_pushed_within_ignores_other_source_prefix():
    import run_pipelines as rp
    from paths import PUSHED_DIR

    category_dir = PUSHED_DIR / "papers"
    category_dir.mkdir(parents=True, exist_ok=True)
    (category_dir / "other_briefing_2024-01-01.md").write_text("x", encoding="utf-8")

    assert rp._already_pushed_within("foo", "papers", lookback_hours=24) is False


def _write_env(tmp_path, contents: str):
    env_path = tmp_path / ".env"
    env_path.write_text(contents, encoding="utf-8")
    return env_path


def test_get_freshrss_user_reads_env_file(tmp_path, monkeypatch):
    import run_pipelines as rp

    _write_env(tmp_path, "FRESHRSS_USER=alice\n")
    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))

    assert rp._get_freshrss_user() == "alice"


def test_get_freshrss_user_falls_back_to_sources_json(tmp_path, monkeypatch):
    import run_pipelines as rp

    _write_env(tmp_path, "")  # no FRESHRSS_USER line
    sources = tmp_path / "sources.json"
    sources.write_text('{"defaults": {"freshrss_user": "from-json"}}', encoding="utf-8")

    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(rp, "SOURCES_JSON", str(sources))

    assert rp._get_freshrss_user() == "from-json"


def test_get_freshrss_user_falls_back_to_env_user(tmp_path, monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(rp, "SOURCES_JSON", str(tmp_path / "missing.json"))
    monkeypatch.setenv("USER", "fallback-user")

    assert rp._get_freshrss_user() == "fallback-user"


def test_load_api_key_from_env_var_when_no_dotenv(tmp_path, monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))  # empty dir → no .env
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-env")

    assert rp.load_api_key() == "sk-test-env"


def test_load_api_key_exits_when_missing(tmp_path, monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(SystemExit):
        rp.load_api_key()


def test_load_api_key_prefers_dotenv_over_env(tmp_path, monkeypatch):
    import run_pipelines as rp

    _write_env(tmp_path, "OPENROUTER_API_KEY=sk-from-dotenv\n")
    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-from-env")

    assert rp.load_api_key() == "sk-from-dotenv"


def test_load_api_key_skips_placeholder_values(tmp_path, monkeypatch):
    import run_pipelines as rp

    _write_env(tmp_path, "OPENROUTER_API_KEY=your_api_key_here\n")
    monkeypatch.setattr(rp, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-real")

    assert rp.load_api_key() == "sk-real"


def test_run_pipeline_2_smoke(monkeypatch, fake_requests, fake_call_ai):
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))

    fake_requests.register(
        "https://github.com/trending",
        FakeResponse(status=200, text=read_fixture("github_trending.html")),
    )

    saved = rp.run_pipeline_2()

    assert saved == 1
    today = datetime.now().strftime("%Y-%m-%d")
    out_file = BRIEFINGS_DIR / "code" / f"github_trending_briefing_{today}.md"
    assert out_file.exists()
    body = out_file.read_text(encoding="utf-8")
    assert body.startswith("# GitHub Trending")
    assert "[AI-SUMMARY]" in body


def test_run_pipeline_2_skips_when_fetch_fails(
    monkeypatch, fake_requests, fake_call_ai
):
    """When the scraper raises, pipeline logs and continues without saving."""
    import requests

    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))

    def boom(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "get", boom)

    saved = rp.run_pipeline_2()
    assert saved == 0
    today = datetime.now().strftime("%Y-%m-%d")
    assert not (
        BRIEFINGS_DIR / "code" / f"github_trending_briefing_{today}.md"
    ).exists()
