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
    # Each element is now (content, batch_items) tuple
    for content, batch_items in out:
        assert isinstance(content, str)
        assert isinstance(batch_items, list)


def test_generate_regular_briefings_returns_batch_items_for_tracking(monkeypatch):
    """_generate_regular_briefings returns (content, batch_items) so callers
    can commit_seen only for successfully processed items."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "track_test", "display_name": "Track", "category": "papers"},
        {"lookback_hours": 24},
    )
    batch_a = [Item(title=f"Paper {i}", date="2026-04-25") for i in range(2)]
    batch_b = [Item(title=f"Paper {i}", date="2026-04-25") for i in range(2, 4)]

    def fake_call_ai(prompt, model="stub", max_tokens=0, **kwargs):
        return "1. **Paper A**\n   > 摘要。\n\n2. **Paper B**\n   > 摘要。"

    monkeypatch.setattr(rp, "call_ai", fake_call_ai)
    monkeypatch.setattr(rp, "log", lambda *_: None)

    out = rp._generate_regular_briefings(
        ds,
        batch_a + batch_b,
        "请总结 {count} 篇 {display_name}：\n{article_list}\n{date}",
        "stub",
    )

    # All items should be tracked in their respective tuples
    all_items = []
    for content, batch_items in out:
        all_items.extend(batch_items)
    assert len(all_items) == 4


def test_make_placeholder_briefing_format():
    """Placeholder briefing contains titles and links for failed items."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "test_ph", "display_name": "Test Placeholder", "category": "papers"},
        {"lookback_hours": 24},
    )
    items = [
        Item(title="Alpha Paper", date="2026-04-25", url="https://doi.org/10.1/a"),
        Item(title="Beta Paper", date="2026-04-25", url="https://doi.org/10.1/b"),
    ]

    result = rp._make_placeholder_briefing(ds, items)
    assert "Test Placeholder" in result
    assert "⚠️" in result
    assert "Alpha Paper" in result
    assert "Beta Paper" in result
    assert "https://doi.org/10.1/a" in result
    assert "https://doi.org/10.1/b" in result


def test_make_placeholder_briefing_handles_no_url():
    """Items without a URL should not crash the placeholder generator."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "no_url", "display_name": "No URL", "category": "papers"},
        {"lookback_hours": 24},
    )
    items = [Item(title="No Link", date="2026-04-25")]
    result = rp._make_placeholder_briefing(ds, items)
    assert "No Link" in result
    assert "查看原文" not in result


def test_retry_failed_items_succeeds_on_second_try(monkeypatch):
    """Phase 2 retries failed items with smaller batches; items that succeed
    on retry should be returned with their batch_items."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "retry_ok", "display_name": "Retry OK", "category": "papers"},
        {"lookback_hours": 24},
    )
    failed_items = [
        Item(title=f"Failed {i}", date="2026-04-25", url=f"https://doi.org/f{i}")
        for i in range(3)
    ]

    call_count = 0

    def fake_call_ai(prompt, model="stub", max_tokens=0, **kwargs):
        nonlocal call_count
        call_count += 1
        count = prompt.count(". Failed")
        return "\n\n".join(f"{i+1}. **Failed {i}**\n   > 摘要。" for i in range(count))

    monkeypatch.setattr(rp, "call_ai", fake_call_ai)
    monkeypatch.setattr(rp, "log", lambda *_: None)

    results = rp._retry_failed_items(
        ds, failed_items,
        "请总结 {count} 篇 {display_name}：\n{article_list}\n{date}",
        "stub",
    )

    assert len(results) == 1
    content, batch_items = results[0]
    assert len(batch_items) == 3
    assert "Failed 0" in content


