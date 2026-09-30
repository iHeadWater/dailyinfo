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
    new_body_chunks: Iterable[str],
    *,
    updated_at: Optional[datetime] = None,
    dropped: Optional[list[str]] = None,
) -> PublicationBundle:
    """Fold a partly-covering run into an existing bundle.

    A run that covers only part of a category -- a forced single-source re-run
    after that source failed -- must not replace the day's bundle, or every
    other source disappears from both delivery sinks.  So items are always
    unioned by identity, never replaced: seen-filtering means a re-run only
    returns items that are new to it, and taking that subset as the bundle
    would drop everything the earlier run published.

    ``new_body_chunks`` are the Markdown chunks the run rendered from items the
    bundle does not yet carry; the caller decides which those are (each chunk
    records the identities it was rendered from).  A chunk the body already contains is
    skipped, so re-rendering a source cannot stack its prose on top of itself.
    Identities dropped as duplicates are appended to ``dropped`` when given, so
    the caller can report them the same way an in-run duplicate is reported.
    """

    if isinstance(new_body_chunks, str):
        # A str is iterable, and a bare string would be appended one character
        # at a time.
        new_body_chunks = [new_body_chunks]

    known_ids = {item.id for item in existing.items}
    item_inputs = [_input_from_item(item) for item in existing.items]
    for item_input in new_inputs:
        try:
            identity = resolve_item_input_identity(item_input)
        except PublicationValidationError:
            # Keep it: finalization rejects a malformed item, and that check
            # belongs there rather than here.
            item_inputs.append(item_input)
            continue
        if identity.item_id in known_ids:
            if dropped is not None:
                dropped.append(f"{identity.source_name}: {identity.item_id}")
            continue
        known_ids.add(identity.item_id)
        item_inputs.append(item_input)

    body = existing.briefing.body
    for new_body_chunk in new_body_chunks:
        chunk = new_body_chunk.strip()
        if chunk and chunk not in body:
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
