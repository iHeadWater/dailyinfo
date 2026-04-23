"""Tests for :class:`scripts.datasource.ScrapeDataSource`."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import read_fixture

DEFAULTS = {"lookback_hours": 24}


def test_github_trending_parses_items(fake_requests):
    from conftest import FakeResponse
    from datasource import DataSource

    fake_requests.register(
        "https://github.com/trending",
        FakeResponse(status=200, text=read_fixture("github_trending.html")),
    )

    ds = DataSource.create(
        {
            "name": "github_trending",
            "display_name": "GitHub Trending",
            "category": "code",
            "type": "scrape",
            "url": "https://github.com/trending?since=daily",
        },
        DEFAULTS,
    )

    items = ds.fetch()
    assert len(items) == 2

    first, second = items
    assert first.title == "A curated list of awesome things for testing."
    assert first.url == "https://github.com/alice/awesome-repo"
    assert first.extra["full_name"] == "alice/awesome-repo"
    assert first.extra["language"] == "Python"
    assert first.extra["stars"] == "1234"
    assert first.extra["stars_today"] == "200"

    assert second.extra["full_name"] == "bob/rust-lib"
    assert second.extra["language"] == "Rust"
    assert second.extra["stars"] == "5678"
    assert second.extra["stars_today"] == "42"


def test_github_trending_format_items_renders_expected_line():
    from datasource import DataSource, Item

    ds = DataSource.create(
        {
            "name": "github_trending",
            "display_name": "GitHub Trending",
            "category": "code",
            "type": "scrape",
            "url": "https://github.com/trending?since=daily",
        },
        DEFAULTS,
    )

    item = Item(
        title="desc",
        date="2024-01-01",
        url="https://github.com/owner/repo",
        extra={
            "full_name": "owner/repo",
            "name": "repo",
            "language": "Go",
            "stars": "100",
            "stars_today": "5",
        },
    )
    line = ds.format_items([item])
    assert "**owner/repo**" in line
    assert "[Go]" in line
    assert "total 100" in line
    assert "+5 today" in line
    assert "https://github.com/owner/repo" in line


def test_dlut_news_parsing_filters_old_entries(fake_requests):
    from conftest import FakeResponse
    from datasource import DataSource

    now = datetime.now()
    old = now - timedelta(days=60)
    html = (
        read_fixture("dlut_news_snippet.html")
        .replace("{FRESH_DAY}", now.strftime("%d"))
        .replace("{FRESH_YM}", now.strftime("%Y-%m"))
        .replace("{OLD_DAY}", old.strftime("%d"))
        .replace("{OLD_YM}", old.strftime("%Y-%m"))
    )

    fake_requests.register(
        "https://dlut.example.edu/news",
        FakeResponse(status=200, text=html),
    )

    ds = DataSource.create(
        {
            "name": "dlut_news",
            "display_name": "DLUT News",
            "category": "resource",
            "type": "scrape",
            "url": "https://dlut.example.edu/news",
            "base_url": "https://dlut.example.edu/",
            "date_format": "dlut_news",
            "max_items": 10,
        },
        DEFAULTS,
    )

    items = ds.fetch()
    assert len(items) == 1, [it.title for it in items]
    it = items[0]
    assert it.title == "Fresh DLUT announcement title"
    assert it.url == "https://dlut.example.edu/info/1234.htm"
    assert it.date == now.strftime("%Y-%m-%d")


def test_dlut_news_respects_max_items(fake_requests):
    """``max_items`` caps the total parsed items."""
    from conftest import FakeResponse
    from datasource import DataSource

    now = datetime.now()
    day = now.strftime("%d")
    ym = now.strftime("%Y-%m")

    block = (
        '<li class="bg-mask">'
        f"<time><span>{day}</span> {ym}</time>"
        '<h4><a href="./info/{i}.htm">Title {i}</a></h4>'
        "</li>"
    )
    html = "<ul>" + "".join(block.format(i=i) for i in range(5)) + "</ul>"

    fake_requests.register(
        "https://dlut.example.edu/",
        FakeResponse(status=200, text=html),
    )

    ds = DataSource.create(
        {
            "name": "dlut_news",
            "category": "resource",
            "type": "scrape",
            "url": "https://dlut.example.edu/",
            "base_url": "https://dlut.example.edu/",
            "date_format": "dlut_news",
            "max_items": 2,
        },
        DEFAULTS,
    )

    assert len(ds.fetch()) == 2
