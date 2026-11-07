"""Event detection for the AI briefing queue.

The functions here are ported from PR #79 (CylenLC, commit 8a733da).  The
tests pin the two behaviours the queue depends on: a changed review produces
an event, and a plain new submission does not flood the model unless the
source explicitly asks for it.
"""

from conference_events import (
    build_snapshot,
    detect_event_types,
    has_review_content,
    is_ai_event,
    snapshot_signature,
)

PAPER = {
    "forum_id": "forum-1",
    "note_id": "note-1",
    "title": "A hydrology foundation model",
    "abstract": "We train a model on streamflow.",
    "authors": ["A. Researcher"],
    "keywords": ["hydrology"],
    "venue": "ICLR 2027",
    "venue_id": "ICLR.cc/2027/Conference/Submission",
    "status": "under_review",
    "pdf": "https://openreview.net/pdf?id=note-1",
    "landing_url": "https://openreview.net/forum?id=forum-1",
    "cdate": 1_000,
    "mdate": 2_000,
}


def _review(reply_id="r1", rating="8", text="summary: good"):
    return {
        "reply_id": reply_id,
        "reply_type": "review",
        "rating": rating,
        "confidence": "4",
        "summary": text,
        "strengths": "novel",
        "weaknesses": "limited baselines",
        "questions": "how does it scale?",
        "decision": "",
        "reply_mdate": 2_100,
    }


def _summary(**overrides):
    value = {
        "review_count": 1,
        "rating_count": 1,
        "rating_mean": 0.8,
        "rating_median": 0.8,
        "rating_std": 0.0,
        "rating_min": 0.8,
        "rating_max": 0.8,
        "rating_range": 0.0,
        "confidence_mean": 4.0,
        "rating_raw_values": ["8"],
        "confidence_raw_values": ["4"],
        "decision": "",
        "presentation": "",
        "strong_signal": True,
        "controversial": False,
    }
    value.update(overrides)
    return value


def test_detect_event_types_reports_each_change():
    before = {
        "content_hash": "c1",
        "review_signature": "r1",
        "meta_review_signature": "",
        "author_response_signature": "",
        "status": "under_review",
        "decision": "",
        "decision_text": "",
        "presentation": "",
        "camera_ready": False,
    }
    after = {
        **before,
        "content_hash": "c2",
        "review_signature": "r2",
        "decision": "Accept",
        "decision_text": "comment: strong paper",
        "presentation": "oral",
        "camera_ready": True,
        "status": "accepted",
        "author_response_signature": "a2",
        "meta_review_signature": "m2",
    }

    assert detect_event_types(before, after) == [
        "PAPER_CONTENT_UPDATED",
        "REVIEWS_CHANGED",
        "DECISION_CHANGED",
        "PRESENTATION_CHANGED",
        "CAMERA_READY_CHANGED",
        "SUBMISSION_STATUS_CHANGED",
        "AUTHOR_RESPONSE_CHANGED",
        "META_REVIEW_CHANGED",
    ]


def test_detect_event_types_treats_first_observation_as_discovery():
    assert detect_event_types(None, {"content_hash": "c1"}) == ["PAPER_DISCOVERED"]


def test_build_snapshot_separates_reviews_meta_decisions_and_responses():
    rows = [
        _review(),
        {
            "reply_id": "m1",
            "reply_type": "meta_review",
            "summary": "metareview: solid",
            "reply_mdate": 2_200,
        },
        {
            "reply_id": "d1",
            "reply_type": "decision",
            "decision": "Accept (Oral)",
            "summary": "comment: accepted",
            "reply_mdate": 2_300,
        },
        {
            "reply_id": "a1",
            "reply_type": "rebuttal",
            "summary": "we added baselines",
            "reply_mdate": 2_400,
        },
    ]

    snapshot = build_snapshot(PAPER, rows, _summary())

    assert snapshot["paper"]["forum_url"].endswith("forum-1")
    assert snapshot["reviews"][0]["rating_raw"] == "8"
    assert snapshot["reviews"][0]["confidence_raw"] == "4"
    assert "strengths: novel" in snapshot["reviews"][0]["text"]
    assert snapshot["meta_reviews"] == [
        {"id": "m1", "text": "metareview: solid", "mdate": 2_200}
    ]
    assert snapshot["decision"] == "Accept (Oral)"
    assert snapshot["decision_text"] == "comment: accepted"
    assert snapshot["author_responses"] == [
        {"id": "a1", "text": "we added baselines", "mdate": 2_400}
    ]
    assert snapshot["raw_review_ratings"] == ["8"]
    assert snapshot["status"] == "accepted"
    assert snapshot["presentation"] == "oral"


