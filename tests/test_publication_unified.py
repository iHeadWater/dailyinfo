"""Unified publication boundary: partial sources, dedup, and resume."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

import pytest

from datasource import Item as PipelineItem
from publication import (
    PublicationBriefingInput,
    PublicationFinalizer,
    PublicationRunCollector,
    PublicationStore,
    validate_bundle,
)
from publication.pipeline import results_from_response


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


def _collector_with_one_source(category, source_name, url="https://example.org/a"):
    item = PipelineItem(
        title=f"{source_name} title",
        date="2026-08-27",
        url=url,
        extra={"item_id": url},
    )
    results = results_from_response(
        _response("item-0001"),
        [item],
        retrieved_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        source_name=source_name,
    )
    collector = PublicationRunCollector(category)
    collector.add(results)
    collector.add_body(
        f"# {source_name}\n\n{results[0].summary}", source_name=source_name
    )
    return collector


def _results_for(source_name, url, external_id=None):
    item = PipelineItem(
        title=f"{source_name} title",
        date="2026-08-27",
        url=url,
        extra={"doi": external_id} if external_id else {"item_id": url},
    )
    return results_from_response(
        _response("item-0001"),
        [item],
        retrieved_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
        source_name=source_name,
    )


def test_a_repeated_item_is_dropped_before_it_reaches_the_bundle():
    """One fetch can hand back the same paper twice.

    The contract keys items by identity, so the second copy used to make
    validate_bundle reject the whole bundle -- and the category then went
    missing from Discord and the Web together.
    """
    collector = PublicationRunCollector("papers")
    first = _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
    repeat = _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")

    kept = collector.take_new(first)
    collector.add(kept)
    dropped = collector.take_new(repeat)

    assert len(kept) == 1
    assert dropped == []
    assert len(collector.dropped_duplicates) == 1
    assert "nature" in collector.dropped_duplicates[0]


def test_a_repeated_item_does_not_lose_the_category(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    monkeypatch.setattr(rp, "log", logs.append)

    collector = PublicationRunCollector("papers")
    collector.add(
        collector.take_new(
            _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
        )
    )
    collector.take_new(
        _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
    )
    collector.add_body(
        "# nature\n\nStructured summary for item-0001.", source_name="nature"
    )

    rp._finalize_category_publication("papers", collector)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert len(bundle.items) == 1
    assert len(bundle.briefing.item_ids) == 1
    # The drop is reported, not silent.
    assert any(
        "duplicate" in line and "nature" in line for line in logs
    ), logs


def _bundle_for(source_name, url, external_id=None):
    """A finalized one-source bundle, plus the chunk that produced its body."""
    collector = PublicationRunCollector("papers")
    collector.add(_results_for(source_name, url, external_id))
    collector.add_body(
        f"# {source_name}\n\nStructured summary for item-0001.",
        source_name=source_name,
    )
    published_at = datetime(2026, 8, 27, 2, tzinfo=UTC)
    bundle = PublicationFinalizer().finalize(
        PublicationBriefingInput(
            category="papers",
            date="2026-08-27",
            title="papers briefing",
            generated_at=published_at,
            published_at=published_at,
            body=collector.body,
        ),
        collector.item_inputs(published_at=published_at),
    )
    return bundle


def _inputs_for(source_name, url, external_id=None):
    collector = PublicationRunCollector("papers")
    collector.add(_results_for(source_name, url, external_id))
    return collector.item_inputs(published_at=datetime(2026, 8, 27, 2, tzinfo=UTC))


def test_merge_appends_a_source_without_touching_existing_items():
    from publication.merge import merge_bundle

    bundle = _bundle_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
    original_ids = [item.id for item in bundle.items]

    merged = merge_bundle(
        bundle,
        _inputs_for("science", "https://www.science.org/doi/y", "10.1000/y"),
        "# science\n\nStructured summary for item-0001.",
        updated_at=datetime(2026, 8, 27, 3, tzinfo=UTC),
    )

    assert [item.id for item in merged.items][: len(original_ids)] == original_ids
    assert len(merged.items) == len(original_ids) + 1
    assert merged.briefing.id == bundle.briefing.id
    assert merged.briefing.body.startswith(bundle.briefing.body)
    assert "science" in merged.briefing.body
    assert merged.briefing.updated_at is not None
    # The merged bundle is a first-class bundle, not a patched one.
    validate_bundle(merged)
    assert bundle.briefing.updated_at is None


def test_merge_ignores_an_item_the_bundle_already_has():
    from publication.merge import merge_bundle

    bundle = _bundle_for("nature", "https://www.nature.com/articles/x", "10.1000/x")

    merged = merge_bundle(
        bundle,
        _inputs_for("nature", "https://www.nature.com/articles/x", "10.1000/x"),
        "# nature\n\nRe-fetched chunk.",
        updated_at=datetime(2026, 8, 27, 3, tzinfo=UTC),
    )

    assert [item.id for item in merged.items] == [item.id for item in bundle.items]
    validate_bundle(merged)


def _publish_nature():
    import run_pipelines as rp

    collector = PublicationRunCollector("papers")
    collector.add(_results_for("nature", "https://www.nature.com/articles/x", "10.1000/x"))
    collector.add_body("# nature\n\nnature chunk", source_name="nature")
    rp._finalize_category_publication("papers", collector)
    return rp


def test_a_resumed_source_is_appended_to_the_day_it_missed():
    """The recovery case: a source that failed joins the bundle it missed."""
    rp = _publish_nature()

    resumed = PublicationRunCollector("papers")
    resumed.add(_results_for("science", "https://www.science.org/doi/y", "10.1000/y"))
    resumed.add_body("# science\n\nscience chunk", source_name="science")
    rp._finalize_category_publication("papers", resumed)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert {item.source.name for item in bundle.items} == {"nature", "science"}
    assert "nature chunk" in bundle.briefing.body
    assert "science chunk" in bundle.briefing.body


def test_a_forced_single_source_rerun_keeps_the_other_sources():
    """`run -f <source>` must not replace the day's bundle with its one source."""
    import run_pipelines as rp

    first = PublicationRunCollector("papers")
    first.add(_results_for("nature", "https://www.nature.com/articles/x", "10.1000/x"))
    first.add(_results_for("science", "https://www.science.org/doi/y", "10.1000/y"))
    first.add_body("# nature\n\nnature chunk", source_name="nature")
    first.add_body("# science\n\nscience chunk", source_name="science")
    rp._finalize_category_publication("papers", first)

    rerun = PublicationRunCollector("papers")
    rerun.add(
        _results_for("nature", "https://www.nature.com/articles/x2", "10.1000/x2")
    )
    rerun.add_body("# nature\n\nAFTERNOON", source_name="nature")
    rp._finalize_category_publication("papers", rerun)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert {item.source.name for item in bundle.items} == {"nature", "science"}
    assert len(bundle.items) == 3
    # The source that did not re-run keeps both its item and its prose.
    assert "science chunk" in bundle.briefing.body
    # The original prose is not duplicated ...
    assert bundle.briefing.body.count("nature chunk") == 1
    # ... and the re-run's chunk is kept, because it renders an item the bundle
    # did not have.
    assert "AFTERNOON" in bundle.briefing.body


