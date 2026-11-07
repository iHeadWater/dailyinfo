"""Conference change events for the optional AI briefing queue.

``build_snapshot``, ``detect_event_types`` and the event names come from
CylenLC's PR #79 (commit 8a733da, ``scripts/conference.py``).  The adaptation
is deliberately narrow: the PR stored an event with a full before/after
snapshot, while this module builds the snapshot from the current
``papers``/``reviews``/``paper_review_summary`` rows and stores only the event
type and fingerprint.  The prompt layer can therefore render the current
state without keeping a second copy of every abstract and review in SQLite.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from typing import Any

from conference_reviews import _numeric_prefix, _presentation_from, normalize_rating

NEW_SUBMISSION = "PAPER_DISCOVERED"
AI_REVIEW_EVENT_TYPES = frozenset(
    {
        "REVIEWS_CHANGED",
        "DECISION_CHANGED",
        "DECISION_SUMMARY_CHANGED",
        "PRESENTATION_CHANGED",
        "AUTHOR_RESPONSE_CHANGED",
        "META_REVIEW_CHANGED",
        "SUBMISSION_STATUS_CHANGED",
    }
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value]
        return list(parsed) if isinstance(parsed, list) else [parsed]
    return [value]


def _review_text_from_row(row: dict, limit: int) -> str:
    """Rebuild the PR's ``_review_text`` from a stored review row."""

    parts: list[str] = []
    for key in (
        "summary",
        "strengths",
        "weaknesses",
        "questions",
        "soundness",
        "presentation",
        "informativeness",
    ):
        value = row.get(key)
        if value not in (None, ""):
            parts.append(f"{key}: {value}")
    return "\n".join(parts)[:limit]


def _paper_view(paper: dict, max_abstract_chars: int) -> dict:
    forum_id = str(paper.get("forum_id") or paper.get("note_id") or "")
    pdf = str(paper.get("pdf") or "").strip()
    if pdf.startswith("/"):
        pdf = f"https://openreview.net{pdf}"
    landing = str(paper.get("landing_url") or "").strip()
    return {
        "note_id": str(paper.get("note_id") or paper.get("id") or ""),
        "forum_id": forum_id,
        "number": paper.get("number"),
        "title": str(paper.get("title") or ""),
        "abstract": str(paper.get("abstract") or "")[:max_abstract_chars],
        "authors": [str(author) for author in _as_list(paper.get("authors"))],
        "affiliations": [
            str(affiliation) for affiliation in _as_list(paper.get("affiliations"))
        ],
        "keywords": [str(keyword) for keyword in _as_list(paper.get("keywords"))],
        "venue": str(paper.get("venue") or ""),
        "venue_id": str(paper.get("venue_id") or paper.get("venueid") or ""),
        "pdf": pdf,
        "landing_url": landing,
        "forum_url": landing or f"https://openreview.net/forum?id={forum_id}",
        "code_url": str(paper.get("code_url") or ""),
        "cdate": int(paper.get("cdate") or 0),
        "mdate": int(paper.get("mdate") or 0),
    }


def _metrics_from_rows(
    reviews: list[dict],
    review_cfg: dict | None,
    summary: dict | None,
) -> dict:
    if summary:
        metrics: dict[str, Any] = {
            "review_count": int(summary.get("review_count") or 0),
            "rating_count": int(summary.get("rating_count") or 0),
            "confidence_mean": summary.get("confidence_mean"),
            "rating_raw_values": _as_list(summary.get("rating_raw_values")),
            "confidence_raw_values": _as_list(summary.get("confidence_raw_values")),
            "strong_signal": bool(summary.get("strong_signal")),
            "controversial": bool(summary.get("controversial")),
        }
        for key in (
            "rating_mean",
            "rating_median",
            "rating_std",
            "rating_min",
            "rating_max",
            "rating_range",
        ):
            if summary.get(key) is not None:
                metrics[key] = summary[key]
        return metrics

    review_cfg = review_cfg or {}
    rating_options = review_cfg.get("rating_options", [])
    normalized = [
        value
        for value in (
            normalize_rating(review.get("rating_raw"), rating_options)
            for review in reviews
        )
        if value is not None
    ]
    confidence = [
        value
        for value in (
            _numeric_prefix(review.get("confidence_raw")) for review in reviews
        )
        if value is not None
    ]
    metrics = {
        "review_count": len(reviews),
        "rating_count": len(normalized),
        "confidence_mean": statistics.fmean(confidence) if confidence else None,
        "rating_raw_values": [
            review["rating_raw"]
            for review in reviews
            if review.get("rating_raw") not in (None, "")
        ],
        "confidence_raw_values": [
            review["confidence_raw"]
            for review in reviews
            if review.get("confidence_raw") not in (None, "")
        ],
        "strong_signal": False,
        "controversial": False,
    }
    if normalized:
        metrics.update(
            rating_mean=statistics.fmean(normalized),
            rating_median=statistics.median(normalized),
            rating_std=statistics.pstdev(normalized),
            rating_min=min(normalized),
            rating_max=max(normalized),
            rating_range=max(normalized) - min(normalized),
        )
        min_reviews = int(review_cfg.get("min_reviews_for_signal", 2))
        strong_threshold = float(review_cfg.get("strong_threshold", 0.75))
        controversy_min = int(review_cfg.get("controversy_min_reviews", 3))
        std_threshold = float(review_cfg.get("controversy_std_threshold", 0.20))
        range_threshold = float(review_cfg.get("controversy_range_threshold", 0.40))
        metrics["strong_signal"] = (
            len(normalized) >= min_reviews
            and metrics["rating_mean"] >= strong_threshold
        )
        metrics["controversial"] = len(normalized) >= controversy_min and (
            metrics["rating_std"] >= std_threshold
            or metrics["rating_range"] >= range_threshold
        )
    return metrics


