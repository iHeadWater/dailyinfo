"""The conference slice is deliberately narrow; config is the contract."""

import json
from pathlib import Path

SOURCES_JSON = Path(__file__).resolve().parents[1] / "config" / "sources.json"


def _config():
    return json.loads(SOURCES_JSON.read_text(encoding="utf-8"))


def _conference_sources():
    return [
        source
        for source in _config()["sources"]
        if source.get("category") == "conference"
    ]


def test_only_the_three_openreview_venues_are_configured():
    sources = _conference_sources()

    assert [source["name"] for source in sources] == [
        "openreview_iclr_2027",
        "openreview_icml_2027",
        "openreview_neurips_2026",
    ]
    assert [source["venue_id"] for source in sources] == [
        "ICLR.cc/2027/Conference",
        "ICML.cc/2027/Conference",
        "NeurIPS.cc/2026/Conference",
    ]
    assert all(source["type"] == "api" for source in sources)
    assert all(source["provider"] == "openreview" for source in sources)
    assert all(source.get("enabled", True) for source in sources)


def test_conference_config_does_not_carry_the_deferred_features():
    forbidden = {"filters", "retrieval", "figures", "prompt_template"}
    for source in _conference_sources():
        assert not forbidden & source.keys(), source["name"]


def test_conference_defaults_are_present():
    defaults = _config()["defaults"]["conference_defaults"]

    assert defaults["public_only"] is True
    assert defaults["page_size"] == 1000
    assert defaults["full_rescan_interval_days"] >= 1
    assert defaults["watermark_overlap_hours"] >= 0
    assert defaults["max_pages_per_run"] > 0
    reviews = defaults["reviews"]
    assert reviews["enabled"] is True
    assert reviews["sample_limit"] == 50
    assert reviews["page_size"] <= 1000
    assert reviews["request_interval_seconds"] > 0
    assert reviews["max_pages_per_run"] > 0


def test_each_enabled_conference_source_has_a_rating_scale():
    for source in _conference_sources():
        options = source.get("reviews", {}).get("rating_options", [])
        assert len(options) >= 2, source["name"]


def test_ai_briefing_defaults_are_bounded():
    defaults = _config()["defaults"]["conference_defaults"]["ai_briefing"]

    assert defaults["enabled"] is True
    assert defaults["include_new_submissions"] is False
    assert 1 <= defaults["max_events_per_briefing"] <= 20
    assert 1 <= defaults["max_batches_per_run"] <= 20
    assert defaults["max_tokens"] > 0
    assert defaults["max_review_chars"] > 0
    assert defaults["max_abstract_chars"] > 0
    assert 1 <= defaults["max_reviews_per_paper"] <= 50
    assert defaults["max_attempts"] >= 1
    assert defaults["seed_limit"] >= 1