def test_snapshot_signature_changes_when_a_review_changes():
    first = snapshot_signature(build_snapshot(PAPER, [_review(rating="6")], _summary()))
    second = snapshot_signature(
        build_snapshot(PAPER, [_review(rating="8")], _summary())
    )

    assert first["review_signature"] != second["review_signature"]
    assert first["fingerprint"] != second["fingerprint"]


def test_new_submission_without_reviews_is_not_an_ai_event_by_default():
    snapshot = build_snapshot(PAPER, [], None)
    signature = snapshot_signature(snapshot)
    event_types = detect_event_types(None, signature)

    assert has_review_content(snapshot) is False
    assert is_ai_event(event_types, snapshot, include_new_submissions=False) is False


def test_new_submission_with_reviews_is_still_an_ai_event():
    snapshot = build_snapshot(PAPER, [_review()], _summary())
    event_types = detect_event_types(None, snapshot_signature(snapshot))

    assert has_review_content(snapshot) is True
    assert is_ai_event(event_types, snapshot, include_new_submissions=False) is True


def test_review_change_event_is_always_ai_relevant():
    snapshot = build_snapshot(PAPER, [_review()], _summary())
    event_types = ["REVIEWS_CHANGED"]

    assert is_ai_event(event_types, snapshot, include_new_submissions=False) is True


def test_snapshot_caps_review_count_abstract_and_review_text():
    many_reviews = [_review(reply_id=f"r{index}") for index in range(15)]
    paper = dict(PAPER, abstract="x" * 10_000)

    snapshot = build_snapshot(
        paper,
        many_reviews,
        _summary(review_count=15),
        max_review_chars=50,
        max_abstract_chars=100,
        max_reviews_per_paper=10,
    )

    assert len(snapshot["reviews"]) == 10
    assert len(snapshot["paper"]["abstract"]) == 100
    assert all(len(review["text"]) <= 50 for review in snapshot["reviews"])


def test_new_review_beyond_the_prompt_cap_still_changes_the_signature():
    first_ten = [_review(reply_id=f"r{index}") for index in range(10)]
    eleven = first_ten + [_review(reply_id="r10")]

    before = snapshot_signature(
        build_snapshot(
            PAPER,
            first_ten,
            _summary(review_count=10),
            max_reviews_per_paper=0,
        )
    )
    after = snapshot_signature(
        build_snapshot(
            PAPER,
            eleven,
            _summary(review_count=11),
            max_reviews_per_paper=0,
        )
    )

    assert detect_event_types(before, after) == ["REVIEWS_CHANGED"]


def test_edit_beyond_the_review_text_limit_is_not_a_new_signature():
    first = _review()
    first["summary"] = "a" * 50
    second = _review()
    second["summary"] = "a" * 50 + "different tail"

    first_signature = snapshot_signature(
        build_snapshot(PAPER, [first], _summary(), max_review_chars=50)
    )
    second_signature = snapshot_signature(
        build_snapshot(PAPER, [second], _summary(), max_review_chars=50)
    )

    assert first_signature["fingerprint"] == second_signature["fingerprint"]


def test_plain_content_update_is_not_ai_relevant_by_default():
    snapshot = build_snapshot(PAPER, [], None)

    assert (
        is_ai_event(["PAPER_CONTENT_UPDATED"], snapshot, include_new_submissions=False)
        is False
    )
    assert (
        is_ai_event(["PAPER_CONTENT_UPDATED"], snapshot, include_new_submissions=True)
        is True
    )
