"""Tests for :class:`scripts.datasource.APIDataSource`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from conftest import fixture_path

DEFAULTS = {"lookback_hours": 24}

HF_FIELD_MAP = {
    "extract": {
        "fields": {
            "name": "id",
            "task": "pipeline_tag",
            "likes": "likes",
            "downloads": "downloads",
        }
    }
}


def _load_hf_fixture():
    return json.loads(fixture_path("huggingface_models.json").read_text())


def test_huggingface_models_parse_fields():
    from datasource import APIDataSource

    ds = APIDataSource(
        {
            "name": "huggingface_models",
            "display_name": "HF Models",
            "category": "ai_news",
            "url": "https://example.test/api/models",
            "max_items": 25,
            **HF_FIELD_MAP,
        },
        DEFAULTS,
    )

    items = ds._parse_huggingface(_load_hf_fixture())
    assert len(items) == 3

    first = items[0]
    assert first.title == "org/model-a"
    assert first.url == "https://huggingface.co/org/model-a"
    assert first.extra["task"] == "text-generation"
    assert first.extra["likes"] == 123
    assert first.extra["downloads"] == 45678


def test_huggingface_daily_papers_parse_top_percent_and_metadata():
    from datasource import APIDataSource

    rows = [
        {
            "paper": {
                "id": f"2401.0000{i}",
                "title": f"Paper {i}",
                "summary": f"Summary {i}",
                "upvotes": i + 1,
                "submittedOnDailyAt": "2026-08-21T00:00:00.000Z",
                "githubRepo": "https://github.com/example/repo",
            }
        }
        for i in range(10)
    ]
    ds = APIDataSource(
        {
            "name": "hf_daily_papers",
            "category": "arxiv",
            "url": "https://huggingface.co/api/daily_papers",
            "top_percent": 30,
        },
        DEFAULTS,
    )

    items = ds._parse_hf_daily_papers(rows)
    assert len(items) == 3
    assert items[0].url == "https://arxiv.org/abs/2401.00009"
    assert items[0].content == "Summary 9"
    assert items[0].extra["upvotes"] == 10
    assert "github.com/example/repo" in items[0].extra["code_url"]


def _hf_rows(day: str, count: int = 3) -> list[dict]:
    return [
        {
            "paper": {
                "id": f"2608.1000{i}",
                "title": f"Paper {i} on {day}",
                "summary": f"Summary {i}",
                "upvotes": i + 1,
                "submittedOnDailyAt": f"{day}T00:00:00.000Z",
            }
        }
        for i in range(count)
    ]


class _RecordingGet:
    """Stub for ``requests.get`` returning per-date Daily Papers payloads."""

    def __init__(self, by_date: dict[str, list[dict]]):
        self.by_date = by_date
        self.requested: list[str] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        day = (params or {})["date"]
        self.requested.append(day)
        rows = self.by_date.get(day, [])

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return rows

        return _Resp()


def _hf_source(tmp_path, monkeypatch, date: str):
    import datasource
    from datasource import APIDataSource

    monkeypatch.setattr(datasource, "_STATE_DIR", tmp_path)
    return APIDataSource(
        {
            "name": "hf_daily_papers",
            "category": "arxiv",
            "url": "https://huggingface.co/api/daily_papers",
            "top_percent": 100,
            "date": date,
        },
        DEFAULTS,
    )


def test_hf_daily_papers_requests_the_configured_date(tmp_path, monkeypatch):
    """A weekday is fetched as that day's page, keeping every paper."""
    import datasource

    get = _RecordingGet({"2026-08-24": _hf_rows("2026-08-24", 26)})
    monkeypatch.setattr(datasource.requests, "get", get)

    items = _hf_source(tmp_path, monkeypatch, "2026-08-24").fetch()

    assert get.requested == ["2026-08-24"]
    assert len(items) == 26  # no max_items cap
    assert {it.date for it in items} == {"2026-08-24"}


def test_hf_daily_papers_skips_weekend_without_requesting(tmp_path, monkeypatch):
    """Hugging Face publishes Mon-Fri; weekends cost no request and yield none."""
    import datasource

    get = _RecordingGet({})
    monkeypatch.setattr(datasource.requests, "get", get)

    for weekend_day in ("2026-08-22", "2026-08-23"):  # Saturday, Sunday
        assert _hf_source(tmp_path, monkeypatch, weekend_day).fetch() == []

    assert get.requested == []


