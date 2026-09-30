"""Unified publication boundary: partial sources, dedup, and resume."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sys

import pytest

from datasource import Item as PipelineItem
from publication import PublicationRunCollector, PublicationStore
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
    collector.add_body(f"# {source_name}\n\n{results[0].summary}")
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
    collector.add_body("# nature\n\nStructured summary for item-0001.")

    rp._finalize_category_publication("papers", collector)

    bundle = PublicationStore().load_bundle(f"papers-{rp.DATE}")
    assert len(bundle.items) == 1
    assert len(bundle.briefing.item_ids) == 1
    # The drop is reported, not silent.
    assert any(
        "duplicate" in line and "nature" in line for line in logs
    ), logs


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
