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


def test_has_real_briefing_today_detects_existing_content():
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"foo_briefing_{today}.md").write_text(
        "# Foo\n\nReal AI summary content.\n", encoding="utf-8"
    )

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._has_real_briefing_today("foo", "papers") is True


def test_has_real_briefing_today_false_for_placeholder_only():
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"foo_briefing_{today}.md").write_text(
        f"# Foo - {today}\n\n📭 过去 24 小时无新内容\n", encoding="utf-8"
    )

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._has_real_briefing_today("foo", "papers") is False


def test_has_real_briefing_today_false_when_dir_missing():
    import run_pipelines as rp

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._has_real_briefing_today("ghost", "nope") is False


def test_has_real_briefing_today_detects_archived_file_in_pushed():
    """After ``dailyinfo push`` moves files to PUSHED_DIR, a re-run of the
    pipeline should still see the briefing and skip regeneration."""
    import run_pipelines as rp
    from paths import PUSHED_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    cat_dir = PUSHED_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"foo_briefing_{today}.md").write_text(
        "# Foo\n\nArchived real content.\n", encoding="utf-8"
    )

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._has_real_briefing_today("foo", "papers") is True


def test_has_real_briefing_today_false_when_neither_dir_has_file():
    """Both BRIEFINGS_DIR and PUSHED_DIR empty for this source → not skipped."""
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR, PUSHED_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    for base in (BRIEFINGS_DIR, PUSHED_DIR):
        cat_dir = base / "papers"
        cat_dir.mkdir(parents=True, exist_ok=True)
        # Unrelated source present in both dirs — must not trigger a skip.
        (cat_dir / f"other_briefing_{today}.md").write_text("other", encoding="utf-8")

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._has_real_briefing_today("foo", "papers") is False


def test_has_real_briefing_today_force_all_bypasses_skip():
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"foo_briefing_{today}.md").write_text("real", encoding="utf-8")

    rp.FORCE_ALL = True
    rp.FORCE_SOURCES = set()
    try:
        assert rp._has_real_briefing_today("foo", "papers") is False
    finally:
        rp.FORCE_ALL = False


def test_has_real_briefing_today_force_named_source_bypasses_skip():
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    today = datetime.now().strftime("%Y-%m-%d")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"foo_briefing_{today}.md").write_text("real", encoding="utf-8")
    (cat_dir / f"bar_briefing_{today}.md").write_text("real", encoding="utf-8")

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = {"foo"}
    try:
        assert rp._has_real_briefing_today("foo", "papers") is False
        assert rp._has_real_briefing_today("bar", "papers") is True
    finally:
        rp.FORCE_SOURCES = set()


def test_resolve_fallback_model_explicit_arg_wins(monkeypatch):
    import run_pipelines as rp

    monkeypatch.setenv("DAILYINFO_FALLBACK_MODEL", "from-env/model")
    assert rp._resolve_fallback_model("explicit/model") == "explicit/model"


def test_resolve_fallback_model_env_override(monkeypatch):
    import run_pipelines as rp

    monkeypatch.setenv("DAILYINFO_FALLBACK_MODEL", "from-env/model")
    assert rp._resolve_fallback_model(None) == "from-env/model"


def test_resolve_fallback_model_default(monkeypatch):
    import run_pipelines as rp

    monkeypatch.delenv("DAILYINFO_FALLBACK_MODEL", raising=False)
    assert rp._resolve_fallback_model(None) == rp.DEFAULT_FALLBACK_MODEL


