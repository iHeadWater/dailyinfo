"""Review extraction and metrics ported from PR #79.

The functions in this module are deliberately close to the originals in
CylenLC's ``conference.py`` (commit 8a733da): ``normalize_rating``,
``reply_kind``, ``_review_text``, ``_response_text``, ``_decision_text`` and
``_presentation_from``.  The adaptation is limited to:

* taking already-normalized reply dictionaries from
  :mod:`openreview_provider` instead of raw ``Note`` objects;
* filtering every reply through the fail-closed ``reply_is_public`` check;
* returning rows for the ``reviews`` table plus one summary per paper.

No model is called here and no relevance decision is made.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from typing import Any

from openreview_provider import (
    VenueCapabilities,
    content_value,
    invitation_matches,
    public_content,
    reply_is_public,
)

KIND_TO_STORED = {
    "official_review": "review",
    "author_response": "rebuttal",
    "meta_review": "meta_review",
    "decision": "decision",
    "other": "other",
}


def normalize_rating(value: Any, ordered_options: list | tuple) -> float | None:
    """Map a rating to its position in a venue-declared ordered scale."""

    if not ordered_options or len(ordered_options) < 2:
        return None
    raw = str(value).strip()
    for index, option in enumerate(ordered_options):
        if raw == str(option).strip():
            return index / (len(ordered_options) - 1)
    numeric = _numeric_prefix(value)
    option_numbers = [_numeric_prefix(option) for option in ordered_options]
    if numeric is not None and numeric in option_numbers:
        return option_numbers.index(numeric) / (len(option_numbers) - 1)
    return None


def _numeric_prefix(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(value or ""))
    return float(match.group(1)) if match else None


def reply_kind(reply: dict, capabilities: VenueCapabilities, config: dict) -> str:
    overrides = config.get("reply_suffixes", {})
    kinds = (
        (
            "official_review",
            overrides.get(
                "official_review", [capabilities.review_name, "Official_Review"]
            ),
        ),
        (
            "meta_review",
            overrides.get(
                "meta_review", [capabilities.meta_review_name, "Meta_Review"]
            ),
        ),
        (
            "decision",
            overrides.get("decision", [capabilities.decision_name, "Decision"]),
        ),
        (
            "author_response",
            overrides.get(
                "author_response",
                [capabilities.rebuttal_name, "Rebuttal", "Author_Response"],
            ),
        ),
    )
    return next(
        (kind for kind, suffixes in kinds if invitation_matches(reply, suffixes)),
        "other",
    )


def _review_text(content: dict) -> str:
    parts: list[str] = []
    preferred = (
        "summary",
        "strengths",
        "weaknesses",
        "questions",
        "review",
        "comment",
        "soundness",
        "presentation",
        "contribution",
        "limitations",
        "metareview",
    )
    for key in preferred:
        value = content_value(content, key, "")
        if value:
            parts.append(f"{key}: {value}")
    excluded = {"rating", "recommendation", "confidence"}
    for key in content:
        if key in preferred or key in excluded:
            continue
        value = content_value(content, key, "")
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            parts.append(f"{key}: {value}")
    return "\n".join(parts)[:6000]


def _response_text(content: dict) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    preferred = (
        "rebuttal",
        "author_response",
        "response",
        "comment",
        "text",
        "summary",
    )
    for key in preferred:
        value = content_value(content, key, "")
        if value not in (None, ""):
            text = str(value).strip()
            if text and text not in seen:
                parts.append(f"{key}: {text}")
                seen.add(text)
    for key in content:
        if key in preferred:
            continue
        value = content_value(content, key, "")
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            if text and text not in seen:
                parts.append(f"{key}: {text}")
                seen.add(text)
    return "\n".join(parts)[:6000]


def _decision_text(content: dict) -> str:
    parts: list[str] = []
    preferred = ("comment", "justification", "metareview", "summary")
    for key in preferred:
        value = content_value(content, key, "")
        if value not in (None, ""):
            parts.append(f"{key}: {value}")
    for key in content:
        if key in preferred or key in {"decision", "recommendation"}:
            continue
        value = content_value(content, key, "")
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            parts.append(f"{key}: {value}")
    return "\n".join(parts)[:6000]


def _presentation_from(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).casefold()
    if re.search(r"\bspotlight\b", text):
        return "spotlight"
    if re.search(r"\boral\b", text):
        return "oral"
    if re.search(r"\bposter\b", text):
        return "poster"
    return ""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(reply: dict) -> str:
    return hashlib.sha256(_json(reply).encode("utf-8")).hexdigest()


def _record(
    *,
    reply: dict,
    kind: str,
    content: dict,
    capabilities: VenueCapabilities,
) -> dict:
    invitations = reply.get("invitations") or []
    record = {
        "reply_id": str(reply.get("reply_id") or reply.get("id") or ""),
        "forum_id": str(reply.get("forum_id") or ""),
        "invitation": str(invitations[0]) if invitations else "",
        "reply_type": KIND_TO_STORED.get(kind, "other"),
        "rating": "",
        "confidence": "",
        "soundness": "",
        "presentation": "",
        "informativeness": "",
        "summary": "",
        "strengths": "",
        "weaknesses": "",
        "questions": "",
        "decision": "",
        "raw_json": _json(reply),
        "content_hash": _content_hash(reply),
        "reply_cdate": int(reply.get("cdate") or 0),
        "reply_mdate": int(reply.get("mdate") or 0),
    }
    if kind == "official_review":
        record.update(
            rating=str(content_value(content, "rating", "") or ""),
            confidence=str(content_value(content, "confidence", "") or ""),
            soundness=str(content_value(content, "soundness", "") or ""),
            presentation=str(content_value(content, "presentation", "") or ""),
            informativeness=str(content_value(content, "informativeness", "") or ""),
            summary=str(
                content_value(content, "paper_summary", "")
                or content_value(content, "summary", "")
                or ""
            ),
            strengths=str(
                content_value(content, "critical_strengths", "")
                or content_value(content, "strengths", "")
                or ""
            ),
            weaknesses=str(
                content_value(content, "critical_weaknesses", "")
                or content_value(content, "weaknesses", "")
                or ""
            ),
            questions=str(
                content_value(content, "questions_for_authors", "")
                or content_value(content, "questions", "")
                or ""
            ),
        )
    elif kind == "decision":
        decision = content_value(
            content, capabilities.decision_field_name, ""
        ) or content_value(content, "recommendation", "")
        record["decision"] = str(decision or "")
        record["summary"] = _decision_text(content)
    elif kind == "author_response":
        record["summary"] = _response_text(content)
    else:
        record["summary"] = _review_text(content)
    return record


def compute_review_metrics(reviews: list[dict], review_cfg: dict) -> dict:
    """Port of the PR's review metrics, including raw values and signals."""

    rating_options = review_cfg.get("rating_options", [])
    normalized = [
        value
        for value in (
            normalize_rating(review.get("rating"), rating_options) for review in reviews
        )
        if value is not None
    ]
    confidence = [
        value
        for value in (_numeric_prefix(review.get("confidence")) for review in reviews)
        if value is not None
    ]
    raw_ratings = [
        review.get("rating")
        for review in reviews
        if review.get("rating") not in (None, "")
    ]
    raw_confidences = [
        review.get("confidence")
        for review in reviews
        if review.get("confidence") not in (None, "")
    ]
    metrics: dict[str, Any] = {
        "review_count": len(reviews),
        "rating_count": len(normalized),
        "confidence_mean": statistics.fmean(confidence) if confidence else None,
        "rating_raw_values": raw_ratings,
        "confidence_raw_values": raw_confidences,
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
        and metrics.get("rating_mean", -math.inf) >= strong_threshold
    )
    metrics["controversial"] = len(normalized) >= controversy_min and (
        metrics.get("rating_std", 0) >= std_threshold
        or metrics.get("rating_range", 0) >= range_threshold
    )
    return metrics