def test_retry_failed_items_falls_back_to_placeholder(monkeypatch):
    """Phase 2 items that still fail should get placeholder content,
    and their batch_items should still be returned for commit_seen."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "retry_fail", "display_name": "Retry Fail", "category": "papers"},
        {"lookback_hours": 24},
    )
    failed_items = [
        Item(title=f"Lost {i}", date="2026-04-25", url=f"https://doi.org/l{i}")
        for i in range(2)
    ]

    def fake_call_ai(prompt, model="stub", max_tokens=0, **kwargs):
        raise rp.BriefingGenerationError("always fails")

    monkeypatch.setattr(rp, "call_ai", fake_call_ai)
    logs = []
    monkeypatch.setattr(rp, "log", lambda msg: logs.append(msg))

    results = rp._retry_failed_items(
        ds, failed_items,
        "请总结 {count} 篇 {display_name}：\n{article_list}\n{date}",
        "stub",
    )

    assert len(results) == 1
    content, batch_items = results[0]
    assert "⚠️" in content  # placeholder
    assert "Lost 0" in content
    assert len(batch_items) == 2  # returned for commit_seen


def test_retry_failed_items_returns_empty_for_no_failures():
    """No failed items → empty result, no AI calls."""
    import run_pipelines as rp
    from datasource import RSSDataSource

    ds = RSSDataSource(
        {"name": "empty", "display_name": "Empty", "category": "papers"},
        {"lookback_hours": 24},
    )
    results = rp._retry_failed_items(ds, [], "template", "model")
    assert results == []


def test_merge_briefing_parts_single_part_passthrough():
    """A single part should be returned as-is."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "single", "display_name": "Single Source", "category": "papers"},
        {"lookback_hours": 24},
    )
    content = "## 📚 Single Source 今日简报 (2026-05-14) - 2篇文章\n\n1. **A**\n2. **B**"
    items = [Item(title="A", date="2026-05-14"), Item(title="B", date="2026-05-14")]

    merged, all_items = rp._merge_briefing_parts(ds, [(content, items)])
    assert merged == content
    assert all_items == items


def test_merge_briefing_parts_empty_returns_empty():
    """No parts → empty string and empty list."""
    import run_pipelines as rp
    from datasource import RSSDataSource

    ds = RSSDataSource(
        {"name": "empty", "display_name": "Empty", "category": "papers"},
        {"lookback_hours": 24},
    )
    merged, all_items = rp._merge_briefing_parts(ds, [])
    assert merged == ""
    assert all_items == []


