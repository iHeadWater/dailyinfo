"""Tests for :class:`scripts.datasource.RSSDataSource`."""

from __future__ import annotations

import datetime
import time

DEFAULTS = {"lookback_hours": 24}


def _make_rss(config, rss_db):
    from datasource import DataSource, build_feed_url_map

    full_map, base_map = build_feed_url_map(rss_db)
    return DataSource.create(
        config, DEFAULTS, db=rss_db, full_map=full_map, base_map=base_map
    )


def test_fetch_title_only_respects_cutoff(rss_db):
    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )

    items = ds.fetch()
    titles = [it.title for it in items]
    assert "Stale Title" not in titles
    assert all(t.startswith("Fresh Title ") for t in titles)
    assert len(titles) == 5


def test_fetch_respects_max_articles(rss_db):
    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
            "max_articles": 2,
        },
        rss_db,
    )

    items = ds.fetch()
    assert len(items) == 2


def test_fetch_base_url_match_ignores_query(rss_db):
    """Feed 2's URL has a query string — resolver should match the base URL."""
    ds = _make_rss(
        {
            "name": "newsfeed",
            "type": "rss",
            "category": "ai_news",
            "url": "https://news.example.com/rss",
        },
        rss_db,
    )
    items = ds.fetch()
    assert items == []  # no entries for feed 2 but resolver should still find it


def test_fetch_returns_empty_for_unknown_url(rss_db):
    ds = _make_rss(
        {
            "name": "ghost",
            "type": "rss",
            "category": "papers",
            "url": "https://nope.example.com/",
        },
        rss_db,
    )
    assert ds.fetch() == []


def test_fetch_use_content_filters_and_truncates(rss_db):
    ds = _make_rss(
        {
            "name": "deep",
            "type": "rss",
            "category": "ai_news",
            "url": "https://deep.example.com/rss",
            "use_content": True,
        },
        rss_db,
    )

    items = ds.fetch()
    titles = [it.title for it in items]
    assert "Deep Short" not in titles  # filtered: <100 chars
    assert "Deep Normal" in titles
    assert "Deep Long" in titles

    long_item = next(it for it in items if it.title == "Deep Long")
    assert len(long_item.content) <= 12100
    assert long_item.content.endswith("[... content truncated ...]")


def test_get_batches_splits_and_caps(rss_db):
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
            "max_articles_per_batch": 2,
            "max_batches": 3,
        },
        rss_db,
    )

    items = [Item(title=f"t{i}", date="2024-01-01") for i in range(10)]
    batches = ds.get_batches(items)
    assert len(batches) == 3  # capped
    assert [len(b) for b in batches] == [2, 2, 2]


def test_get_batches_without_limit_returns_single_batch(rss_db):
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    items = [Item(title="only", date="2024-01-01")]
    batches = ds.get_batches(items)
    assert batches == [items]


def test_format_items_numbered_title_list(rss_db):
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    items = [
        Item(title="Alpha", date="2024-01-01"),
        Item(title="Beta", date="2024-01-02"),
    ]
    assert ds.format_items(items) == "1. Alpha\n2. Beta"


def test_fetch_no_db_returns_empty():
    from datasource import RSSDataSource

    ds = RSSDataSource(
        {"name": "x", "url": "https://x.test/", "category": "papers"},
        DEFAULTS,
        db=None,
    )
    assert ds.fetch() == []


def test_seen_dedup_filters_already_processed(rss_db):
    """Items whose URLs are already in seen should be filtered out."""
    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    # First fetch returns all fresh items
    items = ds.fetch()
    assert len(items) == 5

    # Mark all as seen
    ds.commit_seen(items)
    assert len(ds._seen) == 5

    # Second fetch should filter all of them out
    items2 = ds.fetch()
    assert items2 == []


def test_use_content_extended_lookback_and_seen_dedup(rss_db):
    """use_content 分支要同时满足两件事:跨天窗口能捞回,且不会重复推送。

    这是低频源采用 72h 窗口的前提 —— 24h 窗口只在条目进入 FreshRSS 的当天
    能捞到它,那天流水线漏跑就永久丢失;而窗口放长之后,同一条必须被 seen
    状态挡住,否则会连续多天重复推送。
    """
    now = int(time.time())
    rss_db.execute(
        "INSERT INTO entry(id_feed, title, link, content, date, lastSeen)"
        " VALUES (?,?,?,?,?,?)",
        (
            2,
            "Two Day Old Issue",
            "https://news.example.com/a/old",
            "<p>an issue published two days ago</p>" + ("word " * 30),
            now - 48 * 3600,
            now - 48 * 3600,
        ),
    )
    rss_db.commit()

    def make(name, lookback_hours):
        return _make_rss(
            {
                "name": name,
                "type": "rss",
                "category": "ai_news",
                "url": "https://news.example.com/rss?format=xml",
                "use_content": True,
                "lookback_hours": lookback_hours,
            },
            rss_db,
        )

    # 48h 前的条目落在 24h 窗口外 —— 这正是原配置的丢数据风险
    assert make("feed2_narrow", 24).fetch() == []

    # 72h 窗口把它捞回来
    ds = make("feed2_wide", 72)
    items = ds.fetch()
    assert [it.title for it in items] == ["Two Day Old Issue"]

    # 窗口跨天后同一条不得二次推送
    ds.commit_seen(items)
    assert ds.fetch() == []


def test_commit_seen_only_records_provided_items(rss_db):
    """commit_seen should only mark the items passed to it, not all fetched items."""
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    items = ds.fetch()
    assert len(items) == 5

    # Only commit first 2 items (simulating partial success)
    ds.commit_seen(items[:2])
    assert len(ds._seen) == 2

    # Re-fetch: 3 uncommitted items should come through
    items2 = ds.fetch()
    assert len(items2) == 3


def test_commit_seen_empty_list_is_harmless(rss_db):
    """commit_seen([]) should not fail and should not affect existing seen state."""
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    items = ds.fetch()
    ds.commit_seen(items[:1])
    assert len(ds._seen) == 1

    ds.commit_seen([])  # no-op
    assert len(ds._seen) == 1


def test_seen_never_expires(rss_db):
    """60-day-old seen records should still participate in dedup and not be cleaned."""
    from datasource import Item

    ds = _make_rss(
        {
            "name": "test_feed1",
            "type": "rss",
            "category": "papers",
            "url": "https://example.com/feed.xml",
        },
        rss_db,
    )
    old_date = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    old_url = "https://old-paper.com/1"

    ds._seen = {old_url: old_date}
    ds._save_seen()

    # Simulate _filter_seen after fetch: old URL should still be blocked
    items = [Item(title="Old Paper", url=old_url, date=datetime.date.today().isoformat())]
    filtered = ds._filter_seen(items)
    assert len(filtered) == 0, "60-day-old URL should still be filtered by dedup"

    # commit_seen should not purge old records
    new_items = [Item(title="New Paper", url="https://new.com/1", date=datetime.date.today().isoformat())]
    ds.commit_seen(new_items)
    assert old_url in ds._seen, "commit_seen should not purge old records"
    assert len(ds._seen) == 2  # old + new both present
