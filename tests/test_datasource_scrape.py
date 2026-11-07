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
    assert "source_published_at" not in first.extra

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
    import datasource
    from datasource import DataSource

    # The datasource judges recency in its own timezone; a host-local clock
    # disagrees with it for the eight hours their dates differ.
    now = datasource.NOW
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
    assert it.extra["source_published_at"] == it.date


def test_dlut_news_respects_max_items(fake_requests):
    """``max_items`` caps the total parsed items."""
    from conftest import FakeResponse
    import datasource
    from datasource import DataSource

    now = datasource.NOW
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


def test_chinawater_filters_seen_articles(fake_requests):
    from conftest import FakeResponse
    from datasource import DataSource

    today = datetime.now().strftime("%Y-%m-%d")
    index_html = (
        '<a href="guokan_list?year=2026&issue=13&yearId=year-uuid'
        '&issueId=issue-uuid">2026年13期</a>'
    )
    list_html = """
    <a href="/portal/journal/portal/client/paper/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">水利调度研究</a>
    <a href="/portal/journal/portal/client/paper/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb">智慧水网建设</a>
    """
    detail_html = f"<div>出版时间：{today}</div>"

    fake_requests.register(
        "https://slzg.cbpt.cnki.net/portal/journal/portal/client/index",
        FakeResponse(status=200, text=index_html),
    )
    fake_requests.register(
        "https://slzg.cbpt.cnki.net/portal/journal/portal/client/guokan_list",
        FakeResponse(status=200, text=list_html),
    )
    fake_requests.register(
        "https://slzg.cbpt.cnki.net/portal/journal/portal/client/paper",
        FakeResponse(status=200, text=detail_html),
    )

    cfg = {
        "name": "chinawater",
        "display_name": "中国水利",
        "category": "papers",
        "type": "scrape",
        "url": "https://slzg.cbpt.cnki.net/portal/journal/portal/client/index",
        "lookback_hours": 48,
        "max_items": 20,
    }

    first = DataSource.create(cfg, DEFAULTS)
    first_items = first.fetch()
    assert [item.title for item in first_items] == ["水利调度研究", "智慧水网建设"]
    first.commit_seen(first_items)

    second = DataSource.create(cfg, DEFAULTS)
    assert second.fetch() == []


def test_dlut_gs_parses_notices_and_applies_keyword_filter(fake_requests):
    """研究生院通知：解析标题/日期，并按 include/exclude 过滤。"""
    from conftest import FakeResponse
    import datasource
    from datasource import DataSource

    today = datasource.NOW.strftime("%Y-%m-%d")
    html = f"""
    <UL>
      <LI><A href="../../info/1089/52172.htm" target="_blank">关于申请2027年联合培养博士双学位项目的通知</A><SPAN class="date">[{today}]</SPAN>
      <DIV class="cb"></DIV></LI>
      <LI><A href="../../info/1089/52162.htm" target="_blank">学校总值班室值班安排</A><SPAN class="date">[{today}]</SPAN>
      <DIV class="cb"></DIV></LI>
    </UL>
    """
    fake_requests.register(
        "http://gs.dlut.test/zytz.htm",
        FakeResponse(status=200, text=html),
    )

    ds = DataSource.create(
        {
            "name": "dlut_gs",
            "display_name": "研究生院通知",
            "category": "resource",
            "type": "scrape",
            "url": "http://gs.dlut.test/zytz.htm",
            "base_url": "http://gs.dlut.test/",
            "date_format": "standard",
            "max_items": 20,
            "include_keywords": ["申请"],
            "exclude_keywords": ["值班"],
        },
        DEFAULTS,
    )

    items = ds.fetch()
    assert [item.title for item in items] == [
        "关于申请2027年联合培养博士双学位项目的通知"
    ]
    assert items[0].url == "http://gs.dlut.test/info/1089/52172.htm"
    assert items[0].date == today
    assert [item.title for item in ds.filtered_out] == ["学校总值班室值班安排"]
    assert ds._total_before_filter == 2


def test_dlut_teach_uses_full_title_attribute(fake_requests):
    """教务处列表的可见文本被截断，完整标题在 title 属性里。"""
    from conftest import FakeResponse
    import datasource
    from datasource import DataSource

    today = datasource.NOW.strftime("%Y-%m-%d")
    full_title = "关于2026年度“人工智能+”虚拟仿真实验教学项目校内推荐结果的公示"
    html = f"""
    <UL>
      <li id="line_u4_0" style="">
        <a href="ny.jsp?urltype=news.NewsContentUrl&wbtreeid=1016&wbnewsid=18298"
           target=_blank title="{full_title}" style="">关于2026年度“人工智能+”虚拟仿真实验教学...</a>
        <SPAN>{today}</SPAN>
      </li>
    </UL>
    """
    fake_requests.register(
        "https://teach.dlut.test/list.jsp",
        FakeResponse(status=200, text=html),
    )

    ds = DataSource.create(
        {
            "name": "dlut_teach",
            "display_name": "教务处通知",
            "category": "resource",
            "type": "scrape",
            "url": "https://teach.dlut.test/list.jsp",
            "base_url": "https://teach.dlut.test/",
            "date_format": "standard",
            "max_items": 20,
        },
        DEFAULTS,
    )

    items = ds.fetch()
    assert [item.title for item in items] == [full_title]
    assert items[0].url.startswith("https://teach.dlut.test/ny.jsp?")
    assert items[0].date == today


def test_keyword_filter_is_noop_without_config(fake_requests):
    """没有配置关键词时，所有条目都保留。"""
    from conftest import FakeResponse
    import datasource
    from datasource import DataSource

    html = (
        '<li class="bg-mask">'
        f"<time><span>{datasource.NOW.strftime('%d')}</span> "
        f"{datasource.NOW.strftime('%Y-%m')}</time>"
        '<h4><a href="./info/1.htm">Any title</a></h4>'
        "</li>"
    )
    fake_requests.register(
        "https://dlut.example.edu/no-filter",
        FakeResponse(status=200, text=html),
    )

    ds = DataSource.create(
        {
            "name": "dlut_news",
            "category": "resource",
            "type": "scrape",
            "url": "https://dlut.example.edu/no-filter",
            "base_url": "https://dlut.example.edu/",
            "date_format": "dlut_news",
            "max_items": 10,
        },
        DEFAULTS,
    )

    assert len(ds.fetch()) == 1
    assert ds.filtered_out == []
