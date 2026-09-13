"""Adapters from heterogeneous pipeline results to structured publication input.

The current pipeline's ``datasource.Item`` is intentionally not itself the
canonical publication model.  This adapter reads structured fields only; it
never parses the final Markdown briefing to recover item metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, List, Mapping, Optional, Union


DateLike = Union[date, datetime, str]
_UNSET = object()


@dataclass
class PublicationItemInput:
    """Structured item data required by :class:`PublicationFinalizer`."""

    source_name: str
    source_url: str
    source_published_at: Optional[DateLike]
    title: str
    summary: str
    authors: List[str]
    tags: List[str]
    language: str
    retrieved_at: datetime
    published_at: datetime
    why_it_matters: Optional[str] = None
    updated_at: Optional[datetime] = None
    external_id: Optional[str] = None
    explicit_id: Optional[str] = None


@dataclass
class PublicationBriefingInput:
    """Structured briefing data; ``body`` is the existing Markdown body."""

    category: str
    date: DateLike
    title: str
    generated_at: datetime
    published_at: datetime
    body: str
    updated_at: Optional[datetime] = None


def _read(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _extra(raw: Any) -> Mapping[str, Any]:
    value = _read(raw, "extra", {})
    return value if isinstance(value, Mapping) else {}


class StructuredPublicationAdapter:
    """Shared adapter for pipeline items with explicit structured metadata."""

    @staticmethod
    def item_from_pipeline(
        raw: Any,
        *,
        source_name: str,
        retrieved_at: datetime,
        published_at: datetime,
        language: Optional[str] = None,
        source_url: Optional[str] = None,
        summary: Any = _UNSET,
        why_it_matters: Any = _UNSET,
        tags: Any = _UNSET,
        title: Any = _UNSET,
    ) -> PublicationItemInput:
        """Adapt a current ``datasource.Item`` or mapping.

        ``content`` is deliberately ignored as an item summary.  A pipeline
        must provide ``summary`` explicitly (top-level or in ``extra``), so a
        missing structured summary fails during finalization instead of being
        guessed from generated Markdown or raw article content.
        """

        extra = _extra(raw)

        def value(key: str, default: Any = None) -> Any:
            raw_value = _read(raw, key, _UNSET)
            if raw_value is not _UNSET and raw_value is not None:
                return raw_value
            return extra.get(key, default)

        external_id = value("external_id")
        if not external_id:
            for key in (
                "arxiv_id",
                "repo_id",
                "guid",
                "openreview_id",
                "doi",
                "full_name",
                "item_id",
                "name",
            ):
                if extra.get(key):
                    external_id = extra[key]
                    break

        return PublicationItemInput(
            source_name=source_name,
            source_url=source_url or value("url", ""),
            source_published_at=value("source_published_at"),
            title=value("title", "") if title is _UNSET else title,
            summary=value("summary", "") if summary is _UNSET else summary,
            why_it_matters=(
                value("why_it_matters") if why_it_matters is _UNSET else why_it_matters
            ),
            authors=value("authors", extra.get("authors", [])),
            tags=value("tags", extra.get("tags", [])) if tags is _UNSET else tags,
            language=language or value("language", extra.get("content_language", "")),
            retrieved_at=retrieved_at,
            published_at=published_at,
            updated_at=value("updated_at"),
            external_id=external_id,
            explicit_id=value("id"),
        )

    @staticmethod
    def briefing(
        *,
        category: str,
        date_value: DateLike,
        title: str,
        body: str,
        generated_at: datetime,
        published_at: datetime,
        updated_at: Optional[datetime] = None,
    ) -> PublicationBriefingInput:
        return PublicationBriefingInput(
            category=category,
            date=date_value,
            title=title,
            generated_at=generated_at,
            published_at=published_at,
            updated_at=updated_at,
            body=body,
        )

    @staticmethod
    def items(
        raw_items: Iterable[Any],
        *,
        source_name: str,
        retrieved_at: datetime,
        published_at: datetime,
        language: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> List[PublicationItemInput]:
        return [
            StructuredPublicationAdapter.item_from_pipeline(
                raw,
                source_name=source_name,
                retrieved_at=retrieved_at,
                published_at=published_at,
                language=language,
                source_url=source_url,
            )
            for raw in raw_items
        ]
