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

    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {
                "sources": [
                    {"name": "nature", "category": "papers", "type": "rss"},
                    {"name": "science", "category": "papers", "type": "rss"},
                ]
            },
            {},
            {},
        ),
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

    def run_one_source(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
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


def test_resume_drives_the_real_dispatch(monkeypatch):
    """Stubbing `_run_category_pipeline` hid that the flag was never set.

    With `PUBLICATION_INTEGRATION` left False, `papers` ran the legacy path:
    the collector stayed empty, the command reported "nothing new", and the
    legacy path marked the recovered items seen -- so they were lost for good.
    """
    from types import SimpleNamespace

    import resume_publication as resume
    import run_pipelines as rp

    _publish(
        "papers",
        "nature",
        "https://www.nature.com/articles/x",
        "10.1000/x",
        "# nature\n\nnature chunk",
    )

    source_cfg = {
        "name": "science",
        "category": "papers",
        "type": "scrape",
        "url": "https://www.science.org/feed",
        "display_name": "Science",
    }
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {"sources": [source_cfg]},
            {},
            {"one_line_summary": "Summarize {article_list}"},
        ),
    )
    item = PipelineItem(
        "Science paper",
        "2026-08-27",
        "https://www.science.org/doi/y",
        extra={"doi": "10.1000/y"},
    )
    ds = SimpleNamespace(
        name="science",
        category="papers",
        display_name="Science",
        lookback_hours=24,
        fetch=lambda: [item],
        get_batches=lambda values: [values],
        format_items=lambda values: "stub",
        commit_seen=lambda values: None,
    )
    monkeypatch.setattr(
        rp, "DataSource", SimpleNamespace(create=staticmethod(lambda *a, **kw: ds))
    )
    monkeypatch.setattr(
        rp,
        "sqlite3",
        SimpleNamespace(
            connect=lambda *a, **kw: SimpleNamespace(
                row_factory=None, execute=lambda *a, **kw: None, close=lambda: None
            ),
            Row=object,
        ),
    )
    monkeypatch.setattr(rp, "build_feed_url_map", lambda db: ({}, {}))
    monkeypatch.setattr(rp, "_ensure_rss_subscriptions", lambda cfg, db, category: [])
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response())

    sent: list[str] = []
    monkeypatch.setattr(
        resume,
        "send_to_discord",
        lambda channel, content: sent.append(content) or True,
    )
    monkeypatch.setattr(resume, "get_channel_id", lambda category: "channel-1")
    monkeypatch.setattr(resume, "_publish_web", lambda category: 0)

    assert resume.main("papers", "science") == 0

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert {item.source.name for item in bundle.items} == {"nature", "science"}
    assert len(sent) == 1
    assert "science" in sent[0]


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


def test_resume_dispatches_only_the_requested_source(monkeypatch):
    """The command promises one source's AI spend; the others must not run."""
    from types import SimpleNamespace

    import resume_publication as resume
    import run_pipelines as rp

    _publish(
        "papers",
        "nature",
        "https://www.nature.com/articles/x",
        "10.1000/x",
        "# nature\n\nnature chunk",
    )

    sources = [
        {
            "name": "nature",
            "category": "papers",
            "type": "scrape",
            "url": "https://www.nature.com",
        },
        {
            "name": "science",
            "category": "papers",
            "type": "scrape",
            "url": "https://www.science.org",
        },
    ]
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {"sources": sources},
            {},
            {"one_line_summary": "Summarize {article_list}"},
        ),
    )
    fetched: list[str] = []

    def make_ds(source_cfg, *_args, **_kwargs):
        name = source_cfg["name"]

        def fetch():
            fetched.append(name)
            return [
                PipelineItem(
                    f"{name} item",
                    "2026-08-27",
                    f"https://example.org/{name}",
                    extra={"doi": f"10.1000/{name}"},
                )
            ]

        return SimpleNamespace(
            name=name,
            category="papers",
            display_name=name,
            lookback_hours=24,
            fetch=fetch,
            get_batches=lambda values: [values],
            format_items=lambda values: "stub",
            commit_seen=lambda values: None,
        )

    monkeypatch.setattr(
        rp, "DataSource", SimpleNamespace(create=staticmethod(make_ds))
    )
    monkeypatch.setattr(
        rp,
        "sqlite3",
        SimpleNamespace(
            connect=lambda *a, **kw: SimpleNamespace(
                row_factory=None, execute=lambda *a, **kw: None, close=lambda: None
            ),
            Row=object,
        ),
    )
    monkeypatch.setattr(rp, "build_feed_url_map", lambda db: ({}, {}))
    monkeypatch.setattr(rp, "_ensure_rss_subscriptions", lambda cfg, db, category: [])
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response())
    monkeypatch.setattr(resume, "send_to_discord", lambda *args: True)
    monkeypatch.setattr(resume, "get_channel_id", lambda category: "channel-1")
    monkeypatch.setattr(resume, "_publish_web", lambda category: 0)

    assert resume.main("papers", "science") == 0
    assert fetched == ["science"]


def test_resume_does_not_post_a_chunk_the_merge_dropped(resume_env, monkeypatch):
    """The delta is what the merge kept, not what the run rendered."""
    resume, sent, web = resume_env
    import run_pipelines as rp

    # The bundle already carries the item this run will render again.
    _publish(
        "papers",
        "science",
        "https://www.science.org/doi/y",
        "10.1000/y",
        "# science\n\nscience chunk",
    )

    def run_known_item(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
        collector.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        collector.add_body("# science\n\nscience chunk again", source_name="science")
        rp._finalize_category_publication(category, collector)
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_known_item)

    assert resume.main("papers", "science") == 0

    assert sent == []
    assert web == []