def build_page_review_data(
    replies: list[dict],
    capabilities: VenueCapabilities,
    config: dict,
) -> tuple[list[dict], list[dict]]:
    """Return ``(review_rows, paper_summaries)`` for one page of replies."""

    review_cfg = config.get("reviews", {})
    records: list[dict] = []
    for reply in replies:
        if not reply_is_public(reply):
            continue
        # Re-apply field-level filtering here as well: an injected client may
        # hand us an already-normalized reply whose content was not filtered.
        sanitized = dict(reply)
        sanitized["content"] = public_content(reply, enforce=True)
        kind = reply_kind(sanitized, capabilities, config)
        content = sanitized.get("content", {}) or {}
        record = _record(
            reply=sanitized,
            kind=kind,
            content=content,
            capabilities=capabilities,
        )
        if record["reply_id"] and record["forum_id"]:
            records.append(record)

    summaries: list[dict] = []
    forums = sorted({record["forum_id"] for record in records})
    for forum_id in forums:
        forum_records = [r for r in records if r["forum_id"] == forum_id]
        reviews = [r for r in forum_records if r["reply_type"] == "review"]
        decisions = sorted(
            (r for r in forum_records if r["reply_type"] == "decision"),
            key=lambda r: (r["reply_mdate"], r["reply_id"]),
        )
        decision = decisions[-1]["decision"] if decisions else ""
        presentation = _presentation_from(decision)
        summary = compute_review_metrics(reviews, review_cfg)
        summary.update(
            forum_id=forum_id,
            decision=decision,
            presentation=presentation,
        )
        summaries.append(summary)
    return records, summaries
