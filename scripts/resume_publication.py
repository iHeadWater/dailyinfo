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
from datetime import datetime
import sys

import run_pipelines as rp
from paths import CURRENT_ENV, get_channel_id
from publication import PublicationRunCollector, PublicationStore
from push_to_discord import send_to_discord

RERUNNABLE_CATEGORIES = ("papers", "ai_news", "arxiv")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNSUPPORTED = 2


def log(msg):
    """输出日志（附带当前环境标记）"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [env:{CURRENT_ENV}] {msg}", flush=True)


def main(category: str, source: str) -> int:
    if category not in RERUNNABLE_CATEGORIES:
        log(
            f"{category} runs as one category-wide function, so a single source "
            "cannot be resumed; use `dailyinfo run --force all` instead."
        )
        return EXIT_UNSUPPORTED

    briefing_id = f"{category}-{rp.DATE}"
    try:
        bundle = PublicationStore().load_bundle(briefing_id)
    except FileNotFoundError:
        log(f"No canonical briefing {briefing_id}: run `dailyinfo run` first.")
        return EXIT_FAILED
    except Exception as exc:
        log(f"Cannot read {briefing_id}: {exc}")
        return EXIT_FAILED

    already_present = source in {item.source.name for item in bundle.items}

    # Force this one source past the "briefing already exists" skip; leave
    # every other source skipped, which is what keeps the AI spend to one
    # source.
    rp.FORCE_ALL = False
    rp.FORCE_SOURCES = {source}
    collector = PublicationRunCollector(category)
    try:
        rp._run_category_pipeline(
            category,
            deep_content=(category == "ai_news"),
            collector=collector,
        )
    except Exception as exc:
        log(f"Re-running {source} failed: {exc}")
        return EXIT_FAILED

    if not collector.body:
        log(f"{source}: nothing new to add to {briefing_id}")
        return EXIT_OK

    if already_present:
        # Its prose is already in the body; re-posting the chunk would show the
        # source twice in the channel.
        log(
            f"{source} is already in {briefing_id}: merged its items, did not "
            "post its prose again (use `dailyinfo run --force all` for a full "
            "rebuild)."
        )
    else:
        channel = get_channel_id(category)
        if not channel:
            log(f"{category}: no Discord channel configured; skipped the delta.")
        else:
            header = f"📎 补充：{source} 今日简报（{category} {rp.DATE}）\n\n"
            if not send_to_discord(channel, header + collector.body):
                log(
                    f"Discord delivery failed for {source}; the canonical "
                    "briefing itself was updated and the Web sink will be "
                    "re-rendered."
                )
                return EXIT_FAILED

    return _publish_web(category)


def _publish_web(category: str) -> int:
    """Re-render the Web sink for this briefing.

    Forced, because its delivery state is already ``success`` for the day and
    the coordinator would otherwise skip the changed bundle.
    """
    import publish_to_web

    return publish_to_web.main(rp.DATE, [category], force=True)


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
