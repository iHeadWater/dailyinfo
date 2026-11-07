import json
from pathlib import Path

import pytest

RATING_OPTIONS = [
    "0: strong reject",
    "2: reject",
    "4: below threshold",
    "6: marginally above threshold",
    "8: accept",
    "10: strong accept",
]
FIXTURE = Path(__file__).parent / "fixtures" / "openreview" / "iclr_public_forum.json"


def _capabilities():
    from openreview_provider import VenueCapabilities

    return VenueCapabilities(
        venue_id="ICLR.cc/2026/Conference",
        submission_invitation="ICLR.cc/2026/Conference/-/Submission",
        submission_venue_id="ICLR.cc/2026/Conference/Submission",
        review_name="Official_Review",
        meta_review_name="Meta_Review",
        decision_name="Decision",
        rebuttal_name="Rebuttal",
        decision_field_name="decision",
    )


def _fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for reply in data["replies"]:
        reply.setdefault("readers", ["everyone"])
    return data


def _config(**reviews):
    base = {
        "rating_options": RATING_OPTIONS,
        "min_reviews_for_signal": 1,
    }
    base.update(reviews)
    return {"reviews": base}


def test_normalize_rating_requires_known_ordered_scale():
    from conference_reviews import normalize_rating

    assert normalize_rating("8: accept", RATING_OPTIONS) == 0.8
    assert normalize_rating(6, RATING_OPTIONS) == 0.6
    assert normalize_rating("8: accept", []) is None
    assert normalize_rating("unknown", RATING_OPTIONS) is None


def test_reply_kind_classifies_review_meta_decision_rebuttal():
    from conference_reviews import reply_kind

    data = _fixture()
    capabilities = _capabilities()
    kinds = [reply_kind(reply, capabilities, {}) for reply in data["replies"]]

    assert kinds == ["official_review", "author_response", "decision"]


def test_build_page_review_data_extracts_review_fields_and_summary():
    from conference_reviews import build_page_review_data

    data = _fixture()
    rows, summaries = build_page_review_data(
        data["replies"], _capabilities(), _config()
    )

    assert len(rows) == 3
    review = next(row for row in rows if row["reply_id"] == "review-1")
    assert review["reply_type"] == "review"
    assert review["rating"] == "8: accept"
    assert review["confidence"] == "4: confident"
    assert review["summary"] == "A useful cross-basin streamflow forecasting model."
    assert review["strengths"] == "Strong hydrological evaluation."
    assert review["weaknesses"] == "Limited extreme-flood analysis."
    assert review["reply_cdate"] == 1751000000000

    summary = summaries[0]
    assert summary["forum_id"] == "forum-hydro-1"
    assert summary["review_count"] == 1
    assert summary["rating_mean"] == 0.8
    assert summary["rating_raw_values"] == ["8: accept"]
    assert summary["strong_signal"] is True
    assert summary["decision"] == "Accept (Poster)"
    assert summary["presentation"] == "poster"


def test_private_reply_is_not_stored():
    from conference_reviews import build_page_review_data

    data = _fixture()
    data["replies"][0]["readers"] = ["ICLR.cc/2026/Conference/Submission42/Authors"]
    rows, summaries = build_page_review_data(
        data["replies"], _capabilities(), _config()
    )

    assert "review-1" not in {row["reply_id"] for row in rows}
    assert summaries[0]["review_count"] == 0


def test_missing_readers_fails_closed():
    from conference_reviews import build_page_review_data

    data = _fixture()
    for reply in data["replies"]:
        reply.pop("readers", None)
    rows, summaries = build_page_review_data(
        data["replies"], _capabilities(), _config()
    )

    assert rows == []
    assert summaries == []


def test_compute_review_metrics_uses_iclr_2027_scale():
    from conference_reviews import compute_review_metrics

    reviews = [
        {"rating": "4", "confidence": "3"},
        {"rating": "3", "confidence": "2"},
        {"rating": "2", "confidence": "1"},
    ]
    metrics = compute_review_metrics(
        reviews,
        {
            "rating_options": ["1", "2", "3", "4"],
            "min_reviews_for_signal": 3,
            "controversy_min_reviews": 3,
        },
    )

    assert metrics["review_count"] == 3
    assert metrics["rating_raw_values"] == ["4", "3", "2"]
    assert metrics["rating_mean"] == pytest.approx((1.0 + 2 / 3 + 1 / 3) / 3)
    assert metrics["strong_signal"] is False
    assert metrics["controversial"] is True


def test_private_field_is_stripped_from_raw_json():
    from conference_reviews import build_page_review_data

    data = _fixture()
    data["replies"][0]["content"]["private_note"] = {
        "value": "hidden reviewer identity",
        "readers": ["ICLR.cc/2026/Conference/Reviewers"],
    }
    rows, _summaries = build_page_review_data(
        data["replies"], _capabilities(), _config()
    )

    review = next(row for row in rows if row["reply_id"] == "review-1")
    assert "hidden reviewer identity" not in review["raw_json"]
    assert "private_note" not in review["raw_json"]


@pytest.mark.parametrize("field", ["recommendation", "overall_recommendation"])
def test_recommendation_field_is_used_when_rating_is_absent(field):
    from conference_reviews import build_page_review_data

    data = _fixture()
    review = data["replies"][0]
    review["content"].pop("rating")
    review["content"][field] = {"value": "8: accept"}

    rows, summaries = build_page_review_data(
        data["replies"], _capabilities(), _config()
    )

    stored = next(row for row in rows if row["reply_id"] == "review-1")
    assert stored["rating"] == "8: accept"
    assert summaries[0]["rating_mean"] == 0.8