def test_merge_briefing_parts_merges_multiple_parts():
    """Multiple parts should be merged with one header, sequential numbering,
    and collected highlights."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "multi", "display_name": "Multi Source", "category": "papers"},
        {"lookback_hours": 24},
    )

    part1_content = (
        "## 📚 Multi Source 今日简报 (2026-05-14) - 2篇文章\n\n"
        "1. **Alpha**\n   > 摘要A\n\n"
        "2. **Beta**\n   > 摘要B\n\n"
        "🔭 **Today's Highlight**\n亮点1"
    )
    part2_content = (
        "## 📚 Multi Source 今日简报 (2026-05-14) - 2篇文章\n\n"
        "1. **Gamma**\n   > 摘要C\n\n"
        "2. **Delta**\n   > 摘要D\n\n"
        "🔭 **Today's Highlight**\n亮点2"
    )
    items1 = [Item(title="Alpha", date="2026-05-14"), Item(title="Beta", date="2026-05-14")]
    items2 = [Item(title="Gamma", date="2026-05-14"), Item(title="Delta", date="2026-05-14")]

    merged, all_items = rp._merge_briefing_parts(
        ds, [(part1_content, items1), (part2_content, items2)]
    )

    # Should have one header with total count
    assert "4篇文章" in merged
    assert merged.count("## 📚") == 1  # only one header

    # Articles should be renumbered 1-4
    assert "1. **Alpha**" in merged
    assert "2. **Beta**" in merged
    assert "3. **Gamma**" in merged
    assert "4. **Delta**" in merged

    # Should NOT have duplicate "1." entries
    assert merged.count("1. **") == 1

    # Highlights should be collected
    assert "亮点1" in merged
    assert "亮点2" in merged

    # All items returned
    assert len(all_items) == 4


def test_merge_briefing_parts_without_highlights():
    """Parts without highlights (e.g. placeholder content) should still merge."""
    import run_pipelines as rp
    from datasource import Item, RSSDataSource

    ds = RSSDataSource(
        {"name": "no_hl", "display_name": "No Highlight", "category": "papers"},
        {"lookback_hours": 24},
    )

    part1 = (
        "## 📚 No Highlight 今日简报 (2026-05-14) - 1篇文章\n\n"
        "1. **Paper A**\n   > 摘要"
    )
    part2 = (
        "## 📚 No Highlight 今日简报 (2026-05-14) - 1篇文章\n\n"
        "1. **Paper B**\n   > 摘要"
    )
    items1 = [Item(title="Paper A", date="2026-05-14")]
    items2 = [Item(title="Paper B", date="2026-05-14")]

    merged, all_items = rp._merge_briefing_parts(
        ds, [(part1, items1), (part2, items2)]
    )

    assert "2篇文章" in merged
    assert "1. **Paper A**" in merged
    assert "2. **Paper B**" in merged
    assert len(all_items) == 2


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


def test_pipeline_resource_unified_news_saves_single_file(
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

    saved = rp.run_pipeline_resource()

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


def test_pipeline_resource_unified_news_idempotent(
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

    saved = rp.run_pipeline_resource()

    assert saved == 0
    assert existing.read_text(encoding="utf-8").startswith("# 大连理工大学校园动态")


def test_pipeline_resource_url_dedup_across_sections(
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

    rp.run_pipeline_resource()

    assert prompts_seen, "AI should have been called"
    # The same URL should not appear twice in the prompt
    url_occurrences = prompts_seen[0].count("info/1234.htm")
    assert url_occurrences <= 1, f"duplicate URL in prompt: appeared {url_occurrences} times"


def test_pipeline_code_smoke(monkeypatch, fake_requests, fake_call_ai):
    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))

    fake_requests.register(
        "https://github.com/trending",
        FakeResponse(status=200, text=read_fixture("github_trending.html")),
    )

    saved = rp.run_pipeline_code()

    assert saved == 1
    today = datetime.now().strftime("%Y-%m-%d")
    out_file = BRIEFINGS_DIR / "code" / f"github_trending_briefing_{today}.md"
    assert out_file.exists()
    body = out_file.read_text(encoding="utf-8")
    assert body.startswith("# GitHub Trending")
    assert "[AI-SUMMARY]" in body


def test_pipeline_code_skips_when_briefing_already_exists(
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
    saved = rp.run_pipeline_code()

    assert saved == 0
    # File stays intact (not overwritten).
    assert existing.read_text(encoding="utf-8").startswith("# GitHub Trending")


def test_pipeline_code_skips_when_fetch_fails(
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

    saved = rp.run_pipeline_code()
    assert saved == 0
    today = datetime.now().strftime("%Y-%m-%d")
    assert not (
        BRIEFINGS_DIR / "code" / f"github_trending_briefing_{today}.md"
    ).exists()


# -----------------------------------------------------------------
# _filter_sources helper
# -----------------------------------------------------------------


def test_filter_sources_by_category_and_type():
    """_filter_sources returns enabled sources matching category and types."""
    import run_pipelines as rp

    cfg = {"sources": [
        {"name": "nature", "type": "rss", "category": "papers", "enabled": True},
        {"name": "science", "type": "rss", "category": "papers", "enabled": True},
        {"name": "arxiv_cs_ai", "type": "rss", "category": "arxiv", "enabled": True},
        {"name": "smolai_news", "type": "rss", "category": "ai_news", "enabled": True},
        {"name": "skxjz", "type": "scrape", "category": "papers", "enabled": True},
        {"name": "disabled_source", "type": "rss", "category": "papers", "enabled": False},
    ]}

    # Filter papers RSS
    result = rp._filter_sources(cfg, "papers", "rss")
    names = [s["name"] for s in result]
    assert "nature" in names
    assert "science" in names
    assert "arxiv_cs_ai" not in names
    assert "skxjz" not in names  # scrape, not rss
    assert "disabled_source" not in names

    # Filter papers scrape+api
    result = rp._filter_sources(cfg, "papers", "scrape", "api")
    assert [s["name"] for s in result] == ["skxjz"]

    # Filter arxiv RSS
    result = rp._filter_sources(cfg, "arxiv", "rss")
    assert [s["name"] for s in result] == ["arxiv_cs_ai"]

    # Filter empty category
    result = rp._filter_sources(cfg, "code", "rss")
    assert result == []
