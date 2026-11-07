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
    forbidden = {"filters", "retrieval", "reviews", "figures", "prompt_template"}
    for source in _conference_sources():
        assert not forbidden & source.keys(), source["name"]


def test_conference_defaults_are_present():
    defaults = _config()["defaults"]["conference_defaults"]

    assert defaults["public_only"] is True
    assert defaults["page_size"] == 1000
    assert defaults["full_rescan_interval_days"] >= 1
    assert defaults["watermark_overlap_hours"] >= 0
