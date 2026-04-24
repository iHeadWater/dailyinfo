"""Tests for pure helpers in ``scripts/datasource.py``."""

from __future__ import annotations

import sqlite3

import pytest


def test_strip_html_removes_script_and_style():
    from datasource import strip_html

    text = (
        "<p>Hello</p><script>alert('x')</script>"
        "<style>body{color:red}</style>"
        "<p>World &amp; friends</p>"
    )
    out = strip_html(text)
    assert "alert" not in out
    assert "color:red" not in out
    assert "Hello" in out
    assert "World & friends" in out


def test_strip_html_collapses_whitespace_and_br():
    from datasource import strip_html

    out = strip_html("foo<br/>bar<br/>baz\n\n\n\nqux")
    assert "foo" in out and "bar" in out and "qux" in out
    assert "\n\n\n" not in out


def test_strip_html_preserves_paragraph_breaks():
    from datasource import strip_html

    out = strip_html("<p>line one</p><p>line two</p>")
    assert "line one" in out
    assert "line two" in out


def test_build_feed_url_map_and_resolve_feed_id():
    from datasource import build_feed_url_map, resolve_feed_id

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE feed (id INTEGER PRIMARY KEY, url TEXT)")
    conn.execute("INSERT INTO feed VALUES (1, 'https://example.com/a?x=1')")
    conn.execute("INSERT INTO feed VALUES (2, 'https://example.com/b')")
    conn.execute("INSERT INTO feed VALUES (3, 'https://escaped.com/c?foo=1&amp;bar=2')")
    conn.commit()

    full_map, base_map = build_feed_url_map(conn)

    assert resolve_feed_id("https://example.com/a?x=1", full_map, base_map) == 1
    assert resolve_feed_id("https://example.com/a?x=999", full_map, base_map) == 1
    assert resolve_feed_id("https://example.com/b", full_map, base_map) == 2
    assert resolve_feed_id("https://escaped.com/c?foo=1&bar=2", full_map, base_map) == 3
    assert resolve_feed_id("https://unknown.test/", full_map, base_map) is None
    assert resolve_feed_id("", full_map, base_map) is None


def test_date_parser_standard():
    from datasource import _parse_date_standard

    assert _parse_date_standard("2024-01-15").strftime("%Y-%m-%d") == "2024-01-15"
    assert _parse_date_standard("  2024-01-15  ").strftime("%Y-%m-%d") == "2024-01-15"
    assert _parse_date_standard("not a date") is None


def test_date_parser_dlut_news():
    from datasource import _parse_date_dlut_news

    dt = _parse_date_dlut_news("<span>07</span><em>2024-03</em>")
    assert dt.strftime("%Y-%m-%d") == "2024-03-07"
    assert _parse_date_dlut_news("no match") is None


def test_date_parser_dlut_future():
    from datasource import _parse_date_dlut_future

    dt = _parse_date_dlut_future("<b>05</b> 2024-06")
    assert dt.strftime("%Y-%m-%d") == "2024-06-05"
    assert _parse_date_dlut_future("<em>nothing here</em>") is None


def test_date_parser_dlut_scidep():
    from datasource import _parse_date_dlut_scidep

    dt = _parse_date_dlut_scidep("<div>17</div> 2023-11")
    assert dt.strftime("%Y-%m-%d") == "2023-11-17"


def test_date_parser_dlut_recruitment_accepts_string_datetime():
    from datasource import _parse_date_dlut_recruitment

    dt = _parse_date_dlut_recruitment("2024-05-20 09:30:00")
    assert dt.strftime("%Y-%m-%d") == "2024-05-20"


def test_item_dataclass_defaults():
    from datasource import Item

    a = Item(title="T", date="2024-01-01")
    b = Item(title="T2", date="2024-01-02")
    a.extra["k"] = "v"
    # Ensure each instance has its own ``extra`` dict (field(default_factory=dict))
    assert b.extra == {}
    assert a.extra == {"k": "v"}
    assert a.url == "" and a.content == ""


@pytest.mark.parametrize(
    "parser_name, value",
    [
        ("_parse_date_dlut_news", "no span no match"),
        ("_parse_date_dlut_future", ""),
        ("_parse_date_dlut_scidep", ""),
        ("_parse_date_dlut_recruitment", ""),
    ],
)
def test_date_parsers_return_none_on_invalid_input(parser_name, value):
    import datasource

    parser = getattr(datasource, parser_name)
    assert parser(value) is None