def test_new_items_of_a_covered_source_reach_the_body():
    """fetch() is seen-filtered, so a re-run's chunk holds only new items.

    Skipping it because the *source* was already in the bundle left those items
    in the item list with no prose anywhere.
    """
    rp = _publish_nature()

    afternoon = PublicationRunCollector("papers")
    afternoon.add(
        _results_for("nature", "https://www.nature.com/articles/x2", "10.1000/x2")
    )
    afternoon.add_body("# nature\n\nnature afternoon chunk", source_name="nature")
    rp._finalize_category_publication("papers", afternoon)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert len(bundle.items) == 2
    assert "nature afternoon chunk" in bundle.briefing.body


def test_a_full_rerun_replaces_the_bundle_without_duplicating_prose():
    rp = _publish_nature()

    full = PublicationRunCollector("papers")
    full.add(_results_for("nature", "https://www.nature.com/articles/x", "10.1000/x"))
    full.add(_results_for("science", "https://www.science.org/doi/y", "10.1000/y"))
    full.add_body("# nature\n\nnature chunk", source_name="nature")
    full.add_body("# science\n\nscience chunk", source_name="science")
    rp._finalize_category_publication("papers", full)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert {item.source.name for item in bundle.items} == {"nature", "science"}
    assert bundle.briefing.body.count("nature chunk") == 1
    assert bundle.briefing.body.count("science chunk") == 1


