"""Tests for FreshRSS SimplePie cache utilities."""

from __future__ import annotations

import json


def test_find_cache_files_for_url_matches_feed_and_pair(tmp_path):
    from freshrss_cache import find_cache_files_for_url

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    spc = cache_dir / "abc.spc"
    html = cache_dir / "abc.html"
    other = cache_dir / "other.spc"

    spc.write_text("https://rss.arxiv.org/rss/cs.AI", encoding="utf-8")
    html.write_text("<rss>cached</rss>", encoding="utf-8")
    other.write_text("https://www.nature.com/nature.rss", encoding="utf-8")

    result = find_cache_files_for_url(cache_dir, "https://rss.arxiv.org/rss/cs.AI")

    assert result == [html, spc]


def test_find_cache_files_for_url_recurses_nested_cache_dirs(tmp_path):
    from freshrss_cache import find_cache_files_for_url

    nested = tmp_path / "cache" / "feeds"
    nested.mkdir(parents=True)
    spc = nested / "nested.spc"
    spc.write_text("serialized rss.arxiv.org/rss/cs.AI cache", encoding="utf-8")

    assert find_cache_files_for_url(tmp_path / "cache", "https://rss.arxiv.org/rss/cs.AI") == [spc]


def test_delete_cache_files_returns_removed_count(tmp_path):
    from freshrss_cache import delete_cache_files

    first = tmp_path / "a.spc"
    second = tmp_path / "a.html"
    first.write_text("x", encoding="utf-8")
    second.write_text("x", encoding="utf-8")

    assert delete_cache_files([first, second]) == 2
    assert not first.exists()
    assert not second.exists()


def test_record_zero_result_counts_consecutive_days(tmp_path):
    from freshrss_cache import record_zero_result

    assert record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-27") == 1
    assert record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-28") == 2
    assert record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-28") == 2

    state = json.loads((tmp_path / "arxiv_cs_ai_zero_state.json").read_text())
    assert state == {
        "last_zero_date": "2026-06-28",
        "consecutive_zero_days": 2,
    }


def test_record_zero_result_resets_after_gap(tmp_path):
    from freshrss_cache import record_zero_result

    assert record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-20") == 1
    assert record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-25") == 1


def test_reset_zero_result_removes_state(tmp_path):
    from freshrss_cache import record_zero_result, reset_zero_result

    record_zero_result(tmp_path, "arxiv_cs_ai", "2026-06-27")
    reset_zero_result(tmp_path, "arxiv_cs_ai")

    assert not (tmp_path / "arxiv_cs_ai_zero_state.json").exists()
