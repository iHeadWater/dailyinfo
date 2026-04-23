"""Tests for :class:`scripts.datasource.RSSDataSource`."""

from __future__ import annotations

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