def _source(name, category, items=None, fetch_error=None):
    from types import SimpleNamespace

    def fetch():
        if fetch_error is not None:
            raise fetch_error
        return items or []

    seen: list = []
    return (
        SimpleNamespace(
            name=name,
            category=category,
            display_name=name,
            lookback_hours=24,
            fetch=fetch,
            get_batches=lambda values: [[value] for value in values],
            format_items=lambda values: "stub",
            commit_seen=lambda values: seen.extend(values),
        ),
        seen,
    )


def test_a_failed_item_is_not_marked_seen(monkeypatch):
    """Marking a failed item seen loses it for good: seen state never expires."""
    import run_pipelines as rp

    good = PipelineItem(
        "Good", "2026-08-27", "https://example.org/good", extra={"item_id": "good"}
    )
    bad = PipelineItem(
        "Bad", "2026-08-27", "https://example.org/bad", extra={"item_id": "bad"}
    )
    ds, seen = _source("nature", "papers", items=[good, bad])
    responses = iter([_response("item-0001"), "not json at all"])
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: next(responses))
    collector = PublicationRunCollector("papers")

    rp._process_regular_source_publication(
        ds,
        {},
        "stub/model",
        {"one_line_summary": "Summarize {article_list}"},
        "one_line_summary",
        collector,
    )
    rp._finalize_category_publication("papers", collector)

    assert [item.url for item in seen] == ["https://example.org/good"]


def test_a_fetch_failure_is_recorded_as_a_gap(monkeypatch):
    """A source that cannot be fetched is missing from the bundle; say so."""
    import run_pipelines as rp

    ds, _seen = _source("nature", "papers", fetch_error=RuntimeError("feed down"))
    collector = PublicationRunCollector("papers")
    monkeypatch.setattr(rp, "log", lambda *_args: None)

    rp._process_regular_source_publication(
        ds,
        {},
        "stub/model",
        {"one_line_summary": "Summarize {article_list}"},
        "one_line_summary",
        collector,
    )

    assert any("nature" in failure for failure in collector.failures)


def _raise(message):
    def boom():
        raise RuntimeError(message)

    return boom


def test_a_code_source_fetch_failure_is_recorded_as_a_gap(monkeypatch):
    import run_pipelines as rp
    from types import SimpleNamespace

    monkeypatch.setattr(rp, "log", lambda *_args: None)
    monkeypatch.setattr(rp.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {
                "sources": [
                    {
                        "name": "github_trending",
                        "category": "code",
                        "type": "scrape",
                        "url": "https://example.org",
                        "display_name": "GH",
                    }
                ]
            },
            {},
            {},
        ),
    )
    ds = SimpleNamespace(
        name="github_trending",
        display_name="GH",
        category="code",
        fetch=_raise("down"),
        lookback_hours=24,
    )
    monkeypatch.setattr(
        rp, "DataSource", SimpleNamespace(create=staticmethod(lambda *a, **kw: ds))
    )

    rp._run_pipeline_code_publication()

    assert any("github_trending" in gap for gap in rp.PUBLICATION_GAPS)


