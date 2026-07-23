"""FreshRSS SimplePie cache cleanup utilities.

SimplePie (FreshRSS's feed parser) caches HTTP responses as ``.spc``
(metadata) and ``.html`` (body) files.  Occasionally a cache entry gets
stuck — the next refresh always says "uses cache" instead of issuing a
real ``GET`` / ``304``.  When that happens the feed silently stops
receiving new articles even though ``lastUpdate`` keeps ticking and
``error`` stays 0.

This module provides safe stale-cache detection and cleanup so that an
external scheduler (cron / launchd / hermes) can periodically clear old
cache files.  The default threshold of 24 hours is far longer than
FreshRSS's normal refresh cycle (``CRON_MIN: */15`` → every 15 min), so
healthy caches are never touched.
"""

from __future__ import annotations

import time
from pathlib import Path

CACHE_EXTENSIONS = {".spc", ".html"}


def find_stale_cache_files(cache_dir: Path, max_age_hours: int = 24) -> list[Path]:
    """Return ``.spc`` and ``.html`` files in *cache_dir* older than
    *max_age_hours*.

    Args:
        cache_dir: Path to the FreshRSS ``cache/`` directory.
        max_age_hours: Files whose *modification* time is older than this
            many hours are considered stale.

    Returns:
        Sorted list of absolute paths to stale cache files.  May be empty.
    """
    if not cache_dir.is_dir():
        return []

    cutoff = time.time() - max_age_hours * 3600
    stale: list[Path] = []

    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix not in CACHE_EXTENSIONS:
            continue
        try:
            mtime = entry.stat().st_mtime
            if mtime < cutoff:
                stale.append(entry)
        except OSError:
            # File was deleted / inaccessible between iterdir and stat —
            # skip it.
            continue

    stale.sort()
    return stale


def clean_stale_cache(
    cache_dir: Path, max_age_hours: int = 24, dry_run: bool = False
) -> tuple[int, int]:
    """Delete stale SimplePie cache files.

    Args:
        cache_dir: Path to the FreshRSS ``cache/`` directory.
        max_age_hours: Age threshold in hours (see :func:`find_stale_cache_files`).
        dry_run: If ``True``, only report what *would* be deleted.

    Returns:
        ``(deleted, errors)`` — count of successfully deleted files and
        count of files that could not be deleted.
    """
    stale_files = find_stale_cache_files(cache_dir, max_age_hours=max_age_hours)
    if not stale_files:
        return 0, 0

    if dry_run:
        return len(stale_files), 0

    deleted = 0
    errors = 0
    for path in stale_files:
        try:
            path.unlink()
            deleted += 1
        except OSError:
            errors += 1
    return deleted, errors
