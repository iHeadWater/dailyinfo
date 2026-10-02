"""Construction of validated canonical publications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .adapters import PublicationBriefingInput, PublicationItemInput
from .identity import (
    briefing_id,
    canonicalize_source_url,
    normalize_external_id,
    resolve_item_id,
)
from .models import (
    Briefing,
    Item,
    PublicationBundle,
    PublicationValidationError,
    SCHEMA_VERSION,
    SourceMetadata,
)
from .validation import (
    normalize_date,
    normalize_datetime,
    normalize_optional_datetime,
    normalize_text_list,
    validate_bundle,
    validate_category,
    validate_id,
    validate_public_source_url,
)


@dataclass(frozen=True)
class ResolvedItemIdentity:
    """The identity fields one item input resolves to, normalized."""

    item_id: str
    source_name: str
    source_url: str
    external_id: Optional[str]


def resolve_item_input_identity(
    item_input: PublicationItemInput,
) -> ResolvedItemIdentity:
    """Resolve and validate one item input's canonical identity.

    The finalizer and the pipeline collector both call this: the collector
    drops an item whose identity is already in the category before it can reach
    ``validate_bundle``, which rejects a duplicate identity by rejecting the
    whole bundle.  Sharing the resolver is what keeps that pre-filter and the
    finalizer's own id in agreement.
    """

    source_url = canonicalize_source_url(
        validate_public_source_url(item_input.source_url)
    )
    source_name = (
        item_input.source_name.strip()
        if isinstance(item_input.source_name, str)
        else item_input.source_name
    )
    if not isinstance(source_name, str) or not source_name:
        raise PublicationValidationError("Item.source.name must be non-empty")
    if item_input.external_id is not None and not isinstance(
        item_input.external_id, str
    ):
        raise PublicationValidationError("Item.source.external_id must be text")
    external_id = normalize_external_id(
        source_name=source_name,
        source_url=source_url,
        external_id=(
            item_input.external_id.strip() if item_input.external_id else None
        ),
    )
    item_id = resolve_item_id(
        source_name=source_name,
        source_url=source_url,
        external_id=external_id,
        explicit_id=item_input.explicit_id,
    )
    validate_id(item_id, "Item.id")
    return ResolvedItemIdentity(
        item_id=item_id,
        source_name=source_name,
        source_url=source_url,
        external_id=external_id,
    )


class PublicationFinalizer:
    """Normalize structured pipeline results into a PublicationBundle.

    No clock is consulted here.  Runtime timestamps are required in the
    adapter input so the same semantic input always produces the same
    canonical representation.  The configured business timezone is used only
    when a source supplies a date without a time.
    """

    def __init__(self, business_timezone: str = "Asia/Shanghai") -> None:
        self.business_timezone = ZoneInfo(business_timezone)

    def finalize(
        self,
        briefing_input: PublicationBriefingInput,
        item_inputs: Iterable[PublicationItemInput],
    ) -> PublicationBundle:
        category = validate_category(briefing_input.category)
        briefing_date = normalize_date(briefing_input.date)
        generated_at = normalize_datetime(briefing_input.generated_at, "generated_at")
        published_at = normalize_datetime(briefing_input.published_at, "published_at")
        updated_at = normalize_optional_datetime(
            briefing_input.updated_at, "updated_at"
        )

        canonical_briefing = Briefing(
            schema_version=SCHEMA_VERSION,
            id=briefing_id(category, briefing_date.isoformat()),
            category=category,
            date=briefing_date,
            title=(
                briefing_input.title.strip()
                if isinstance(briefing_input.title, str)
                else briefing_input.title
            ),
            generated_at=generated_at,
            published_at=published_at,
            updated_at=updated_at,
            item_ids=[],
            body=briefing_input.body,
        )

        items = []
        for item_input in item_inputs:
            identity = resolve_item_input_identity(item_input)
            item_source_url = identity.source_url
            source_name = identity.source_name
            external_id = identity.external_id
            item_id = identity.item_id
            item = Item(
                schema_version=SCHEMA_VERSION,
                id=item_id,
                category=category,
                title=(
                    item_input.title.strip()
                    if isinstance(item_input.title, str)
                    else item_input.title
                ),
                source=SourceMetadata(
                    name=source_name,
                    url=item_source_url,
                    external_id=external_id,
                ),
                authors=normalize_text_list(item_input.authors, "authors"),
                source_published_at=normalize_optional_datetime(
                    item_input.source_published_at,
                    "source_published_at",
                    date_timezone=self.business_timezone,
                ),
                retrieved_at=normalize_datetime(
                    item_input.retrieved_at, "retrieved_at"
                ),
                published_at=normalize_datetime(
                    item_input.published_at, "published_at"
                ),
                updated_at=normalize_optional_datetime(
                    item_input.updated_at, "updated_at"
                ),
                summary=(
                    item_input.summary.strip()
                    if isinstance(item_input.summary, str)
                    else item_input.summary
                ),
                why_it_matters=(
                    item_input.why_it_matters.strip()
                    if isinstance(item_input.why_it_matters, str)
                    and item_input.why_it_matters.strip()
                    else None
                ),
                tags=normalize_text_list(item_input.tags, "tags", unique=True),
                language=(
                    item_input.language.strip()
                    if isinstance(item_input.language, str)
                    else item_input.language
                ),
                briefing_ids=[canonical_briefing.id],
            )
            items.append(item)
            canonical_briefing.item_ids.append(item.id)

        bundle = PublicationBundle(
            schema_version=SCHEMA_VERSION,
            briefing=canonical_briefing,
            items=items,
        )
        validate_bundle(bundle)
        return bundle