def test_hf_daily_papers_respects_seen_state(tmp_path, monkeypatch):
    """A same-day rerun must not re-emit papers already committed."""
    import datasource

    get = _RecordingGet({"2026-08-21": _hf_rows("2026-08-21")})
    monkeypatch.setattr(datasource.requests, "get", get)

    src = _hf_source(tmp_path, monkeypatch, "2026-08-21")
    first = src.fetch()
    assert len(first) == 3
    src.commit_seen(first)

    assert _hf_source(tmp_path, monkeypatch, "2026-08-21").fetch() == []


def test_huggingface_daily_papers_format_includes_heat_and_summary():
    from datasource import APIDataSource, Item

    ds = APIDataSource(
        {"name": "hf_daily_papers", "category": "arxiv", "url": "x"},
        DEFAULTS,
    )
    out = ds.format_items(
        [
            Item(
                title="Hydro paper",
                date="2026-08-21",
                url="https://arxiv.org/abs/1",
                content="A summary",
                extra={"upvotes": 12},
            )
        ]
    )
    assert "⭐ 12 upvotes" in out
    assert "A summary" in out


def test_huggingface_parse_handles_non_list_data():
    from datasource import APIDataSource

    ds = APIDataSource(
        {
            "name": "huggingface_models",
            "url": "x",
            "category": "ai_news",
            **HF_FIELD_MAP,
        },
        DEFAULTS,
    )
    assert ds._parse_huggingface({"not": "a list"}) == []


def test_huggingface_parse_respects_max_items():
    from datasource import APIDataSource

    ds = APIDataSource(
        {
            "name": "huggingface_models",
            "url": "x",
            "category": "ai_news",
            "max_items": 2,
            **HF_FIELD_MAP,
        },
        DEFAULTS,
    )
    assert len(ds._parse_huggingface(_load_hf_fixture())) == 2


def test_huggingface_fetch_end_to_end(fake_requests):
    from conftest import FakeResponse
    from datasource import DataSource

    fake_requests.register(
        "https://example.test/api/models",
        FakeResponse(status=200, json_data=_load_hf_fixture()),
    )

    ds = DataSource.create(
        {
            "name": "huggingface_models",
            "display_name": "HF Models",
            "category": "ai_news",
            "type": "api",
            "url": "https://example.test/api/models",
            **HF_FIELD_MAP,
        },
        DEFAULTS,
    )
    items = ds.fetch()
    assert len(items) == 3
    assert all(it.url.startswith("https://huggingface.co/") for it in items)


def test_huggingface_models_format_items():
    from datasource import APIDataSource, Item

    ds = APIDataSource(
        {"name": "huggingface_models", "url": "x", "category": "ai_news"},
        DEFAULTS,
    )
    items = [
        Item(
            title="a",
            date="2024-01-01",
            extra={
                "name": "org/a",
                "task": "text-generation",
                "likes": 1,
                "downloads": 2,
            },
        ),
        Item(
            title="b",
            date="2024-01-01",
            extra={"name": "org/b", "likes": 3, "downloads": 4},
        ),
    ]
    out = ds.format_items(items)
    assert "**org/a** (text-generation)" in out
    assert "likes 1, downloads 2" in out
    assert "**org/b**" in out and "(text-generation)" not in out.split("\n")[1]


def test_huggingface_datasets_format_items():
    from datasource import APIDataSource, Item

    ds = APIDataSource(
        {"name": "huggingface_datasets", "url": "x", "category": "ai_news"},
        DEFAULTS,
    )
    items = [
        Item(
            title="d",
            date="2024-01-01",
            extra={"name": "org/ds", "likes": 7, "downloads": 8},
        )
    ]
    out = ds.format_items(items)
    assert "**org/ds**" in out
    assert "likes 7, downloads 8" in out


