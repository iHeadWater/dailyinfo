"""Tests for :mod:`scripts.freshrss_admin`."""

from __future__ import annotations

import sqlite3

import pytest


def _fresh_db(path: str = ":memory:") -> sqlite3.Connection:
    """FreshRSS-shaped DB: the columns ensure_subscription touches."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE category (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE feed ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " url TEXT NOT NULL,"
        " kind INTEGER NOT NULL DEFAULT 0,"
        " category INTEGER NOT NULL DEFAULT 0,"
        " name TEXT NOT NULL,"
        " website TEXT,"
        " description TEXT,"
        " lastUpdate INTEGER NOT NULL DEFAULT 0,"
        " priority INTEGER NOT NULL DEFAULT 10,"
        " pathEntries TEXT,"
        " httpAuth TEXT,"
        " error BOOLEAN NOT NULL DEFAULT 0,"
        " ttl INTEGER NOT NULL DEFAULT 0,"
        " attributes TEXT,"
        " cache_nbEntries INT NOT NULL DEFAULT 0,"
        " cache_nbUnreads INT NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE entry ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, id_feed INTEGER, title TEXT,"
        " link TEXT, content TEXT, date INTEGER, lastSeen INTEGER)"
    )
    conn.execute("INSERT INTO category(id, name) VALUES (1, 'Uncategorized')")
    conn.execute("INSERT INTO category(id, name) VALUES (2, 'AI News')")
    conn.commit()
    return conn


def test_ensure_subscription_inserts_missing_feed():
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    added = ensure_subscription(db, url="https://example.com/feed.xml", name="Example")

    assert added is True
    # A rollback is a no-op after a commit; it discards the row if the commit
    # is ever removed -- which is how production would silently lose it, since
    # the pipeline closes the connection in a finally.
    db.rollback()
    row = db.execute(
        "SELECT url, name, category, kind, priority, ttl, lastUpdate, error FROM feed"
    ).fetchone()
    assert dict(row) == {
        "url": "https://example.com/feed.xml",
        "name": "Example",
        "category": 1,
        "kind": 0,
        "priority": 10,
        "ttl": 3600,
        "lastUpdate": 0,
        "error": 0,
    }


def test_ensure_subscription_is_a_noop_on_exact_url():
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    db.execute(
        "INSERT INTO feed (url, name) VALUES (?, ?)",
        ["https://example.com/feed.xml", "Existing"],
    )

    added = ensure_subscription(db, url="https://example.com/feed.xml", name="Example")

    assert added is False
    assert db.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 1


def test_ensure_subscription_sequence_is_idempotent():
    """The production sequence: first day inserts, every later day is a no-op."""
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    assert ensure_subscription(db, url="https://example.com/f.xml", name="F") is True
    assert ensure_subscription(db, url="https://example.com/f.xml", name="F") is False
    assert db.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 1


def test_ensure_subscription_ignores_query_stripped_variants():
    """A bare feed URL must not satisfy a section feed -- exact match only.

    Tolerant matching would conclude the section feed is subscribed when only
    its base URL is, and the pipeline would silently summarize the wrong feed.
    """
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    db.execute(
        "INSERT INTO feed (url, name) VALUES (?, ?)",
        ["https://www.latent.space/feed", "Latent Space (bare)"],
    )

    added = ensure_subscription(
        db,
        url="https://www.latent.space/feed?section=ainews",
        name="Latent Space AINews",
    )

    assert added is True
    assert db.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 2


def test_ensure_subscription_rejects_empty_url_or_name():
    """A junk row would be permanent (no UNIQUE index); fail instead."""
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    for url, name in [
        ("", "x"),
        ("   ", "x"),
        (None, "x"),
        ("https://example.com/f.xml", ""),
        ("https://example.com/f.xml", "   "),
        (None, None),
    ]:
        with pytest.raises(ValueError, match="url and name"):
            ensure_subscription(db, url=url, name=name)

    assert db.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 0


def test_ensure_subscription_resolves_category_by_name():
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    ensure_subscription(
        db, url="https://example.com/feed.xml", name="Example", category="AI News"
    )

    assert db.execute("SELECT category FROM feed").fetchone()["category"] == 2


def test_ensure_subscription_falls_back_to_uncategorized():
    from freshrss_admin import ensure_subscription

    db = _fresh_db()
    ensure_subscription(
        db, url="https://example.com/feed.xml", name="Example", category="No Such"
    )

    assert db.execute("SELECT category FROM feed").fetchone()["category"] == 1


def test_ensure_rss_subscriptions_only_adds_this_categorys_enabled_rss_sources():
    """The run_pipelines wiring subscribes exactly the sources _filter_sources
    would process -- enabled, that category, type rss."""
    import run_pipelines as rp

    db = _fresh_db()
    cfg = {
        "sources": [
            {
                "name": "latent_space",
                "display_name": "Latent Space AINews",
                "type": "rss",
                "category": "ai_news",
                "enabled": True,
                "url": "https://www.latent.space/feed?section=ainews",
                "freshrss_category": "AI News",
            },
            {
                "name": "disabled_one",
                "type": "rss",
                "category": "ai_news",
                "enabled": False,
                "url": "https://example.com/off.xml",
            },
            {
                "name": "papers_one",
                "type": "rss",
                "category": "papers",
                "enabled": True,
                "url": "https://example.com/papers.xml",
            },
            {
                "name": "scrape_one",
                "type": "scrape",
                "category": "ai_news",
                "enabled": True,
                "url": "https://example.com/scrape",
            },
        ]
    }

    added = rp._ensure_rss_subscriptions(cfg, db, "ai_news")

    assert added == ["latent_space"]
    rows = db.execute("SELECT name, category FROM feed").fetchall()
    assert [dict(r) for r in rows] == [{"name": "Latent Space AINews", "category": 2}]


def test_unknown_freshrss_category_warns_and_falls_back(monkeypatch):
    """A typo must not vanish into Uncategorized without a log line."""
    import run_pipelines as rp

    db = _fresh_db()
    msgs: list[str] = []
    monkeypatch.setattr(rp, "log", msgs.append)
    cfg = {
        "sources": [
            {
                "name": "typo_source",
                "type": "rss",
                "category": "ai_news",
                "enabled": True,
                "url": "https://example.com/f.xml",
                "freshrss_category": "AI New",
            }
        ]
    }

    added = rp._ensure_rss_subscriptions(cfg, db, "ai_news")

    assert added == ["typo_source"]
    assert any("AI New" in m and "WARN" in m for m in msgs)
    assert db.execute("SELECT category FROM feed").fetchone()["category"] == 1


def test_subscription_sync_survives_a_locked_db(tmp_path, monkeypatch):
    """A locked DB must degrade to a warning, not kill the whole category run."""
    import run_pipelines as rp

    db_path = tmp_path / "locked.sqlite"
    db = _fresh_db(str(db_path))
    db.execute("PRAGMA busy_timeout = 50")
    blocker = sqlite3.connect(str(db_path))
    blocker.execute("BEGIN EXCLUSIVE")
    msgs: list[str] = []
    monkeypatch.setattr(rp, "log", msgs.append)
    cfg = {
        "sources": [
            {
                "name": "later",
                "type": "rss",
                "category": "ai_news",
                "enabled": True,
                "url": "https://example.com/f.xml",
            }
        ]
    }

    try:
        added = rp._ensure_rss_subscriptions(cfg, db, "ai_news")
    finally:
        blocker.rollback()
        blocker.close()

    assert added == []
    assert any("WARN" in m for m in msgs)


def test_similar_url_already_subscribed_warns_but_still_inserts(monkeypatch):
    """A near-miss stored URL must not lead to a silent second subscription."""
    import run_pipelines as rp

    db = _fresh_db()
    db.execute(
        "INSERT INTO feed (url, name) VALUES (?, ?)",
        ["https://example.com/feed", "Stored variant"],
    )
    msgs: list[str] = []
    monkeypatch.setattr(rp, "log", msgs.append)
    cfg = {
        "sources": [
            {
                "name": "src",
                "type": "rss",
                "category": "ai_news",
                "enabled": True,
                "url": "https://example.com/feed?x=1",
            }
        ]
    }

    added = rp._ensure_rss_subscriptions(cfg, db, "ai_news")

    assert added == ["src"]
    # The warning names the stored feed, never its URL (subscription URLs can
    # carry private tokens).
    assert any("similar feed" in m and "Stored variant" in m for m in msgs)
    assert not any("https://example.com" in m for m in msgs)
    assert db.execute("SELECT COUNT(*) FROM feed").fetchone()[0] == 2


def test_a_category_run_subscribes_and_resolves_in_the_same_run(tmp_path, monkeypatch):
    """Driving the real pipeline reaches the subscription loop, and the feed
    it inserted resolves in the same run (subscribe precedes the URL map)."""
    import json
    import time

    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    db_path = tmp_path / "freshrss.sqlite"
    db = _fresh_db(str(db_path))
    now = int(time.time())
    # The feed row does not exist yet; the auto-increment id will be 1.
    db.execute(
        "INSERT INTO entry (id_feed, title, link, content, date, lastSeen)"
        " VALUES (1, 'First post', 'https://www.latent.space/p/1', ?, ?, ?)",
        ["x " * 200, now, now],
    )
    db.commit()  # the pipeline opens its own connection; an open write txn locks it out
    sources_json = tmp_path / "sources.json"
    sources_json.write_text(
        json.dumps(
            {
                "defaults": {},
                "prompt_templates": {"latent_space_ainews": "summary of {content}"},
                "sources": [
                    {
                        "name": "latent_space",
                        "display_name": "Latent Space AINews",
                        "type": "rss",
                        "category": "ai_news",
                        "enabled": True,
                        "prompt_template": "latent_space_ainews",
                        "url": "https://www.latent.space/feed?section=ainews",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(rp, "SOURCES_JSON", str(sources_json))
    monkeypatch.setattr(rp, "FRESHRSS_DB", str(db_path))
    monkeypatch.setattr(rp, "log", lambda *_: None)
    monkeypatch.setattr(rp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        rp, "call_ai", lambda prompt, model="", max_tokens=0, **kw: "stub summary"
    )

    rp.run_pipeline_ai_news()

    row = db.execute("SELECT url, name FROM feed").fetchone()
    assert dict(row) == {
        "url": "https://www.latent.space/feed?section=ainews",
        "name": "Latent Space AINews",
    }
    saved = list((BRIEFINGS_DIR / "ai_news").glob("latent_space_briefing_*.md"))
    assert len(saved) == 1