def test_resume_does_not_post_a_chunk_a_concurrent_run_published(
    resume_env, monkeypatch
):
    """The delta is what this merge added, not what the store gained meanwhile.

    A cron run that publishes the same item while the resume is in flight makes
    the store's item set grow -- reading it again would credit that writer's
    item to this run and post a chunk the merge had dropped.
    """
    resume, sent, web = resume_env
    import run_pipelines as rp

    def run_with_a_concurrent_writer(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
        other = PublicationRunCollector("papers")
        other.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        other.add_body("# science\n\nscience chunk", source_name="science")
        rp._finalize_category_publication("papers", other)

        collector.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        collector.add_body("# science\n\nscience chunk", source_name="science")
        rp._finalize_category_publication(category, collector)
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_with_a_concurrent_writer)

    assert resume.main("papers", "science") == 0
    assert sent == []


def test_resume_records_the_delta_it_posted(resume_env):
    """Otherwise the next plain push reposts the whole day."""
    from publication import DeliveryState, DeliveryStateStore

    resume, sent, web = resume_env
    import run_pipelines as rp

    briefing_id = f"papers-{rp.DATE}"
    store = DeliveryStateStore()
    store.save(
        DeliveryState(
            schema_version=1,
            briefing_id=briefing_id,
            sink="discord",
            status="success",
            attempt_count=1,
            first_attempted_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
            last_attempted_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
            delivered_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        )
    )

    assert resume.main("papers", "science") == 0
    assert len(sent) == 1

    state = DeliveryStateStore().load(briefing_id, "discord")
    assert state is not None and state.status == "success"


def test_resume_posts_only_the_requested_source_chunk(resume_env, monkeypatch):
    """The delta is what was recovered, not whatever else the run rendered."""
    resume, sent, web = resume_env
    import run_pipelines as rp

    def run_two_sources(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
        collector.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        collector.add_body("# science\n\nscience chunk", source_name="science")
        collector.add(
            _results_for("nature", "https://www.nature.com/articles/z", "10.1000/z")
        )
        collector.add_body("# nature\n\nnature chunk", source_name="nature")
        # Production finalizes inside the category pipeline; the delta is what
        # the merge kept, so a stub that skips it would post nothing.
        rp._finalize_category_publication(category, collector)
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_two_sources)

    assert resume.main("papers", "science") == 0

    assert len(sent) == 1
    _channel, content = sent[0]
    assert "science chunk" in content
    assert "nature chunk" not in content


def test_resume_reports_when_the_source_has_nothing_new(resume_env, monkeypatch):
    resume, sent, web = resume_env
    import run_pipelines as rp

    monkeypatch.setattr(rp, "_run_category_pipeline", lambda *a, **kw: 0)

    assert resume.main("papers", "science") == 0

    assert sent == []
    assert web == []


def test_resume_fails_when_the_source_fails_again(resume_env, monkeypatch):
    """A second failure is not a successful recovery."""
    resume, sent, web = resume_env
    import run_pipelines as rp

    def run_failing_source(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
        collector.add_failure("science: fetch failed: feed down")
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_failing_source)

    assert resume.main("papers", "science") == 1

    assert sent == []
    assert web == []


def test_resume_renders_the_web_even_when_discord_fails(resume_env, monkeypatch):
    """The Web render must not depend on the Discord send succeeding."""
    resume, _sent, web = resume_env
    monkeypatch.setattr(resume, "send_to_discord", lambda *args: False)

    assert resume.main("papers", "science") == 1

    assert web == ["papers"]


def test_resume_rejects_a_source_it_cannot_dispatch(monkeypatch):
    """A source with no dispatchable type is "known" but never runs."""
    import resume_publication as resume
    import run_pipelines as rp

    logs: list[str] = []
    monkeypatch.setattr(resume, "log", logs.append)
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {"sources": [{"name": "ghost", "category": "papers"}]},
            {},
            {},
        ),
    )

    assert resume.main("papers", "ghost") == 1
    assert any("ghost" in line for line in logs)


def test_resume_delivers_the_part_that_merged_before_reporting_failure(
    resume_env, monkeypatch
):
    """A partial recovery still publishes what it recovered."""
    resume, sent, web = resume_env
    import run_pipelines as rp

    def run_partial(
        category,
        *,
        create_marker=False,
        deep_content=False,
        collector=None,
        only_source=None,
    ):
        collector.add(
            _results_for("science", "https://www.science.org/doi/y", "10.1000/y")
        )
        collector.add_body("# science\n\nscience chunk", source_name="science")
        rp._finalize_category_publication(category, collector)
        collector.add_failure("science: 2 item(s) lacked valid structured AI output")
        return 1

    monkeypatch.setattr(rp, "_run_category_pipeline", run_partial)

    assert resume.main("papers", "science") == 1
    assert len(sent) == 1
    assert web == ["papers"]


def test_resume_rejects_an_unknown_source(monkeypatch):
    import resume_publication as resume
    import run_pipelines as rp

    logs: list[str] = []
    monkeypatch.setattr(resume, "log", logs.append)
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {"sources": [{"name": "nature", "category": "papers", "type": "rss"}]},
            {},
            {},
        ),
    )

    assert resume.main("papers", "sciense") == 1
    assert any("sciense" in line for line in logs)


def test_resume_refuses_a_day_without_a_briefing(monkeypatch):
    import resume_publication as resume
    import run_pipelines as rp

    monkeypatch.setattr(resume, "log", lambda *_args: None)
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {"sources": [{"name": "science", "category": "papers", "type": "rss"}]},
            {},
            {},
        ),
    )

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
