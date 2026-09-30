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
from typing import Optional

from publication import (
    DELIVERY_SINKS,
    DeliveryState,
    DeliveryStateStore,
    DeliveryStoreError,
    PublicationRunCollector,
    PublicationStore,
    PublishResult,
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


def _delivery_state(briefing_id: str, sink: str) -> Optional[DeliveryState]:
    try:
        return DeliveryStateStore().load(briefing_id, sink)
    except Exception as exc:
        log(f"cannot read the {sink} delivery state: {exc}")
        return None


def _carries_the_day(briefing_id: str, sink: str) -> bool:
    """Whether this sink already has the briefing's earlier content.

    Only then is a delta the right thing to post.  If the push never ran, or
    failed, the sink has nothing to append to and the whole briefing has to go
    out -- recording a delta as the day's delivery would lose the rest of it.

    Read before the run: the merge voids the record, so afterwards the state is
    always a tombstone and the answer would always be "no".
    """
    state = _delivery_state(briefing_id, sink)
    return state is not None and state.status == "success"


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
    carries_the_day = _carries_the_day(briefing_id, "discord")

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

    # What the channel holds is the pre-run content.  Anything that appeared in
    # the bundle since -- beyond what this run added -- is content a supplement
    # cannot speak for: recording success would mark the day delivered with it
    # missing from the channel.
    after_ids = _bundle_item_ids(briefing_id) or set()
    uncovered = after_ids - before_ids - added

    exit_code = EXIT_OK
    if delta and carries_the_day and uncovered:
        log(
            f"{briefing_id} gained {len(uncovered)} item(s) from another run "
            f"while {source} was being recovered; a supplement cannot speak for "
            "those. Run `dailyinfo push` so the channel gets the whole briefing."
        )
        # The site renders the whole bundle, so refreshing it here is safe and
        # useful even though Discord cannot be claimed.
        _publish_web(category)
        exit_code = EXIT_FAILED
    elif delta:
        # The Web sink goes first: the merge marked it undelivered (a pending
        # tombstone), and if this command stopped before rendering it, the
        # merged content would reach no sink at all.
        exit_code = _publish_web(category)
        channel = get_channel_id(category)
        if not channel:
            log(f"{category}: no Discord channel configured; skipped the delta.")
        else:
            if carries_the_day:
                payload = f"📎 补充：{source} 今日简报（{category} {rp.DATE}）\n\n{delta}"
            else:
                # The channel has none of today's briefing, so a supplement
                # would be the only thing it ever sees.
                log(
                    "  Discord has no delivery for this briefing yet; posting "
                    "the whole briefing instead of a supplement"
                )
                payload = PublicationStore().load_bundle(briefing_id).briefing.body
            covered = (
                before_ids | added
                if carries_the_day
                else (_bundle_item_ids(briefing_id) or set())
            )
            if not _post_and_record(
                briefing_id, channel, payload, covered_ids=covered
            ):
                exit_code = EXIT_FAILED
    else:
        log(f"{source}: nothing new to add to {briefing_id}")
        missing = [
            sink for sink in DELIVERY_SINKS if not _carries_the_day(briefing_id, sink)
        ]
        if missing:
            # Everything is merged, but a sink never received it -- a failed
            # delta earlier, or a merge by another path.  The commands below
            # repair that, and this run must not look like a success.
            log(
                f"  {', '.join(missing)} still has no delivery for {briefing_id}: "
                "run `dailyinfo push` and `dailyinfo publish --sink web`"
            )
            exit_code = EXIT_FAILED

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


def _post_and_record(
    briefing_id: str, channel: str, payload: str, *, covered_ids: set[str]
) -> bool:
    """Post to Discord and record the outcome as this attempt's own.

    Through the same machinery as every other sink write, because this is the
    one that used to bypass it.  A merge that lands while the send is in flight
    replaces the record with its tombstone; recording a bare success here would
    erase that and leave the day reading delivered with the merged content
    missing.  ``record_result`` refuses an outcome for a state that changed
    underneath it, and the tombstone then stands for the next push.

    ``covered_ids`` is what this payload speaks for, re-checked here rather than
    only before the Web render: that render runs the site's git gates and
    takes seconds, and a co-writer merging inside it would otherwise be adopted
    as this attempt's own state.
    """
    current = _bundle_item_ids(briefing_id) or set()
    if current - covered_ids:
        log(
            f"  {briefing_id} changed while this send was being prepared: "
            "run `dailyinfo push` and `dailyinfo publish --sink web`"
        )
        return False
    store = DeliveryStateStore()
    when = datetime.now(timezone.utc)
    try:
        pending = store.begin_attempt(briefing_id, "discord", attempted_at=when)
        delivered = send_to_discord(channel, payload)
        store.record_result(
            PublishResult(
                sink="discord",
                publication_id=briefing_id,
                status="success" if delivered else "failed",
                attempted_at=when,
                error=None if delivered else "Discord transport returned failure",
            ),
            expected=pending,
        )
    except DeliveryStoreError as exc:
        log(f"  Discord was re-published while this send was in flight: {exc}")
        return False
    if not delivered:
        log(f"Discord delivery failed for {briefing_id}; the briefing was updated.")
    return delivered


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