class _StubAIResponse:
    """Tiny stand-in for OpenRouter JSON responses used by call_ai tests."""

    def __init__(self, content: str = "", finish_reason: str = "stop"):
        self._payload = {
            "choices": [
                {
                    "message": {"content": content},
                    "finish_reason": finish_reason,
                }
            ]
        }

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _install_call_ai_stubs(monkeypatch, responses, logs):
    """Queue ``responses`` for successive requests.post calls and capture logs."""
    import run_pipelines as rp

    monkeypatch.setattr(rp, "API_KEY", "sk-test")
    monkeypatch.setattr(rp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(rp, "log", lambda msg: logs.append(msg))

    iterator = iter(responses)

    def fake_post(url, *args, **kwargs):
        return next(iterator)

    monkeypatch.setattr(rp.requests, "post", fake_post)


def test_call_ai_returns_primary_content_on_first_success(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    _install_call_ai_stubs(
        monkeypatch,
        [_StubAIResponse(content="hello world", finish_reason="stop")],
        logs,
    )

    assert rp.call_ai("prompt", model="primary/model") == "hello world"


def test_call_ai_falls_back_after_primary_empty_responses(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    responses = [
        _StubAIResponse(content="", finish_reason="length"),
        _StubAIResponse(content="", finish_reason="content_filter"),
        _StubAIResponse(content="", finish_reason="error"),
        _StubAIResponse(content="fallback reply", finish_reason="stop"),
    ]
    _install_call_ai_stubs(monkeypatch, responses, logs)

    result = rp.call_ai(
        "prompt", model="primary/model", fallback_model="fallback/model"
    )
    assert result == "fallback reply"
    joined = "\n".join(logs)
    assert "finish_reason=length" in joined
    assert "switching to fallback fallback/model" in joined
    assert "primary/model attempt 3/3" in joined


def test_call_ai_treats_length_finish_as_incomplete(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    responses = [
        _StubAIResponse(content="1. **Half", finish_reason="length"),
        _StubAIResponse(content="", finish_reason="length"),
        _StubAIResponse(content="", finish_reason="length"),
        _StubAIResponse(content="fallback complete", finish_reason="stop"),
    ]
    _install_call_ai_stubs(monkeypatch, responses, logs)

    result = rp.call_ai(
        "prompt", model="primary/model", fallback_model="fallback/model"
    )

    assert result == "fallback complete"
    assert "finish_reason=length" in "\n".join(logs)


def test_call_ai_raises_when_both_models_empty(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    responses = [_StubAIResponse(content="", finish_reason="error")] * 5
    _install_call_ai_stubs(monkeypatch, responses, logs)

    with pytest.raises(ValueError) as excinfo:
        rp.call_ai("prompt", model="primary/model", fallback_model="fallback/model")
    assert "primary/model" in str(excinfo.value)
    assert "fallback/model" in str(excinfo.value)


def test_validate_briefing_content_rejects_missing_items():
    import run_pipelines as rp

    content = "1. **A**\n   > 摘要。\n\n2. **B**\n   > 摘要。"

    with pytest.raises(rp.BriefingGenerationError):
        rp.validate_briefing_content(content, expected_count=3)


def test_validate_briefing_content_accepts_title_matches_without_numbering():
    import run_pipelines as rp

    content = (
        "## Briefing\n\n"
        "**Alpha Paper**\n摘要。\n\n"
        "**Beta Paper**\n摘要。\n\n"
        "**Gamma Paper**\n摘要。\n"
    )

    rp.validate_briefing_content(
        content,
        expected_count=3,
        expected_titles=["Alpha Paper", "Beta Paper", "Gamma Paper"],
    )


def test_validate_briefing_content_rejects_cutoff_markdown():
    import run_pipelines as rp

    with pytest.raises(rp.BriefingGenerationError):
        rp.validate_briefing_content("1. **A**\n   > 摘要。\n\n2. **N", 2)


def test_generate_regular_briefings_splits_incomplete_batch(monkeypatch):
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "demo", "display_name": "Demo", "category": "papers"},
        {"lookback_hours": 24},
    )
    items = [Item(title=f"Paper {i}", date="2026-04-25") for i in range(4)]
    calls = []

    def fake_call_ai(prompt, model="stub", max_tokens=0, **kwargs):
        count = prompt.count(". Paper")
        calls.append(count)
        if count > 2:
            return "1. **Paper 0**\n   > 摘要。"
        return "\n\n".join(f"{i + 1}. **Paper {i}**\n   > 摘要。" for i in range(count))

    monkeypatch.setattr(rp, "call_ai", fake_call_ai)
    monkeypatch.setattr(rp, "log", lambda *_: None)

    out = rp._generate_regular_briefings(
        ds,
        items,
        "请总结 {count} 篇 {display_name}：\n{article_list}\n{date}",
        "stub",
    )

    assert calls == [4, 2, 2]
    assert len(out) == 2


def _make_dlut_news_sources_json(path, templates_extra=None):
    """Write a minimal sources.json with two dlut_news group sources + one recruitment source."""
    from datetime import datetime

    now = datetime.now()
    fresh_day = now.strftime("%d")
    fresh_ym = now.strftime("%Y-%m")
    html = (
        read_fixture("dlut_news_snippet.html")
        .replace("{FRESH_DAY}", fresh_day)
        .replace("{FRESH_YM}", fresh_ym)
        .replace("{OLD_DAY}", "01")
        .replace("{OLD_YM}", "2020-01")
    )
    # write html to a known path so fake_requests can serve it
    (path.parent / "dlut_zhxw.html").write_text(html, encoding="utf-8")
    (path.parent / "dlut_xsky.html").write_text(html, encoding="utf-8")

    import json

    data = {
        "defaults": {"lookback_hours": 48, "model": "stub/model"},
        "prompt_templates": {
            "university_news_unified": "Unified news: {items}",
            "recruitment": "Recruitment: {items}",
        },
        "sources": [
            {
                "name": "dlut_zhxw",
                "display_name": "大连理工大学 - 综合新闻",
                "category": "resource",
                "enabled": True,
                "news_group": "dlut_news",
                "section": "综合新闻",
                "url": "https://news.dlut.test/zhxw.htm",
                "base_url": "https://news.dlut.test/",
                "selector": "li.bg-mask",
                "fields": {
                    "title": "h4 a",
                    "url": "h4 a[href]",
                    "date_day": "time > span",
                    "date_ym": "time",
                },
                "date_format": "dlut_news",
                "max_items": 10,
                "type": "scrape",
            },
            {
                "name": "dlut_xsky",
                "display_name": "大连理工大学 - 学术科研",
                "category": "resource",
                "enabled": True,
                "news_group": "dlut_news",
                "section": "学术科研",
                "url": "https://news.dlut.test/xsky.htm",
                "base_url": "https://news.dlut.test/",
                "selector": "li.bg-mask",
                "fields": {
                    "title": "h4 a",
                    "url": "h4 a[href]",
                    "date_day": "time > span",
                    "date_ym": "time",
                },
                "date_format": "dlut_news",
                "max_items": 10,
                "type": "scrape",
            },
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return html


def test_run_pipeline_3_unified_news_saves_single_file(
    monkeypatch, tmp_path, fake_requests, fake_call_ai
):
    """8 news sources → 1 dlut_news_briefing file, recruitment untouched."""
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    sources_json = tmp_path / "sources.json"
    html = _make_dlut_news_sources_json(sources_json)

    monkeypatch.setattr(rp, "SOURCES_JSON", str(sources_json))
    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()

    fake_requests.register("https://news.dlut.test/zhxw.htm", FakeResponse(200, html))
    fake_requests.register("https://news.dlut.test/xsky.htm", FakeResponse(200, html))

    saved = rp.run_pipeline_3()

    today = datetime.now().strftime("%Y-%m-%d")
    unified = BRIEFINGS_DIR / "resource" / f"dlut_news_briefing_{today}.md"
    assert unified.exists(), "expected unified briefing file"
    body = unified.read_text(encoding="utf-8")
    assert "大连理工大学校园动态" in body
    assert "[AI-SUMMARY]" in body

    # Individual source files must NOT exist
    for name in ("dlut_zhxw", "dlut_xsky"):
        assert not (
            BRIEFINGS_DIR / "resource" / f"{name}_briefing_{today}.md"
        ).exists(), f"{name} individual file should not exist"


def test_run_pipeline_3_unified_news_idempotent(
    monkeypatch, fake_requests, fake_call_ai
):
    """Second run on same day skips unified news generation."""
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))
    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()

    today = datetime.now().strftime("%Y-%m-%d")
    resource_dir = BRIEFINGS_DIR / "resource"
    resource_dir.mkdir(parents=True, exist_ok=True)
    existing = resource_dir / f"dlut_news_briefing_{today}.md"
    existing.write_text("# 大连理工大学校园动态\n\nReal content.\n", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise AssertionError("network should not be hit when skipping")

    import requests
    monkeypatch.setattr(requests, "get", boom)

    saved = rp.run_pipeline_3()

    assert saved == 0
    assert existing.read_text(encoding="utf-8").startswith("# 大连理工大学校园动态")


def test_run_pipeline_3_url_dedup_across_sections(
    monkeypatch, tmp_path, fake_requests, fake_call_ai
):
    """Same URL appearing in two sections should only appear once in prompt."""
    import json
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    from datetime import datetime

    now = datetime.now()
    fresh_day = now.strftime("%d")
    fresh_ym = now.strftime("%Y-%m")
    html = (
        read_fixture("dlut_news_snippet.html")
        .replace("{FRESH_DAY}", fresh_day)
        .replace("{FRESH_YM}", fresh_ym)
        .replace("{OLD_DAY}", "01")
        .replace("{OLD_YM}", "2020-01")
    )

    sources_json = tmp_path / "sources.json"
    sources_json.write_text(json.dumps({
        "defaults": {"lookback_hours": 48, "model": "stub/model"},
        "prompt_templates": {"university_news_unified": "Unified: {items}"},
        "sources": [
            {
                "name": "src_a", "display_name": "Section A", "category": "resource",
                "enabled": True, "news_group": "dlut_news", "section": "综合新闻",
                "url": "https://dlut.test/a", "base_url": "https://dlut.test/",
                "selector": "li.bg-mask",
                "fields": {"title": "h4 a", "url": "h4 a[href]",
                           "date_day": "time > span", "date_ym": "time"},
                "date_format": "dlut_news", "max_items": 10, "type": "scrape",
            },
            {
                "name": "src_b", "display_name": "Section B", "category": "resource",
                "enabled": True, "news_group": "dlut_news", "section": "学术科研",
                "url": "https://dlut.test/b", "base_url": "https://dlut.test/",
                "selector": "li.bg-mask",
                "fields": {"title": "h4 a", "url": "h4 a[href]",
                           "date_day": "time > span", "date_ym": "time"},
                "date_format": "dlut_news", "max_items": 10, "type": "scrape",
            },
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(rp, "SOURCES_JSON", str(sources_json))
    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()

    # Both sections return the same URL → should be deduped to 1 item total
    fake_requests.register("https://dlut.test/a", FakeResponse(200, html))
    fake_requests.register("https://dlut.test/b", FakeResponse(200, html))

    prompts_seen = []
    def capture_ai(prompt, **kwargs):
        prompts_seen.append(prompt)
        return "[AI-SUMMARY]"

    monkeypatch.setattr(rp, "call_ai", capture_ai)

    rp.run_pipeline_3()

    assert prompts_seen, "AI should have been called"
    # The same URL should not appear twice in the prompt
    url_occurrences = prompts_seen[0].count("info/1234.htm")
    assert url_occurrences <= 1, f"duplicate URL in prompt: appeared {url_occurrences} times"


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


def test_run_pipeline_2_skips_when_briefing_already_exists(
    monkeypatch, fake_requests, fake_call_ai
):
    """If today's briefing is already saved, pipeline 2 should not re-fetch or re-call AI."""
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))

    today = datetime.now().strftime("%Y-%m-%d")
    code_dir = BRIEFINGS_DIR / "code"
    code_dir.mkdir(parents=True, exist_ok=True)
    existing = code_dir / f"github_trending_briefing_{today}.md"
    existing.write_text("# GitHub Trending\n\nreal content.\n", encoding="utf-8")

    # Register a response that would fail the test if actually fetched.
    def _boom(*args, **kwargs):
        raise AssertionError("network should not be hit when skipping")

    import requests

    monkeypatch.setattr(requests, "get", _boom)

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    saved = rp.run_pipeline_2()

    assert saved == 0
    # File stays intact (not overwritten).
    assert existing.read_text(encoding="utf-8").startswith("# GitHub Trending")


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
