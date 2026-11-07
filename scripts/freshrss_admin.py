"""Write-side helpers for the FreshRSS database.

FreshRSS's data directory is bind-mounted into this repo's data root, so the
same SQLite file the RSS sources read from can also be written directly --
no Docker, no API, no credentials. This module exists so that adding an RSS
source to ``config/sources.json`` is the whole of the setup.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urldefrag, urljoin, urlsplit

import requests

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


def resolve_existing_subscription_url(
    db: sqlite3.Connection, *, url: str, name: str
) -> str:
    """Reuse a known feed only after a verified permanent redirect.

    Names identify candidates, not equivalence. Query parameters must survive
    unchanged, so one section never substitutes for another or a bare feed.
    Exact subscriptions and entirely new sources require no network probe.
    """
    if db.execute("SELECT 1 FROM feed WHERE url = ?", [url]).fetchone():
        return url
    candidates = {
        row[0] for row in db.execute("SELECT url FROM feed WHERE name = ?", [name])
    }
    if not candidates or urlsplit(url).scheme not in {"http", "https"}:
        return url
    current = url
    visited = {url}
    for _ in range(3):
        try:
            response = requests.get(
                current, stream=True, allow_redirects=False, timeout=(3, 5)
            )
            try:
                if response.status_code not in {301, 308}:
                    return url
                location = response.headers.get("Location")
                if not location:
                    return url
                target = urldefrag(urljoin(current, location))[0]
            finally:
                response.close()
        except requests.RequestException:
            return url
        parsed = urlsplit(target)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or (urlsplit(current).scheme == "https" and parsed.scheme != "https")
            or parsed.query != urlsplit(url).query
            or target in visited
        ):
            return url
        if target in candidates:
            return target
        visited.add(target)
        current = target
    return url


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
