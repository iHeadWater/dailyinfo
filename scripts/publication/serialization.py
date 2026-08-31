"""Deterministic JSON-compatible serialization and semantic hashing."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from typing import Any, Dict, Mapping

from .models import Briefing, Item, PublicationBundle, SourceMetadata
from .validation import (
    normalize_datetime,
    validate_briefing,
    validate_bundle,
    validate_item,
)


def datetime_to_iso(value: datetime) -> str:
    """Serialize an aware timestamp as canonical UTC ISO-8601 with ``Z``."""

    normalized = normalize_datetime(value, "datetime")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source_to_dict(source: SourceMetadata) -> Dict[str, Any]:
    return {
        "name": source.name,
        "url": source.url,
        "external_id": source.external_id,
    }


def item_to_dict(item: Item) -> Dict[str, Any]:
    validate_item(item)
    return {
        "schema_version": item.schema_version,
        "id": item.id,
        "category": item.category,
        "title": item.title,
        "source": _source_to_dict(item.source),
        "authors": list(item.authors),
        "source_published_at": (
            datetime_to_iso(item.source_published_at)
            if item.source_published_at is not None
            else None
        ),
        "retrieved_at": datetime_to_iso(item.retrieved_at),
        "published_at": datetime_to_iso(item.published_at),
        "updated_at": (
            datetime_to_iso(item.updated_at) if item.updated_at is not None else None
        ),
        "summary": item.summary,
        "why_it_matters": item.why_it_matters,
        "tags": sorted(set(item.tags)),
        "language": item.language,
        "briefing_ids": sorted(item.briefing_ids),
    }


def briefing_to_dict(briefing: Briefing) -> Dict[str, Any]:
    validate_briefing(briefing)
    return {
        "schema_version": briefing.schema_version,
        "id": briefing.id,
        "category": briefing.category,
        "date": briefing.date.isoformat(),
        "title": briefing.title,
        "generated_at": datetime_to_iso(briefing.generated_at),
        "published_at": datetime_to_iso(briefing.published_at),
        "updated_at": (
            datetime_to_iso(briefing.updated_at)
            if briefing.updated_at is not None
            else None
        ),
        "item_ids": list(briefing.item_ids),
        "body": briefing.body,
    }


def bundle_to_dict(bundle: PublicationBundle) -> Dict[str, Any]:
    validate_bundle(bundle)
    # Item ordering is set-like at bundle level; briefing.item_ids preserves
    # editorial order separately.
    return {
        "schema_version": bundle.schema_version,
        "briefing": briefing_to_dict(bundle.briefing),
        "items": [
            item_to_dict(item) for item in sorted(bundle.items, key=lambda x: x.id)
        ],
    }


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return UTF-8-compatible, whitespace-independent canonical JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def serialize_item(item: Item) -> str:
    return canonical_json(item_to_dict(item))


def serialize_briefing(briefing: Briefing) -> str:
    return canonical_json(briefing_to_dict(briefing))


def serialize_bundle(bundle: PublicationBundle) -> str:
    return canonical_json(bundle_to_dict(bundle))


def _item_semantic_dict(item: Item) -> Dict[str, Any]:
    """Fields whose change is a publication-content change.

    Retrieval/lifecycle time and relationship membership are record metadata.
    Existing item membership is allowed to grow as an item appears in another
    daily briefing.
    """

    value = item_to_dict(item)
    for key in ("retrieved_at", "published_at", "updated_at", "briefing_ids"):
        value.pop(key)
    return value


def _briefing_semantic_dict(briefing: Briefing) -> Dict[str, Any]:
    value = briefing_to_dict(briefing)
    for key in ("generated_at", "published_at", "updated_at"):
        value.pop(key)
    return value


def item_content_hash(item: Item) -> str:
    payload = canonical_json(_item_semantic_dict(item)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def briefing_content_hash(briefing: Briefing) -> str:
    payload = canonical_json(_briefing_semantic_dict(briefing)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def bundle_content_hash(bundle: PublicationBundle) -> str:
    validate_bundle(bundle)
    payload = {
        "schema_version": bundle.schema_version,
        "briefing": _briefing_semantic_dict(bundle.briefing),
        "items": [
            _item_semantic_dict(item)
            for item in sorted(bundle.items, key=lambda x: x.id)
        ],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_datetime(value: Any, field_name: str):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    return normalize_datetime(value, field_name)


def item_from_dict(value: Mapping[str, Any]) -> Item:
    try:
        source = value["source"]
        item = Item(
            schema_version=value["schema_version"],
            id=value["id"],
            category=value["category"],
            title=value["title"],
            source=SourceMetadata(
                name=source["name"],
                url=source["url"],
                external_id=source.get("external_id"),
            ),
            authors=value["authors"],
            source_published_at=(
                _parse_datetime(value["source_published_at"], "source_published_at")
                if value["source_published_at"] is not None
                else None
            ),
            retrieved_at=_parse_datetime(value["retrieved_at"], "retrieved_at"),
            published_at=_parse_datetime(value["published_at"], "published_at"),
            updated_at=(
                _parse_datetime(value["updated_at"], "updated_at")
                if value.get("updated_at") is not None
                else None
            ),
            summary=value["summary"],
            why_it_matters=value.get("why_it_matters"),
            tags=value["tags"],
            language=value["language"],
            briefing_ids=value["briefing_ids"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid serialized Item: {exc}") from exc
    return validate_item(item)


def briefing_from_dict(value: Mapping[str, Any]) -> Briefing:
    try:
        briefing = Briefing(
            schema_version=value["schema_version"],
            id=value["id"],
            category=value["category"],
            date=date.fromisoformat(value["date"]),
            title=value["title"],
            generated_at=_parse_datetime(value["generated_at"], "generated_at"),
            published_at=_parse_datetime(value["published_at"], "published_at"),
            updated_at=(
                _parse_datetime(value["updated_at"], "updated_at")
                if value.get("updated_at") is not None
                else None
            ),
            item_ids=value["item_ids"],
            body=value["body"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid serialized Briefing: {exc}") from exc
    return validate_briefing(briefing)


def bundle_from_dict(value: Mapping[str, Any]) -> PublicationBundle:
    try:
        bundle = PublicationBundle(
            schema_version=value["schema_version"],
            briefing=briefing_from_dict(value["briefing"]),
            items=[item_from_dict(item) for item in value["items"]],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid serialized PublicationBundle: {exc}") from exc
    return validate_bundle(bundle)


def deserialize_bundle(payload: str) -> PublicationBundle:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid publication JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("serialized publication must be a JSON object")
    return bundle_from_dict(value)
