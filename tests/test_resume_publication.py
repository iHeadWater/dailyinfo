"""Resuming one source into the day's canonical briefing."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from datasource import Item as PipelineItem
from publication import PublicationRunCollector, PublicationStore
from publication.pipeline import results_from_response


UTC = timezone.utc


def _response() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "source_ref": "item-0001",
                    "summary": "Structured summary for item-0001.",
                    "why_it_matters": None,
                    "tags": [],
                }
            ]
        },
        ensure_ascii=False,
    )


def _results_for(source_name, url, external_id):
    item = PipelineItem(
        title=f"{source_name} title",
        date="2026-08-27",
        url=url,
        extra={"doi": external_id},
    )
    return results_from_response(
        _response(),
        [item],
        retrieved_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        source_name=source_name,
    )


def _publish(category, source_name, url, external_id, chunk):
    collector = PublicationRunCollector(category)
    collector.add(_results_for(source_name, url, external_id))
    collector.add_body(chunk, source_name=source_name)
    import run_pipelines as rp

    rp._finalize_category_publication(category, collector)


@pytest.fixture
def resume_env(monkeypatch):
    """A published day, a stubbed one-source run, and captured delivery."""
    import resume_publication as resume
    import run_pipelines as rp

    _publish(
        "papers",
        "nature",
        "https://www.nature.com/articles/x",
        "10.1000/x",
        "# nature\n\nnature chunk",
    )

    sent: list[tuple[str, str]] = []
    web: list[tuple] = []
    monkeypatch.setattr(
        resume,
        "send_to_discord",
        lambda channel, content: sent.append((channel, content)) or True,
    )
    monkeypatch.setattr(resume, "get_channel_id", lambda category: "channel-1")
    monkeypatch.setattr(
        resume, "_publish_web", lambda category: web.append(category) or 0
    )

    def run_one_source(category, *, deep_content=False, collector=None):
        collector.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        collector.add_body("# science\n\nscience chunk", source_name="science")
        # Production finalizes inside the category pipeline, which is what
        # merges the recovered source into the day's bundle.
        rp._finalize_category_publication(category, collector)
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_one_source)
    return resume, sent, web


def test_resume_sends_only_the_chunk_it_recovered(resume_env):
    resume, sent, web = resume_env

    assert resume.main("papers", "science") == 0

    assert len(sent) == 1
    channel, content = sent[0]
    assert channel == "channel-1"
    assert "science chunk" in content
    # The part already delivered is not posted again.
    assert "nature chunk" not in content
    assert web == ["papers"]


def test_resume_does_not_repost_a_source_the_bundle_already_has(resume_env, monkeypatch):
    resume, sent, web = resume_env
    import run_pipelines as rp

    def run_known_source(category, *, deep_content=False, collector=None):
        collector.add(
            _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
        )
        collector.add_body("# nature\n\nnature chunk", source_name="nature")
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_known_source)

    assert resume.main("papers", "nature") == 0

    assert sent == []
    assert web == ["papers"]


def test_resume_reports_when_the_source_has_nothing_new(resume_env, monkeypatch):
    resume, sent, web = resume_env
    import run_pipelines as rp

    monkeypatch.setattr(
        rp, "_run_category_pipeline", lambda *a, **kw: 0
    )

    assert resume.main("papers", "science") == 0

    assert sent == []
    assert web == []


def test_resume_refuses_a_day_without_a_briefing(monkeypatch):
    import resume_publication as resume

    monkeypatch.setattr(resume, "log", lambda *_args: None)

    assert resume.main("papers", "science") == 1


def test_resume_rejects_categories_it_cannot_rerun_one_source_of(monkeypatch):
    import resume_publication as resume

    monkeypatch.setattr(resume, "log", lambda *_args: None)

    assert resume.main("code", "github_trending") == 2


def test_the_recovered_source_reaches_the_merged_bundle(resume_env):
    """Resume is not only a delivery: the bundle gains the source."""
    import run_pipelines as rp

    resume, _sent, _web = resume_env

    assert resume.main("papers", "science") == 0

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert {item.source.name for item in bundle.items} == {"nature", "science"}
    assert "science chunk" in bundle.briefing.body
