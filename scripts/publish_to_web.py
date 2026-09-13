#!/usr/bin/env python3
"""Publish canonical DailyInfo briefings to the configured Web checkout."""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from paths import WORKSPACE_ROOT
from publication import (
    CANONICAL_CATEGORIES,
    DeliveryCoordinator,
    DeliveryStateStore,
    PublicationStore,
    WebPublisher,
    sanitize_error,
)


CONTENT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _today() -> str:
    return datetime.now(CONTENT_TIMEZONE).date().isoformat()


def _parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD: {value!r}") from exc


def _parse_categories(value: Optional[Iterable[str]]) -> list[str]:
    if value is None:
        return list(CANONICAL_CATEGORIES)
    result = []
    for category in value:
        for candidate in category.split(","):
            candidate = candidate.strip()
            if not candidate:
                continue
            if candidate not in CANONICAL_CATEGORIES:
                raise ValueError(f"unsupported Publication v1 category: {candidate}")
            if candidate not in result:
                result.append(candidate)
    return result


def main(
    date_value: Optional[str] = None,
    categories: Optional[Iterable[str]] = None,
    *,
    force: bool = False,
    publication_store: Optional[PublicationStore] = None,
    delivery_store: Optional[DeliveryStateStore] = None,
    publisher: Optional[WebPublisher] = None,
) -> int:
    """Publish existing canonical briefings; never trigger a pipeline run."""

    try:
        target_date = _parse_date(date_value or _today())
        selected_categories = _parse_categories(categories)
        publication_store = publication_store or PublicationStore()
        delivery_store = delivery_store or DeliveryStateStore()
        briefings = publication_store.list_briefings(
            date_value=target_date, categories=selected_categories
        )
        if not briefings:
            print(
                f"No canonical publications to publish for {target_date} "
                f"under {WORKSPACE_ROOT}"
            )
            return 0
        publisher = publisher or WebPublisher(publication_store=publication_store)
        coordinator = DeliveryCoordinator(delivery_store)
    except Exception as exc:
        print(f"Web publication setup failed: {sanitize_error(exc)}")
        return 1

    failures = 0
    for briefing in briefings:
        try:
            bundle = publication_store.load_bundle(briefing.id)
            result = coordinator.publish(bundle, publisher, force=force)
        except Exception as exc:
            failures += 1
            print(f"Web publication failed for {briefing.id}: {sanitize_error(exc)}")
            continue
        if result.status == "failed":
            failures += 1
            detail = f": {result.error}" if result.error else ""
            print(f"Web publication failed for {briefing.id}{detail}")
        elif result.status == "skipped":
            print(f"Web publication skipped for {briefing.id}")
        else:
            ref = f" commit={result.external_ref}" if result.external_ref else ""
            print(f"Web publication succeeded for {briefing.id}{ref}")
    return 1 if failures else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="Briefing date (YYYY-MM-DD).")
    parser.add_argument(
        "--categories",
        default=None,
        help="Comma-separated Publication v1 categories; defaults to all five.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reconcile the Web target even when web delivery is already successful.",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    raise SystemExit(
        main(
            args.date,
            [args.categories] if args.categories else None,
            force=args.force,
        )
    )