def build_snapshot(
    paper: dict,
    review_rows: list[dict],
    summary: dict | None,
    *,
    review_cfg: dict | None = None,
    max_review_chars: int = 6000,
    max_abstract_chars: int = 4000,
    max_reviews_per_paper: int = 10,
) -> dict:
    """Build the PR-shaped snapshot used by the briefing prompt."""

    review_cfg = review_cfg or {}
    rating_options = review_cfg.get("rating_options", [])
    reviews: list[dict] = []
    meta_reviews: list[dict] = []
    decisions: list[dict] = []
    author_responses: list[dict] = []

    for row in review_rows:
        kind = str(row.get("reply_type") or "other")
        reply_id = str(row.get("reply_id") or row.get("id") or "")
        mdate = int(row.get("reply_mdate") or row.get("mdate") or 0)
        if kind == "review":
            rating = row.get("rating")
            confidence = row.get("confidence")
            reviews.append(
                {
                    "id": reply_id,
                    "rating_raw": rating,
                    "rating_numeric": _numeric_prefix(rating),
                    "rating_normalized": normalize_rating(rating, rating_options),
                    "confidence_raw": confidence,
                    "confidence_numeric": _numeric_prefix(confidence),
                    "text": _review_text_from_row(row, max_review_chars),
                    "mdate": mdate,
                }
            )
        elif kind == "meta_review":
            meta_reviews.append(
                {
                    "id": reply_id,
                    "text": str(row.get("summary") or "")[:max_review_chars],
                    "mdate": mdate,
                }
            )
        elif kind == "decision":
            decisions.append(
                {
                    "id": reply_id,
                    "value": str(row.get("decision") or ""),
                    "text": str(row.get("summary") or "")[:max_review_chars],
                    "mdate": mdate,
                }
            )
        elif kind == "rebuttal":
            author_responses.append(
                {
                    "id": reply_id,
                    "text": str(row.get("summary") or "")[:max_review_chars],
                    "mdate": mdate,
                }
            )

    reviews.sort(key=lambda item: item["id"])
    meta_reviews.sort(key=lambda item: item["id"])
    author_responses.sort(key=lambda item: (item["mdate"], item["id"]))
    decisions.sort(key=lambda item: (item["mdate"], item["id"]))

    # A paper can theoretically accumulate many replies.  The cap bounds the
    # prompt even when an author posts a long sequence of rebuttals; the
    # latest author responses are the ones the briefing should mention.
    limit = int(max_reviews_per_paper)
    if limit > 0:
        # Keep the latest replies: a new review is usually the highest id and
        # the reason the event exists.  Zero means "no cap" and is used for
        # signature building, where missing review 11 would hide a change.
        reviews = reviews[-limit:]
        meta_reviews = meta_reviews[-limit:]
        author_responses = author_responses[-limit:]
        decisions = decisions[-limit:]

    decision = str((summary or {}).get("decision") or "") or (
        decisions[-1]["value"] if decisions else ""
    )
    decision_text = decisions[-1]["text"] if decisions else ""
    presentation = str((summary or {}).get("presentation") or "") or _presentation_from(
        decision, paper.get("venue", "")
    )

    status = str(paper.get("status") or "unknown")
    if decision:
        folded = decision.casefold()
        if "reject" in folded:
            status = "rejected"
        elif "accept" in folded:
            status = "accepted"

    return {
        "paper": _paper_view(paper, max_abstract_chars),
        "status": status,
        "decision": decision,
        "decision_text": decision_text,
        "presentation": presentation,
        "camera_ready": bool(paper.get("camera_ready", False)),
        "reviews": reviews,
        "meta_reviews": meta_reviews,
        "author_responses": author_responses,
        "review_metrics": _metrics_from_rows(reviews, review_cfg, summary),
        "raw_review_ratings": [
            review["rating_raw"]
            for review in reviews
            if review.get("rating_raw") not in (None, "")
        ],
    }