def test_huggingface_spaces_format_items():
    from datasource import APIDataSource, Item

    ds = APIDataSource(
        {"name": "huggingface_spaces", "url": "x", "category": "ai_news"},
        DEFAULTS,
    )
    items = [
        Item(
            title="s",
            date="2024-01-01",
            extra={"name": "org/sp", "sdk": "gradio", "likes": 9},
        )
    ]
    out = ds.format_items(items)
    assert "**org/sp**" in out
    assert "[gradio]" in out
    assert "likes 9" in out


def test_dlut_api_object_list_shape_and_cutoff():
    from datasource import APIDataSource

    now = datetime.now()
    recent = now - timedelta(hours=2)
    old = now - timedelta(days=5)

    api_data = {
        "object": {
            "list": [
                {
                    "title": "Recent event",
                    "publishDate": recent.strftime("%Y-%m-%d %H:%M:%S"),
                },
                {
                    "title": "Old event",
                    "publishDate": old.strftime("%Y-%m-%d %H:%M:%S"),
                },
                {"title": "", "publishDate": recent.strftime("%Y-%m-%d %H:%M:%S")},
            ]
        }
    }

    ds = APIDataSource(
        {
            "name": "dlut_recruitment",
            "display_name": "DLUT Recruit",
            "category": "resource",
            "url": "https://dlut.example.edu/api/list",
            "list_url": "https://dlut.example.edu/list",
            "extract": {"fields": {"title": "title", "date": "publishDate"}},
            "max_items": 10,
        },
        DEFAULTS,
    )

    items = ds._parse_dlut_api(api_data)
    assert [it.title for it in items] == ["Recent event"]
    assert items[0].url == "https://dlut.example.edu/list"


def test_dlut_api_flat_list_shape():
    from datasource import APIDataSource

    now = datetime.now()
    api_data = [
        {"title": "A", "publishDate": now.strftime("%Y-%m-%d %H:%M:%S")},
        {"title": "B", "publishDate": now.strftime("%Y-%m-%d %H:%M:%S")},
    ]

    ds = APIDataSource(
        {
            "name": "dlut_recruitment",
            "category": "resource",
            "url": "https://x.test/",
            "extract": {"fields": {"title": "title", "date": "publishDate"}},
            "max_items": 1,
        },
        DEFAULTS,
    )
    assert len(ds._parse_dlut_api(api_data)) == 1


def test_dlut_api_list_key_shape():
    from datasource import APIDataSource

    now = datetime.now()
    api_data = {
        "list": [
            {"title": "first", "publishDate": now.strftime("%Y-%m-%d %H:%M:%S")},
        ]
    }
    ds = APIDataSource(
        {
            "name": "dlut_recruitment",
            "category": "resource",
            "url": "https://x.test/",
            "extract": {"fields": {"title": "title", "date": "publishDate"}},
            "max_items": 10,
        },
        DEFAULTS,
    )
    assert [it.title for it in ds._parse_dlut_api(api_data)] == ["first"]


def test_crossref_parse_uses_online_date_for_new_items():
    from datasource import APIDataSource

    now = datetime.now()
    old = now - timedelta(days=60)
    api_data = {
        "message": {
            "items": [
                {
                    "title": ["Recently posted online"],
                    "URL": "https://doi.org/10.3724/example",
                    "DOI": "10.3724/example",
                    "published-print": {
                        "date-parts": [[old.year, old.month, old.day]]
                    },
                    "published-online": {
                        "date-parts": [[now.year, now.month, now.day]]
                    },
                }
            ]
        }
    }
    ds = APIDataSource(
        {
            "name": "shuili_xuebao",
            "category": "papers",
            "url": "https://api.crossref.org/works",
            "parser": "crossref",
            "lookback_hours": 24,
        },
        DEFAULTS,
    )

    items = ds._parse_crossref(api_data)

    assert [item.title for item in items] == ["Recently posted online"]
    assert items[0].date == now.strftime("%Y-%m-%d")


def test_shuili_xuebao_config_sorts_by_updated():
    cfg_path = Path(__file__).parent.parent / "config" / "sources.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    source = next(s for s in cfg["sources"] if s["name"] == "shuili_xuebao")

    assert "sort=updated" in source["url"]


