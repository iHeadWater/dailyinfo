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


def _mark_delivered(briefing_id: str) -> None:
    from publication import DeliveryState, DeliveryStateStore

    store = DeliveryStateStore()
    for sink in ("discord", "web"):
        store.save(
            DeliveryState(
                schema_version=1,
                briefing_id=briefing_id,
                sink=sink,
                status="success",
                attempt_count=1,
                first_attempted_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
                last_attempted_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
                delivered_at=datetime(2026, 8, 27, 1, tzinfo=UTC),
            )
        )


def test_a_refetched_published_item_is_not_rendered_again():
    """Seeding drops items the day already carries before they are rendered.

    Without it a chunk that mixes an already-published item with a new one
    brings the old item's prose along, and the merge appends the whole chunk.
    """
    rp = _publish_nature()
    published = rp._published_item_ids("papers")
    assert published

    collector = PublicationRunCollector("papers", known_item_ids=published)
    kept = collector.take_new(
        _results_for("nature", "https://www.nature.com/articles/x", "10.1000/x")
        + _results_for("nature", "https://www.nature.com/articles/x2", "10.1000/x2")
    )

    assert [result.raw_item.url for result in kept] == [
        "https://www.nature.com/articles/x2"
    ]


def test_a_notice_does_not_overwrite_a_real_briefing(tmp_path, monkeypatch):
    """A briefing may legitimately mention ⚠️; that must not mark it a notice."""
    import run_pipelines as rp

    monkeypatch.setattr(rp, "BRIEFINGS_DIR", tmp_path / "briefings")
    target = rp.BRIEFINGS_DIR / "papers" / f"nature_briefing_{rp.DATE}.md"
    target.parent.mkdir(parents=True)
    real = "# Nature\n\n一条提到 ⚠️ 撤稿提醒的正常简报。\n" + "内容" * 60
    target.write_text(real, encoding="utf-8")

    rp._save_placeholder(
        "papers", target.name, f"# Nature - {rp.DATE}\n\n⚠️ 获取失败\n"
    )

    assert target.read_text(encoding="utf-8") == real


def test_a_notice_replaces_an_older_notice(tmp_path, monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "BRIEFINGS_DIR", tmp_path / "briefings")
    target = rp.BRIEFINGS_DIR / "papers" / f"nature_briefing_{rp.DATE}.md"
    target.parent.mkdir(parents=True)
    target.write_text(f"# Nature - {rp.DATE}\n\n⚠️ 获取失败\n", encoding="utf-8")

    assert rp._save_placeholder(
        "papers", target.name, f"# Nature - {rp.DATE}\n\n📭 过去 24 小时无新内容\n"
    )
    assert "📭 过去" in target.read_text(encoding="utf-8")


def test_an_unopenable_freshrss_db_is_recorded_as_a_gap(monkeypatch):
    import run_pipelines as rp

    monkeypatch.setattr(rp, "log", lambda *_args: None)
    monkeypatch.setattr(rp, "_load_sources", lambda: ({"sources": []}, {}, {}))
    monkeypatch.setattr(rp.sqlite3, "connect", _raise("database is locked"))
    collector = PublicationRunCollector("papers")

    rp._run_category_pipeline("papers", collector=collector)

    assert any("FreshRSS" in failure for failure in collector.failures)


def test_a_merged_briefing_is_delivered_again():
    """A merge changes the content, so "already delivered" no longer holds.

    Both sinks key that on identity alone and would skip -- so a source
    recovered with `run -f <source>` or `run --force all` reached neither of
    them, while both reported the day as delivered.
    """
    rp = _publish_nature()
    briefing_id = f"papers-{rp.DATE}"
    _mark_delivered(briefing_id)

    resumed = PublicationRunCollector("papers")
    resumed.add(_results_for("science", "https://www.science.org/doi/y", "10.1000/y"))
    resumed.add_body("# science\n\nscience chunk", source_name="science")
    rp._finalize_category_publication("papers", resumed)

    from publication import DeliveryStateStore

    store = DeliveryStateStore()
    assert store.load(briefing_id, "discord") is None
    assert store.load(briefing_id, "web") is None


def test_an_unchanged_briefing_stays_delivered():
    """Re-publishing identical content must not cause a second delivery."""
    rp = _publish_nature()
    briefing_id = f"papers-{rp.DATE}"
    _mark_delivered(briefing_id)

    same = PublicationRunCollector("papers")
    same.add(_results_for("nature", "https://www.nature.com/articles/x", "10.1000/x"))
    same.add_body("# nature\n\nnature chunk", source_name="nature")
    rp._finalize_category_publication("papers", same)

    from publication import DeliveryStateStore

    assert DeliveryStateStore().load(briefing_id, "discord") is not None


def test_the_store_lock_excludes_a_second_writer():
    """A resume can overlap a cron run; two merges from one base lose one."""
    import threading

    import run_pipelines as rp

    entered = threading.Event()
    acquired = threading.Event()

    def second_writer():
        entered.set()
        with rp._store_lock():
            acquired.set()

    with rp._store_lock():
        thread = threading.Thread(target=second_writer)
        thread.start()
        assert entered.wait(timeout=5)
        # Exclusion means it cannot get in while the first writer holds it.
        assert not acquired.wait(timeout=0.5)
    thread.join(timeout=5)
    assert acquired.is_set()


def test_force_bypasses_the_low_frequency_skip(tmp_path, monkeypatch):
    """A resumed low-frequency source used to be skipped by its own archive.

    Four configured sources have a lookback above 24h, and yesterday's archive
    sits inside that window -- so `resume` reported success while fetching
    nothing.
    """
    import run_pipelines as rp

    monkeypatch.setattr(rp, "PUSHED_DIR", tmp_path / "pushed")
    archive = rp.PUSHED_DIR / "papers"
    archive.mkdir(parents=True)
    (archive / f"skxjz_briefing_{rp.DATE}.md").write_text("archived", encoding="utf-8")

    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = set()
    assert rp._already_pushed_within("skxjz", "papers", 48) is True

    rp.FORCE_SOURCES = {"skxjz"}
    assert rp._already_pushed_within("skxjz", "papers", 48) is False


def test_a_write_failure_leaves_that_source_fetchable(monkeypatch):
    """A failed write must not mark the item seen: seen state never expires."""
    import run_pipelines as rp

    bad = PipelineItem(
        "Bad write", "2026-08-27", "https://example.org/bad", extra={"item_id": "bad"}
    )
    good = PipelineItem(
        "Good write",
        "2026-08-27",
        "https://example.org/good",
        extra={"item_id": "good"},
    )
    ds_bad, seen_bad = _source("nature", "papers", items=[bad])
    ds_good, seen_good = _source("science", "papers", items=[good])
    monkeypatch.setattr(rp, "call_ai", lambda prompt, **_kwargs: _response("item-0001"))

    real_save = rp.save

    def flaky_save(category, filename, content):
        if "nature" in filename:
            raise OSError("disk full")
        return real_save(category, filename, content)

    monkeypatch.setattr(rp, "save", flaky_save)

    collector = PublicationRunCollector("papers")
    templates = {"one_line_summary": "Summarize {article_list}"}
    rp._process_regular_source_publication(
        ds_bad, {}, "stub/model", templates, "one_line_summary", collector
    )
    rp._process_regular_source_publication(
        ds_good, {}, "stub/model", templates, "one_line_summary", collector
    )
    rp._finalize_category_publication("papers", collector)

    assert seen_bad == []
    assert [item.url for item in seen_good] == ["https://example.org/good"]
    assert any("nature" in failure for failure in collector.failures)


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