def snapshot_signature(snapshot: dict) -> dict:
    """Return the compact signature compared by ``detect_event_types``."""

    paper = snapshot.get("paper") or {}
    content_fingerprint = {
        key: paper.get(key)
        for key in (
            "title",
            "abstract",
            "authors",
            "affiliations",
            "keywords",
            "pdf",
            "landing_url",
            "note_id",
            "mdate",
        )
    }
    if paper.get("code_url"):
        content_fingerprint["code_url"] = paper["code_url"]
    content_hash = _hash(content_fingerprint)
    review_signature = _hash(
        [
            {
                "id": review.get("id", ""),
                "rating": review.get("rating_raw"),
                "confidence": review.get("confidence_raw"),
                "text_hash": _hash(review.get("text", "")),
            }
            for review in snapshot.get("reviews", [])
        ]
    )
    meta_reviews = snapshot.get("meta_reviews", [])
    author_responses = snapshot.get("author_responses", [])
    meta_review_signature = _hash(meta_reviews) if meta_reviews else ""
    author_response_signature = _hash(author_responses) if author_responses else ""
    signature = {
        "content_hash": content_hash,
        "review_signature": review_signature,
        "meta_review_signature": meta_review_signature,
        "author_response_signature": author_response_signature,
        "status": snapshot.get("status", "unknown"),
        "decision": snapshot.get("decision", ""),
        "decision_text": snapshot.get("decision_text", ""),
        "presentation": snapshot.get("presentation", ""),
        "camera_ready": bool(snapshot.get("camera_ready", False)),
    }
    signature["fingerprint"] = _hash(
        {
            "content_hash": content_hash,
            "review_signature": review_signature,
            "meta_review_signature": meta_review_signature,
            "author_response_signature": author_response_signature,
            "status": signature["status"],
            "decision": signature["decision"],
            "decision_text": signature["decision_text"],
            "presentation": signature["presentation"],
            "camera_ready": signature["camera_ready"],
        }
    )
    return signature


def detect_event_types(before: dict | None, after: dict) -> list[str]:
    """Compare two signatures; ported verbatim from PR #79."""

    if before is None:
        return [NEW_SUBMISSION]
    events = []
    if before.get("content_hash") != after.get("content_hash"):
        events.append("PAPER_CONTENT_UPDATED")
    if before.get("review_signature") != after.get("review_signature"):
        events.append("REVIEWS_CHANGED")
    if before.get("decision") != after.get("decision"):
        events.append("DECISION_CHANGED")
    elif (before.get("decision_text") or "") != (after.get("decision_text") or ""):
        events.append("DECISION_SUMMARY_CHANGED")
    if before.get("presentation") != after.get("presentation"):
        events.append("PRESENTATION_CHANGED")
    if before.get("camera_ready") != after.get("camera_ready"):
        events.append("CAMERA_READY_CHANGED")
    if before.get("status") != after.get("status"):
        events.append("SUBMISSION_STATUS_CHANGED")
    if before.get("author_response_signature") != after.get(
        "author_response_signature"
    ):
        events.append("AUTHOR_RESPONSE_CHANGED")
    if (before.get("meta_review_signature") or "") != (
        after.get("meta_review_signature") or ""
    ):
        events.append("META_REVIEW_CHANGED")
    return events


def has_review_content(snapshot: dict) -> bool:
    """Whether the snapshot contains anything that warrants an AI briefing."""

    return bool(
        snapshot.get("reviews")
        or snapshot.get("meta_reviews")
        or snapshot.get("author_responses")
        or snapshot.get("decision")
    )


def is_ai_event(
    event_types: list[str],
    snapshot: dict,
    *,
    include_new_submissions: bool,
) -> bool:
    if not event_types:
        return False
    if any(event_type in AI_REVIEW_EVENT_TYPES for event_type in event_types):
        return True
    if has_review_content(snapshot):
        return True
    if include_new_submissions and (
        NEW_SUBMISSION in event_types or "PAPER_CONTENT_UPDATED" in event_types
    ):
        return True
    return False


def build_event(
    *,
    source: str,
    forum_id: str,
    event_types: list[str],
    signature: dict,
    now_ms: int,
) -> dict:
    event_id = _hash(
        {
            "source": source,
            "forum_id": forum_id,
            "types": event_types,
            "after": signature.get("fingerprint", ""),
        }
    )
    return {
        "event_id": event_id,
        "source": source,
        "forum_id": forum_id,
        "event_types": event_types,
        "fingerprint": signature.get("fingerprint", ""),
        "created_ms": now_ms,
    }