def test_crossref_fetch_filters_seen_articles(fake_requests):
    from conftest import FakeResponse
    from datasource import DataSource

    now = datetime.now()
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Hydraulic engineering paper"],
                    "URL": "https://doi.org/10.1234/example",
                    "DOI": "10.1234/example",
                    "published": {
                        "date-parts": [[now.year, now.month, now.day]]
                    },
                }
            ]
        }
    }
    fake_requests.register(
        "https://api.crossref.org/works",
        FakeResponse(status=200, json_data=payload),
    )

    cfg = {
        "name": "shuili_xuebao",
        "display_name": "水利学报",
        "category": "papers",
        "type": "api",
        "url": "https://api.crossref.org/works",
        "parser": "crossref",
        "lookback_hours": 720,
    }

    first = DataSource.create(cfg, DEFAULTS)
    first_items = first.fetch()
    assert [item.title for item in first_items] == ["Hydraulic engineering paper"]
    first.commit_seen(first_items)

    second = DataSource.create(cfg, DEFAULTS)
    assert second.fetch() == []


def test_dlut_recruitment_filters_expired_deadlines(monkeypatch):
    import datetime

    import datasource
    from datasource import APIDataSource

    monkeypatch.setattr(
        datasource, "NOW", datetime.datetime(2026, 5, 27, 7, 44, 0)
    )
    api_data = {
        "object": {
            "list": [
                {
                    "id": "old",
                    "title": "Expired internship",
                    "startTime": "2026-05-27 01:00:00",
                    "endTime": "2026-05-26 00:00:00",
                },
                {
                    "id": "today",
                    "title": "Today deadline internship",
                    "startTime": "2026-05-27 01:00:00",
                    "endTime": "2026-05-27 00:00:00",
                },
                {
                    "id": "future",
                    "title": "Future internship",
                    "startTime": "2026-05-27 01:00:00",
                    "endTime": "2026-05-28 00:00:00",
                },
            ]
        }
    }
    ds = APIDataSource(
        {
            "name": "dlut_internship",
            "category": "resource",
            "url": "https://x.test/",
            "list_url": "https://x.test/list",
            "extract": {
                "fields": {
                    "title": "title",
                    "date": "startTime",
                    "deadline": "endTime",
                }
            },
            "max_items": 10,
        },
        DEFAULTS,
    )

    items = ds._parse_dlut_api(api_data)

    assert [item.title for item in items] == [
        "Today deadline internship",
        "Future internship",
    ]
    assert [item.extra["deadline"] for item in items] == [
        "2026-05-27",
        "2026-05-28",
    ]


def test_dlut_recruitment_expired_cursor_item_stops_pagination(monkeypatch):
    import datetime

    import datasource
    from datasource import APIDataSource

    monkeypatch.setattr(
        datasource, "NOW", datetime.datetime(2026, 5, 27, 7, 44, 0)
    )
    ds = APIDataSource(
        {
            "name": "dlut_internship",
            "category": "resource",
            "url": "https://x.test/",
            "list_url": "https://x.test/list",
            "extract": {
                "fields": {
                    "title": "title",
                    "date": "startTime",
                    "deadline": "endTime",
                }
            },
        },
        DEFAULTS,
    )
    rows = [
        {
            "id": "new",
            "title": "New internship",
            "startTime": "2026-05-27 02:00:00",
            "endTime": "2026-05-28 00:00:00",
        },
        {
            "id": "cursor",
            "title": "Previously seen expired internship",
            "startTime": "2026-05-27 01:00:00",
            "endTime": "2026-05-26 00:00:00",
        },
        {
            "id": "older",
            "title": "Older internship",
            "startTime": "2026-05-27 00:00:00",
            "endTime": "2026-05-28 00:00:00",
        },
    ]

    items, should_stop = ds._parse_dlut_api_rows(
        rows,
        cursor={"last_id": "cursor", "last_time": "2026-05-27 01:00:00"},
    )

    assert [item.title for item in items] == ["New internship"]
    assert should_stop is True


def test_dlut_recruitment_format_items_includes_deadline():
    from datasource import APIDataSource, Item

    ds = APIDataSource(
        {"name": "dlut_internship", "url": "x", "category": "resource"},
        DEFAULTS,
    )
    out = ds.format_items(
        [
            Item(
                title="Internship",
                date="2026-05-27",
                extra={"deadline": "2026-05-28"},
            )
        ]
    )

    assert "截止: 2026-05-28" in out
