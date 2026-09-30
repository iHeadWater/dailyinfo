"""Append a resumed source to an already-finalized publication."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .adapters import PublicationBriefingInput, PublicationItemInput
from .finalizer import PublicationFinalizer, resolve_item_input_identity
from .models import Item, PublicationBundle, PublicationValidationError


def _input_from_item(item: Item) -> PublicationItemInput:
    """Rebuild a finalizer input for an item that is already in a bundle.

    ``explicit_id`` carries the item's own id, so re-finalizing cannot move an
    existing item to a different identity.
    """

    return PublicationItemInput(
        source_name=item.source.name,
        source_url=item.source.url,
        source_published_at=item.source_published_at,
        title=item.title,
        summary=item.summary,
        authors=list(item.authors),
        tags=list(item.tags),
        language=item.language,
        retrieved_at=item.retrieved_at,
        published_at=item.published_at,
        why_it_matters=item.why_it_matters,
        updated_at=item.updated_at,
        external_id=item.source.external_id,
        explicit_id=item.id,
    )


def merge_bundle(
    existing: PublicationBundle,
    new_inputs: Iterable[PublicationItemInput],
    new_body_chunk: str,
    *,
    updated_at: Optional[datetime] = None,
) -> PublicationBundle:
    """Append one resumed source to an existing bundle.

    A run that covers only part of a category -- a forced single-source re-run
    after that source failed -- must not replace the day's bundle, or every
    other source disappears from both delivery sinks.  ``new_inputs`` are the
    items that run produced and ``new_body_chunk`` is the Markdown it rendered;
    items the bundle already carries are ignored, so re-running a source that
    partly succeeded adds only what is new.

    The result is a freshly finalized bundle: same identity, more items, and a
    body that ends with the appended chunk.  The appended chunk keeps its own
    rendering, which is what lets a resumed source reach Discord on its own
    (see ``scripts/resume_publication.py``).
    """

    known_ids = {item.id for item in existing.items}
    item_inputs = [_input_from_item(item) for item in existing.items]
    for item_input in new_inputs:
        try:
            item_id = resolve_item_input_identity(item_input).item_id
        except PublicationValidationError:
            # Keep it: finalization rejects a malformed item, and that check
            # belongs there rather than here.
            item_inputs.append(item_input)
            continue
        if item_id in known_ids:
            continue
        known_ids.add(item_id)
        item_inputs.append(item_input)

    body = existing.briefing.body
    chunk = new_body_chunk.strip()
    if chunk:
        body = f"{body}\n\n{chunk}"

    return PublicationFinalizer().finalize(
        PublicationBriefingInput(
            category=existing.briefing.category,
            date=existing.briefing.date,
            title=existing.briefing.title,
            generated_at=existing.briefing.generated_at,
            published_at=existing.briefing.published_at,
            updated_at=updated_at,
            body=body,
        ),
        item_inputs,
    )
