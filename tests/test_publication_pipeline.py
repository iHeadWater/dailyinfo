"""Phase 2B structured pipeline-to-publication boundary tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sys
from types import SimpleNamespace

import pytest

from conftest import FIXTURES_DIR, FakeResponse, read_fixture
from datasource import Item as PipelineItem
from publication import PublicationBriefingInput, PublicationFinalizer, PublicationStore
from publication.pipeline import (
    PublicationRunCollector,
    StructuredResultError,
    parse_structured_response,
    results_from_response,
    structured_entries,
)


UTC = timezone.utc


def _response(*refs: str) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "source_ref": ref,
                    "summary": f"Structured summary for {ref}.",
                    "why_it_matters": None,
                    "tags": [],
                }
                for ref in refs
            ]
        },
        ensure_ascii=False,
    )


def test_structured_response_requires_exact_source_refs():
    assert parse_structured_response(_response("item-0001"), ["item-0001"])[
        "item-0001"
    ]["summary"]

    with pytest.raises(StructuredResultError, match="missing source_ref"):
        parse_structured_response(_response("item-0001"), ["item-0001", "item-0002"])
    with pytest.raises(StructuredResultError, match="duplicate"):
        parse_structured_response(
            json.dumps(
                {
                    "items": [
                        {"source_ref": "item-0001", "summary": "a"},
                        {"source_ref": "item-0001", "summary": "b"},
                    ]
                }
            ),
            ["item-0001"],
        )
    with pytest.raises(StructuredResultError, match="valid JSON"):
        parse_structured_response("1. **Legacy Markdown**", ["item-0001"])


@pytest.mark.parametrize(
    ("category", "source_name", "url", "external_id"),
    [
        ("papers", "nature", "https://www.nature.com/articles/demo", "doi:10.1/demo"),
        ("ai_news", "smolai_news", "https://news.smol.ai/p/demo", "smolai-demo"),
        ("code", "github_trending", "https://github.com/org/repo", "org/repo"),
        (
            "resource",
            "dlut_zhxw",
            "https://news.dlut.edu.cn/info/demo.htm",
            "dlut-demo",
        ),
        ("arxiv", "arxiv_cs_ai", "https://arxiv.org/abs/2608.12345", "2608.12345"),
    ],
)
def test_realistic_five_category_structured_results_finalize(
    category, source_name, url, external_id
):
    item = PipelineItem(
        title=f"{category} source title",
        date="2026-08-27",
        url=url,
        extra=(
            {"doi": external_id} if category == "papers" else {"item_id": external_id}
        ),
    )
    results = results_from_response(
        _response("item-0001"),
        [item],
        retrieved_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        source_name=source_name,
    )
    collector = PublicationRunCollector(category)
    collector.add(results)
    collector.add_body(f"# {category}\n\n{results[0].summary}")
    published_at = datetime(2026, 8, 27, 2, tzinfo=UTC)
    bundle = PublicationFinalizer().finalize(
        PublicationBriefingInput(
            category=category,
            date="2026-08-27",
            title=f"{category} briefing",
            generated_at=published_at,
            published_at=published_at,
            body=collector.body,
        ),
        collector.item_inputs(published_at=published_at),
    )
    assert bundle.briefing.item_ids == [bundle.items[0].id]
    assert bundle.items[0].summary.startswith("Structured summary")


def test_regular_pipeline_uses_one_structured_result_for_markdown_and_publication(
    monkeypatch,
):
    import run_pipelines as rp

    item = PipelineItem(
        title="A paper with immutable source facts",
        date="2026-08-27",
        url="https://example.org/paper/1",
        content="raw source content must not become the summary",
    )
    ds = SimpleNamespace(
        name="fixture_papers",
        category="papers",
        display_name="Fixture Papers",
        lookback_hours=24,
        _total_before_filter=1,
        fetch=lambda: [item],
        get_batches=lambda values: [values],
        format_items=lambda values: "\n".join(
            f"{index + 1}. {value.title}" for index, value in enumerate(values)
        ),
        commit_seen=lambda values: None,
    )
    calls = []

    def fake_call_ai(prompt, **kwargs):
        calls.append(prompt)
        return _response("item-0001")

    monkeypatch.setattr(rp, "PUBLICATION_INTEGRATION", True)
    monkeypatch.setattr(rp, "call_ai", fake_call_ai)
    collector = PublicationRunCollector("papers")
    saved = rp._process_regular_source(
        ds,
        {},
        "stub/model",
        {"one_line_summary": "Summarize {count}: {article_list}"},
        "one_line_summary",
        collector,
    )
    rp._finalize_category_publication("papers", collector)

    assert saved == 1
    assert len(calls) == 1
    assert "source_ref=item-0001" in calls[0]
    store = PublicationStore()
    bundle = store.load_bundle(f"papers-{rp.DATE}")
    assert bundle.items[0].title == item.title
    assert bundle.items[0].summary.startswith("Structured summary")
    assert item.content not in bundle.items[0].summary
    assert bundle.items[0].id in bundle.briefing.item_ids
    assert bundle.briefing.body == collector.body


def test_structured_entries_are_explicit_and_do_not_use_title_matching():
    item = PipelineItem("Same title", "2026-08-27", "https://example.org/a")
    formatter = SimpleNamespace(format_items=lambda items: items[0].title)
    entries, refs = structured_entries(formatter, [item])
    assert refs == ["item-0001"]
    assert entries.startswith("[source_ref=item-0001]")


def test_existing_code_pipeline_reaches_canonical_store(monkeypatch, fake_requests):
    """The real code-source path uses structured output before storing."""

    import run_pipelines as rp
    from paths import BRIEFINGS_DIR

    monkeypatch.setattr(rp, "SOURCES_JSON", str(FIXTURES_DIR / "sources_min.json"))
    fake_requests.register(
        "https://github.com/trending",
        FakeResponse(status=200, text=read_fixture("github_trending.html")),
    )

    def structured_reply(prompt, **_kwargs):
        refs = sorted(set(re.findall(r"\[source_ref=(item-\d+)\]", prompt)))
        return _response(*refs)

    monkeypatch.setattr(rp, "call_ai", structured_reply)
    monkeypatch.setattr(rp, "PUBLICATION_INTEGRATION", True)
    assert rp.run_pipeline_code() == 1
    body = (BRIEFINGS_DIR / "code").glob("github_trending_briefing_*.md")
    assert next(body).exists()
    assert PublicationStore().load_bundle(f"code-{rp.DATE}").items


def test_existing_resource_pipeline_reaches_canonical_store(monkeypatch, tmp_path):
    """The unified DLUT resource path keeps section/source metadata structured."""

    import run_pipelines as rp

    source_cfg = {
        "name": "dlut_zhxw",
        "display_name": "DLUT 综合新闻",
        "category": "resource",
        "type": "scrape",
        "enabled": True,
        "news_group": "dlut_news",
        "section": "综合新闻",
        "url": "https://news.dlut.edu.cn/zhxw.htm",
    }
    config = {
        "defaults": {"model": "stub/model"},
        "prompt_templates": {"university_news_unified": "News: {items}"},
        "sources": [source_cfg],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    item = PipelineItem(
        title="A structured university notice",
        date="2026-08-27",
        url="https://news.dlut.edu.cn/info/notice.htm",
    )
    ds = SimpleNamespace(
        name="dlut_zhxw",
        display_name="DLUT 综合新闻",
        category="resource",
        fetch=lambda: [item],
        format_items=lambda values: values[0].title,
    )
    monkeypatch.setattr(rp, "SOURCES_JSON", str(config_path))
    monkeypatch.setattr(
        rp.DataSource, "create", staticmethod(lambda *_args, **_kwargs: ds)
    )
    monkeypatch.setattr(rp, "PUBLICATION_INTEGRATION", True)
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response("item-0001"))

    assert rp.run_pipeline_resource() == 1
    bundle = PublicationStore().load_bundle(f"resource-{rp.DATE}")
    assert bundle.items[0].source.name == "dlut_zhxw"
    assert "综合新闻" in bundle.briefing.body


def test_ai_news_deep_content_uses_structured_summary(monkeypatch):
    import run_pipelines as rp

    item = PipelineItem(
        title="AI newsletter item",
        date="2026-08-27",
        url="https://news.smol.ai/p/item-1",
        content="Long raw newsletter content stays input-only.",
    )
    ds = SimpleNamespace(
        name="smolai_news",
        category="ai_news",
        fetch=lambda: [item],
        commit_seen=lambda values: None,
    )
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response("item-0001"))
    monkeypatch.setattr(rp, "PUBLICATION_INTEGRATION", True)
    collector = PublicationRunCollector("ai_news")
    assert (
        rp._process_deep_content_source_publication(
            ds,
            {"prompt_template": "Summarize: {content}"},
            "stub/model",
            {"smolai_categorized": "Summarize: {content}"},
            collector,
        )
        == 1
    )
    rp._finalize_category_publication("ai_news", collector)
    bundle = PublicationStore().load_bundle(f"ai_news-{rp.DATE}")
    assert bundle.items[0].summary.startswith("Structured summary")
    assert item.content not in bundle.items[0].summary


def test_run_returns_nonzero_when_publication_finalization_fails(monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "load_api_key", lambda: "")
    monkeypatch.setattr(rp, "log", lambda *_args: None)

    def fail_pipeline():
        raise rp.PublicationIntegrationError("fixture finalizer failure")

    monkeypatch.setattr(rp, "run_pipeline_code", fail_pipeline)
    monkeypatch.setattr(sys, "argv", ["run_pipelines.py", "--pipeline", "4"])
    assert rp.main() == 1


def test_finalizer_failure_does_not_commit_deferred_seen_state(monkeypatch):
    import run_pipelines as rp

    item = PipelineItem("Bad URL item", "2026-08-27", "ftp://internal.invalid/item")
    seen = []
    ds = SimpleNamespace(
        name="bad_source",
        category="papers",
        display_name="Bad Source",
        lookback_hours=24,
        fetch=lambda: [item],
        get_batches=lambda values: [values],
        format_items=lambda values: values[0].title,
        commit_seen=lambda values: seen.extend(values),
    )
    monkeypatch.setattr(rp, "PUBLICATION_INTEGRATION", True)
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response("item-0001"))
    collector = PublicationRunCollector("papers")
    rp._process_regular_source(
        ds,
        {},
        "stub/model",
        {"one_line_summary": "Summarize {article_list}"},
        "one_line_summary",
        collector,
    )
    with pytest.raises(rp.PublicationIntegrationError):
        rp._finalize_category_publication("papers", collector)
    assert seen == []
