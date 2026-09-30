"""Write-side helpers for the FreshRSS database.

FreshRSS's data directory is bind-mounted into this repo's data root, so the
same SQLite file the RSS sources read from can also be written directly --
no Docker, no API, no credentials. This module exists so that adding an RSS
source to ``config/sources.json`` is the whole of the setup.
"""

from __future__ import annotations

import sqlite3

UNCATEGORIZED_ID = 1
# Every existing subscription was created with these values; match them so an
# auto-inserted feed refreshes on the same cadence as the rest. TTL is a
# per-feed override of FreshRSS's global refresh interval, in seconds.
_FEED_PRIORITY = 10
_FEED_TTL = 3600


def resolve_category(db: sqlite3.Connection, name: str) -> int | None:
    """Return the FreshRSS category id for *name*, or None when absent."""
    row = db.execute("SELECT id FROM category WHERE name = ?", [name]).fetchone()
    return row[0] if row else None


def _category_id(db: sqlite3.Connection, name: str | None) -> int:
    """Resolve a FreshRSS category name; unknown or absent -> Uncategorized."""
    if not name:
        return UNCATEGORIZED_ID
    resolved = resolve_category(db, name)
    return resolved if resolved is not None else UNCATEGORIZED_ID


def ensure_subscription(
    db: sqlite3.Connection,
    *,
    url: str,
    name: str,
    category: str | None = None,
) -> bool:
    """Subscribe *url* if it is not already present; True when inserted.

    Matching is exact on purpose: a section feed (``...feed?section=x``) must
    not be considered present because its query-stripped base is subscribed,
    or the pipeline would silently summarize the wrong feed. A junk row would
    be permanent -- ``feed.url`` is NOT NULL but has no UNIQUE index, so an
    empty URL inserts cleanly and is never corrected.
    """
    url = (url or "").strip()
    name = (name or "").strip()
    if not url or not name:
        raise ValueError(f"rss source {name or url!r}: url and name are required")

    present = db.execute("SELECT 1 FROM feed WHERE url = ?", [url]).fetchone()
    if present:
        return False
    db.execute(
        "INSERT INTO feed (url, name, category, kind, priority, ttl, lastUpdate, error)"
        " VALUES (?, ?, ?, 0, ?, ?, 0, 0)",
        [url, name, _category_id(db, category), _FEED_PRIORITY, _FEED_TTL],
    )
    db.commit()
    return True