def test_a_resource_news_source_fetch_failure_is_recorded_as_a_gap(monkeypatch):
    import run_pipelines as rp
    from types import SimpleNamespace

    monkeypatch.setattr(rp, "log", lambda *_args: None)
    monkeypatch.setattr(
        rp,
        "_load_sources",
        lambda: (
            {
                "sources": [
                    {
                        "name": "dlut_zhxw",
                        "category": "resource",
                        "type": "scrape",
                        "url": "https://example.org",
                        "display_name": "DLUT",
                        "news_group": rp._DLUT_NEWS_GROUP,
                    }
                ]
            },
            {},
            {},
        ),
    )
    ds = SimpleNamespace(
        name="dlut_zhxw",
        display_name="DLUT",
        category="resource",
        fetch=_raise("down"),
        lookback_hours=24,
    )
    monkeypatch.setattr(
        rp, "DataSource", SimpleNamespace(create=staticmethod(lambda *a, **kw: ds))
    )

    rp._run_pipeline_resource_publication()

    assert any("dlut_zhxw" in gap for gap in rp.PUBLICATION_GAPS)


def test_a_deep_content_fetch_failure_does_not_kill_the_category():
    import run_pipelines as rp

    ds, _seen = _source("latent_space", "ai_news", fetch_error=RuntimeError("feed down"))
    collector = PublicationRunCollector("ai_news")

    rp._process_deep_content_source_publication(ds, {}, "stub/model", {}, collector)

    assert any("latent_space" in failure for failure in collector.failures)


def test_a_finalized_gap_reaches_the_exit_code(monkeypatch):
    """The seam: finalization records the gap, main turns it into a non-zero run."""
    import run_pipelines as rp

    logs: list[str] = []
    monkeypatch.setattr(rp, "log", logs.append)
    monkeypatch.setattr(rp, "_get_deepseek_key", lambda: "sk-test")
    monkeypatch.setattr(sys, "argv", ["run_pipelines.py", "--pipeline", "4"])

    def pipeline_with_one_failed_source():
        collector = PublicationRunCollector("code")
        collector.add(_results_for("github_trending", "https://github.com/o/r"))
        collector.add_body("# chunk", source_name="github_trending")
        collector.add_failure("huggingface: fetch failed")
        rp._finalize_category_publication("code", collector)
        return 1

    monkeypatch.setattr(rp, "run_pipeline_code", pipeline_with_one_failed_source)

    assert rp.main() == 1
    assert any("Canonical publication gaps" in line for line in logs)


def test_main_resets_gaps_between_runs(monkeypatch):
    """An agent runtime may call main() twice in one process."""
    import run_pipelines as rp

    rp.PUBLICATION_GAPS.append("stale gap from an earlier run")
    monkeypatch.setattr(rp, "log", lambda *_args: None)
    monkeypatch.setattr(rp, "_get_deepseek_key", lambda: "sk-test")
    monkeypatch.setattr(rp, "run_pipeline_code", lambda: 1)
    monkeypatch.setattr(sys, "argv", ["run_pipelines.py", "--pipeline", "4"])

    assert rp.main() == 0


def test_a_rerun_that_brings_new_items_keeps_the_older_ones():
    """fetch() is seen-filtered, so a re-run only returns what is new.

    Replacing the bundle with that subset dropped everything the morning run
    had already published.
    """
    rp = _publish_nature()

    afternoon = PublicationRunCollector("papers")
    afternoon.add(
        _results_for("nature", "https://www.nature.com/articles/x2", "10.1000/x2")
    )
    afternoon.add_body("# nature\n\nnature afternoon chunk", source_name="nature")
    rp._finalize_category_publication("papers", afternoon)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert len(bundle.items) == 2


