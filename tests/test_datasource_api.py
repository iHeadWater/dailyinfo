"""Tests for :class:`scripts.datasource.APIDataSource`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

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
