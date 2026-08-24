"""Configuration coverage for the OpenReview conference registry."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_openreview_registry_covers_requested_conferences():
    config = json.loads(
        (REPO_ROOT / "config" / "sources.json").read_text(encoding="utf-8")
    )
    sources = {
        source["name"]: source
        for source in config["sources"]
        if source.get("provider") == "openreview"
    }

    expected = {
        "openreview_iclr_2026": "ICLR.cc/2026/Conference",
        "openreview_icml_2026": "ICML.cc/2026/Conference",
        "openreview_neurips_2026": "NeurIPS.cc/2026/Conference",
        "openreview_aaai_2026": "AAAI.org/2026/Conference",
        "openreview_kdd_2026": "KDD.org/2026/Research_Track_August",
        "openreview_kdd_2026_cycle2": "KDD.org/2026/Research_Track_Cycle_2",
        "openreview_cvpr_2026": "thecvf.com/CVPR/2026/Conference",
        "openreview_acl_2026": "aclweb.org/ACL/2026/Conference",
        "openreview_emnlp_2026": "EMNLP/2026/Conference",
        "openreview_iccv_2025": "thecvf.com/ICCV/2025/Conference",
        "openreview_naacl_2025": "aclweb.org/NAACL/2025/Conference",
    }
    assert {name: sources[name]["venue_id"] for name in expected} == expected
    assert all(
        sources[name]["enabled"]
        for name in expected
        if name in {
            "openreview_iclr_2026",
            "openreview_icml_2026",
            "openreview_neurips_2026",
        }
    )
    assert all(
        not sources[name]["enabled"]
        for name in expected
        if name not in {
            "openreview_iclr_2026",
            "openreview_icml_2026",
            "openreview_neurips_2026",
        }
    )
    assert all(
        sources[name]["url"].startswith("https://openreview.net/")
        for name in expected
    )


def test_acl_and_cvf_are_canonical_for_public_proceedings():
    config = json.loads(
        (REPO_ROOT / "config" / "sources.json").read_text(encoding="utf-8")
    )
    sources = {source["name"]: source for source in config["sources"]}
    assert sources["acl_anthology_acl_2026"]["provider"] == "acl"
    assert sources["acl_anthology_acl_2026"]["url"].startswith(
        "https://aclanthology.org/"
    )
    assert sources["cvf_cvpr_2026"]["provider"] == "cvf"
    assert sources["cvf_cvpr_2026"]["url"].startswith(
        "https://openaccess.thecvf.com/"
    )
    for name in (
        "openreview_cvpr_2026",
        "openreview_acl_2026",
        "openreview_emnlp_2026",
        "openreview_iccv_2025",
        "openreview_naacl_2025",
    ):
        assert sources[name]["enabled"] is False


def test_conference_source_inherits_shared_profile():
    import run_pipelines

    source = {
        "name": "openreview_example",
        "provider": "openreview",
        "retrieval": {"threshold": 0.6},
    }
    resolved = run_pipelines._resolve_conference_source(
        source,
        {
            "conference_defaults": {
                "public_only": True,
                "retrieval": {"backend": "llama_cpp", "threshold": 0.45},
                "reviews": {"enabled": True},
            }
        },
    )

    assert resolved["public_only"] is True
    assert resolved["retrieval"] == {"backend": "llama_cpp", "threshold": 0.6}
    assert resolved["reviews"] == {"enabled": True}
    assert source == {
        "name": "openreview_example",
        "provider": "openreview",
        "retrieval": {"threshold": 0.6},
    }
