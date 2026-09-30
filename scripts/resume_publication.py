#!/usr/bin/env python3
"""Re-run one source and fold it into the day's canonical briefing.

This is the recovery path for a gap: a source failed, the rest of its category
published, and the day is missing that source.  The source is re-run on its own
-- every other source is skipped, so no AI is spent on them -- the result is
merged into the existing bundle, and only the recovered chunk is posted to
Discord.  The Web sink re-renders from the bundle.

Only categories whose sources run individually are supported (papers, ai_news,
arxiv).  `code` and `resource` run as whole-category functions, so there is no
single source to resume; use `dailyinfo run --force all` for those.

Today only: the pipelines write to today's date, so a past day cannot be
resumed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys

import run_pipelines as rp
from paths import CURRENT_ENV, get_channel_id
from publication import (
    DELIVERY_SCHEMA_VERSION,
    DeliveryState,
    DeliveryStateStore,
    PublicationRunCollector,
    PublicationStore,
)
from push_to_discord import send_to_discord

RERUNNABLE_CATEGORIES = ("papers", "ai_news", "arxiv")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNSUPPORTED = 2


def log(msg: str) -> None:
    """输出日志（附带当前环境标记）"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [env:{CURRENT_ENV}] {msg}", flush=True)


def _bundle_item_ids(briefing_id: str) -> set[str] | None:
    """The identities today's briefing carries, or None when it has no briefing."""
    try:
        bundle = PublicationStore().load_bundle(briefing_id)
    except FileNotFoundError:
        return None
    return {item.id for item in bundle.items}


def _known_sources(category: str) -> list[str]:
    cfg, _defaults, _templates = rp._load_sources()
    return [
        source["name"]
        for source in cfg.get("sources", [])
        if source.get("category") == category
        and source.get("enabled", True) is not False
        # Only these are dispatched per source; a source without a type is
        # "known" but never runs, which would look like a successful no-op.
        and source.get("type") in ("rss", "scrape", "api")
    ]


def main(category: str, source: str) -> int:
    if category not in RERUNNABLE_CATEGORIES:
        log(
            f"{category} runs as one category-wide function, so a single source "
            "cannot be resumed; use `dailyinfo run --force all` instead."
        )
        return EXIT_UNSUPPORTED

    known = _known_sources(category)
    if source not in known:
        # A typo would otherwise force nothing, collect nothing, and report
        # success.
        log(
            f"{category} has no enabled source named {source!r}; "
            f"known sources: {', '.join(known) or '(none)'}"
        )
        return EXIT_FAILED

    briefing_id = f"{category}-{rp.DATE}"
    try:
        before_ids = _bundle_item_ids(briefing_id)
    except Exception as exc:
        log(f"Cannot read {briefing_id}: {exc}")
        return EXIT_FAILED
    if before_ids is None:
        log(f"No canonical briefing {briefing_id}: run `dailyinfo run` first.")
        return EXIT_FAILED

    # A collector means publication mode, but the flag has to be set too: the
    # legacy path ignores the collector and marks the fetched items seen, which
    # loses exactly what this command exists to recover.
    rp.PUBLICATION_INTEGRATION = True
    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = {source}
    collector = PublicationRunCollector(category, known_item_ids=before_ids)
    try:
        rp._run_category_pipeline(
            category,
            create_marker=(category == "arxiv"),
            deep_content=(category == "ai_news"),
            collector=collector,
            only_source=source,
        )
    except Exception as exc:
        log(f"Re-running {source} failed: {exc}")
        return EXIT_FAILED

    # What the merge actually kept, not what the run rendered: a chunk whose
    # items the bundle already had was dropped, so posting it would repeat
    # content the channel already shows.  The collector records this during
    # finalization, inside the store lock -- re-reading the store here would
    # also count a concurrent run's contribution.
    added = collector.added_item_ids
    delta = "\n\n".join(
        part.text
        for part in collector.body_parts
        if part.source_name == source and set(part.item_ids) & added
    )

    exit_code = EXIT_OK
    if delta:
        # The Web sink goes first: its delivery state for the day is already
        # `success`, so if this command stopped before rendering it, the merged
        # content would reach no sink at all.
        exit_code = _publish_web(category)
        channel = get_channel_id(category)
        if not channel:
            log(f"{category}: no Discord channel configured; skipped the delta.")
        else:
            header = f"📎 补充：{source} 今日简报（{category} {rp.DATE}）\n\n"
            if send_to_discord(channel, header + delta):
                _record_discord_delta(briefing_id)
            else:
                log(f"Discord delivery failed for {source}; the briefing was updated.")
                exit_code = EXIT_FAILED
    else:
        log(f"{source}: nothing new to add to {briefing_id}")

    if collector.failures:
        # The part that succeeded is merged and delivered; say what is still
        # missing, and do not report success.
        log(
            f"{source} is still incomplete ({len(added)} item(s) merged): "
            + "; ".join(collector.failures)
        )
        exit_code = EXIT_FAILED
    return exit_code


def _publish_web(category: str) -> int:
    """Re-render the Web sink for this briefing.

    Forced, because its delivery state is already ``success`` for the day and
    the coordinator would otherwise skip the changed bundle.
    """
    import publish_to_web

    return publish_to_web.main(rp.DATE, [category], force=True)


def _record_discord_delta(briefing_id: str) -> None:
    """Record that the channel now carries the whole briefing.

    What the channel had plus the delta *is* the current content, so the day
    counts as delivered; the merge that produced the delta left the sink
    marked undelivered, and without this the next plain push reposts the
    entire briefing.
    """
    now = datetime.now(timezone.utc)
    try:
        DeliveryStateStore().save(
            DeliveryState(
                schema_version=DELIVERY_SCHEMA_VERSION,
                briefing_id=briefing_id,
                sink="discord",
                status="success",
                attempt_count=1,
                first_attempted_at=now,
                last_attempted_at=now,
                delivered_at=now,
            )
        )
    except Exception as exc:
        log(f"could not record the delta delivery: {exc}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run one source of a category and merge it into today's "
            "canonical briefing."
        )
    )
    parser.add_argument(
        "-c",
        "--category",
        required=True,
        choices=RERUNNABLE_CATEGORIES,
        help="Category to resume.",
    )
    parser.add_argument(
        "-s",
        "--source",
        required=True,
        help="Source name from config/sources.json (e.g. 'nature').",
    )
    return parser


if __name__ == "__main__":
    args = _build_parser().parse_args()
    sys.exit(main(args.category, args.source))