def test_a_new_source_brings_its_prose_even_when_another_source_also_ran():
    rp = _publish_nature()

    run = PublicationRunCollector("papers")
    run.add(_results_for("nature", "https://www.nature.com/articles/x2", "10.1000/x2"))
    run.add(_results_for("science", "https://www.science.org/doi/y", "10.1000/y"))
    run.add_body("# nature\n\nnature new chunk", source_name="nature")
    run.add_body("# science\n\nscience chunk", source_name="science")
    rp._finalize_category_publication("papers", run)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    # The source the bundle never carried contributes its prose ...
    assert "science chunk" in bundle.briefing.body
    # ... the prose it did carry survives, exactly once ...
    assert bundle.briefing.body.count("nature chunk") == 1
    # ... and the new item of a carried source brings the prose it rendered.
    assert "nature new chunk" in bundle.briefing.body


def test_a_retry_after_a_failed_write_is_not_a_duplicate(monkeypatch):
    """The retry re-runs the same item; it must not be deduped against itself."""
    import run_pipelines as rp

    item = PipelineItem(
        "Late paper",
        "2026-08-27",
        "https://arxiv.org/abs/2608.99999",
        extra={"arxiv_id": "2608.99999"},
    )
    ds, _seen = _source("arxiv_cs_ai", "arxiv", items=[item])
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response("item-0001"))
    collector = PublicationRunCollector("arxiv")

    writes = {"n": 0}
    real_save = rp.save

    def flaky_save(category, filename, content):
        writes["n"] += 1
        if writes["n"] == 1:
            raise OSError("disk full")
        return real_save(category, filename, content)

    monkeypatch.setattr(rp, "save", flaky_save)

    rp._process_deep_content_source_publication(ds, {}, "stub/model", {}, collector)

    assert len(collector.results) == 1
    assert collector.dropped_duplicates == []
    assert collector.failures == []


def test_source_failure_publishes_the_successful_part():
    """A source-level failure must cost that source, not the whole category."""
    import run_pipelines as rp

    collector = _collector_with_one_source("code", "github_trending")
    collector.add_failure("huggingface: 1 item(s) lacked valid structured AI output")

    rp._finalize_category_publication("code", collector)

    bundle = PublicationStore().load_bundle(f"code-{rp.DATE}")
    assert len(bundle.items) == 1
    assert bundle.items[0].source.name == "github_trending"
    assert any("huggingface" in gap for gap in rp.PUBLICATION_GAPS)


def test_whole_category_failure_publishes_nothing_but_is_recorded():
    import run_pipelines as rp

    collector = PublicationRunCollector("code")
    collector.add_failure("github_trending: fetch failed")

    rp._finalize_category_publication("code", collector)

    with pytest.raises(FileNotFoundError):
        PublicationStore().load_bundle(f"code-{rp.DATE}")
    assert any("github_trending" in gap for gap in rp.PUBLICATION_GAPS)


def test_a_canonical_gap_makes_run_exit_nonzero(monkeypatch):
    import run_pipelines as rp

    logs: list[str] = []
    monkeypatch.setattr(rp, "log", logs.append)
    monkeypatch.setattr(rp, "_get_deepseek_key", lambda: "sk-test")
    monkeypatch.setattr(sys, "argv", ["run_pipelines.py", "--pipeline", "4"])

    # Control: the same stub without a gap exits 0, so a non-zero result below
    # can only come from the gap and not from the stub's return value.
    monkeypatch.setattr(rp, "run_pipeline_code", lambda: 1)
    assert rp.main() == 0

    def pipeline_with_a_gap():
        rp.PUBLICATION_GAPS.append("code: huggingface failed")
        return 1

    monkeypatch.setattr(rp, "run_pipeline_code", pipeline_with_a_gap)
    assert rp.main() == 1
    # The gap must be reported as a gap, not swallowed into a pipeline crash.
    assert any("Canonical publication gaps" in line for line in logs)
