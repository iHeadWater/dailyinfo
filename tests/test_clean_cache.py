"""Tests for FreshRSS SimplePie cache cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

from clean_cache import clean_stale_cache, find_stale_cache_files


class TestFindStaleCacheFiles:
    """Tests for :func:`find_stale_cache_files`."""

    def test_returns_empty_list_for_empty_directory(self, tmp_path):
        """An empty cache directory yields no stale files."""
        result = find_stale_cache_files(tmp_path, max_age_hours=24)
        assert result == []

    def test_returns_empty_list_when_all_files_are_fresh(self, tmp_path):
        """Files modified recently are not considered stale."""
        now = time.time()
        spc = tmp_path / "feed1.spc"
        spc.write_text("cache data")
        os.utime(spc, (now, now))

        html = tmp_path / "feed1.html"
        html.write_text("<rss>...</rss>")
        os.utime(html, (now, now))

        result = find_stale_cache_files(tmp_path, max_age_hours=24)
        assert result == []

    def test_returns_stale_files_only(self, tmp_path):
        """Only files older than max_age_hours are returned."""
        now = time.time()
        one_hour_ago = now - 3600
        two_days_ago = now - 48 * 3600

        # Fresh file (1 hour old)
        fresh_spc = tmp_path / "fresh.spc"
        fresh_spc.write_text("cache data")
        os.utime(fresh_spc, (one_hour_ago, one_hour_ago))

        # Stale files (2 days old)
        stale_spc = tmp_path / "stale.spc"
        stale_spc.write_text("cache data")
        os.utime(stale_spc, (two_days_ago, two_days_ago))

        stale_html = tmp_path / "stale.html"
        stale_html.write_text("<rss>...</rss>")
        os.utime(stale_html, (two_days_ago, two_days_ago))

        result = find_stale_cache_files(tmp_path, max_age_hours=24)

        stale_paths = {p.name for p in result}
        assert stale_paths == {"stale.spc", "stale.html"}
        assert "fresh.spc" not in stale_paths

    def test_ignores_non_cache_files(self, tmp_path):
        """Files that are not .spc or .html are ignored."""
        two_days_ago = time.time() - 48 * 3600

        txt = tmp_path / "readme.txt"
        txt.write_text("not a cache file")
        os.utime(txt, (two_days_ago, two_days_ago))

        no_ext = tmp_path / "cachefile"
        no_ext.write_text("no extension")
        os.utime(no_ext, (two_days_ago, two_days_ago))

        result = find_stale_cache_files(tmp_path, max_age_hours=24)
        assert result == []

    def test_respects_custom_threshold(self, tmp_path):
        """max_age_hours parameter controls the staleness boundary."""
        now = time.time()
        three_hours_ago = now - 3 * 3600
        six_hours_ago = now - 6 * 3600

        spc_3h = tmp_path / "feed_3h.spc"
        spc_3h.write_text("data")
        os.utime(spc_3h, (three_hours_ago, three_hours_ago))

        spc_6h = tmp_path / "feed_6h.spc"
        spc_6h.write_text("data")
        os.utime(spc_6h, (six_hours_ago, six_hours_ago))

        # With threshold of 4 hours, only the 6h-old file is stale
        result = find_stale_cache_files(tmp_path, max_age_hours=4)
        stale_names = {p.name for p in result}
        assert stale_names == {"feed_6h.spc"}

        # With threshold of 2 hours, both are stale
        result = find_stale_cache_files(tmp_path, max_age_hours=2)
        stale_names = {p.name for p in result}
        assert stale_names == {"feed_3h.spc", "feed_6h.spc"}


class TestCleanStaleCache:
    """Tests for :func:`clean_stale_cache`."""

    def test_deletes_stale_files_and_keeps_fresh(self, tmp_path):
        """Stale files are deleted; fresh files are preserved."""
        now = time.time()
        two_days_ago = now - 48 * 3600

        # Stale — should be deleted
        stale_spc = tmp_path / "old.spc"
        stale_spc.write_text("old")
        os.utime(stale_spc, (two_days_ago, two_days_ago))

        stale_html = tmp_path / "old.html"
        stale_html.write_text("<old/>")
        os.utime(stale_html, (two_days_ago, two_days_ago))

        # Fresh — should be kept
        fresh_spc = tmp_path / "new.spc"
        fresh_spc.write_text("new")
        os.utime(fresh_spc, (now, now))

        deleted, errors = clean_stale_cache(tmp_path, max_age_hours=24)

        assert deleted == 2
        assert errors == 0
        assert not stale_spc.exists()
        assert not stale_html.exists()
        assert fresh_spc.exists()

    def test_dry_run_does_not_delete(self, tmp_path):
        """dry_run=True reports what would be deleted but keeps files."""
        two_days_ago = time.time() - 48 * 3600

        spc = tmp_path / "dry.spc"
        spc.write_text("data")
        os.utime(spc, (two_days_ago, two_days_ago))

        deleted, errors = clean_stale_cache(tmp_path, max_age_hours=24, dry_run=True)
        assert deleted == 1
        assert errors == 0
        assert spc.exists(), "dry_run should not delete files"

    def test_handles_permission_error_gracefully(self, tmp_path, monkeypatch):
        """A PermissionError on unlink is counted as an error, not a crash."""
        two_days_ago = time.time() - 48 * 3600

        spc = tmp_path / "locked.spc"
        spc.write_text("data")
        os.utime(spc, (two_days_ago, two_days_ago))

        def _fake_unlink(p):
            raise PermissionError(f"fake permission error: {p}")

        monkeypatch.setattr(Path, "unlink", _fake_unlink)

        deleted, errors = clean_stale_cache(tmp_path, max_age_hours=24)
        assert deleted == 0
        assert errors == 1
        assert spc.exists()
