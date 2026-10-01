"""Check that the tests guarding this branch's fixes can actually fail.

Each probe breaks one line of production code and asserts that a named test
goes red. A fix whose mutation leaves the suite green is a fix nothing
verifies -- and that, not any single bug, is what every review round on this
branch kept finding: a pin that never applied, a redaction with no test, a
test that could not fail.

Prose review found those; this script is meant to, from now on.

Run with a clean working tree: it edits files and restores them.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class Probe:
    """One invariant, the edit that breaks it, and the test that must notice."""

    label: str
    path: str
    old: str
    new: str
    test: str


PROBES: tuple[Probe, ...] = (
    Probe(
        # The original finding: deleting the wiring from main() left the suite
        # green, so a refactor could have disabled the fallback silently.
        label="the fallback key comes from the getter, not a literal",
        path="scripts/run_pipelines.py",
        old="    glm_key = _get_glm_key()",
        new='    glm_key = ""',
        test=(
            "tests/test_run_pipelines.py"
            "::test_call_ai_uses_deepseek_primary_glm_fallback"
        ),
    ),
    Probe(
        # startswith("your_") passed this test's predecessor while accepting
        # the placeholder .env.example actually ships.
        label="placeholder keys are rejected on the environment path",
        path="scripts/run_pipelines.py",
        old='    key = os.environ.get("GLM_API_KEY", "")\n    if key and "your_" not in key:',
        new='    key = os.environ.get("GLM_API_KEY", "")\n    if key:',
        test=("tests/test_run_pipelines.py::test_load_glm_key_rejects_env_placeholder"),
    ),
    Probe(
        # The last unredacted provider string in the four scripts, and the
        # second time a security fix on this branch shipped without a test.
        label="a failed send redacts and bounds the response body",
        path="scripts/push_to_discord.py",
        old="{one_line(resp.text, DISCORD_BOT_TOKEN)[:BODY_EXCERPT_CHARS]}",
        new="{resp.text}",
        test=(
            "tests/test_push_to_discord.py"
            "::test_send_failure_redacts_and_bounds_the_response_body"
        ),
    ),
    Probe(
        # The isolation has to be asserted, or a future conftest edit that
        # stops applying it is invisible until someone's suite goes red.
        label="the suite reads .env from a temp dir, not the repository",
        path="tests/conftest.py",
        old='        ("run_pipelines", "PROJECT_ROOT", str(tmp_path)),',
        new='        ("run_pipelines", "PROJECT_ROOT", str(REPO_ROOT)),',
        test=(
            "tests/test_env_isolation.py"
            "::test_run_pipelines_reads_a_env_from_a_temp_dir"
        ),
    ),
    Probe(
        # A behaviour change described as a logging change: an empty
        # generation now skips the push instead of sending a bare header.
        label="an empty generation is not pushed",
        path="scripts/backfill_push.py",
        old="    if not content:",
        new="    if False:",
        test=(
            "tests/test_backfill_push.py"
            "::test_empty_generation_is_logged_as_skipped_not_as_a_failure"
        ),
    ),
    Probe(
        # The default batch size never reached the sources, so each one sent
        # its whole article list in a single AI call. Found by running the
        # real pipeline, not by any unit test -- which is why it gets a pin.
        label="the documented batch size reaches a source that does not set one",
        path="scripts/datasource.py",
        old="        merged = {**defaults, **config}",
        new="        merged = dict(config)",
        test=(
            "tests/test_datasource_rss.py"
            "::test_get_batches_honours_the_default_batch_size"
        ),
    ),
    Probe(
        # A cap bound alone cannot tell the word-boundary cut apart from the
        # cap // 2 floor; the prose fixture pins which one fired.
        label="the deep-content cut lands on a word boundary",
        path="scripts/datasource.py",
        old='                        plain.rfind(" ", 0, self.max_content_chars),',
        new="                        -1,",
        test=(
            "tests/test_datasource_rss.py"
            "::test_use_content_per_source_cap_overrides_default"
        ),
    ),
    Probe(
        # A use_content template may only use placeholders the deep-content
        # path substitutes; anything else reaches the model verbatim.
        label="a use_content template stays deep-compatible",
        path="config/sources.json",
        old="（来自 Latent Space 的 AINews 日报）",
        new="（来自 Latent Space 的 AINews 日报）{count}",
        test="tests/test_run_pipelines.py"
        "::test_every_source_prompt_template_resolves",
    ),
    Probe(
        # Tolerant matching would treat the query-stripped base URL as the
        # same feed; a section feed must be matched exactly.
        label="subscription matching is exact, not query-stripped",
        path="scripts/freshrss_admin.py",
        old='"SELECT 1 FROM feed WHERE url = ?", [url]',
        new='"SELECT 1 FROM feed WHERE url = ?", [url.split("?")[0]]',
        test="tests/test_freshrss_admin.py"
        "::test_ensure_subscription_ignores_query_stripped_variants",
    ),
    Probe(
        # A helper nothing calls is a helper that does nothing: the wiring
        # into the category pipeline needs its own pin.
        label="the category run reaches the subscription sync",
        path="scripts/run_pipelines.py",
        old="    for name in _ensure_rss_subscriptions(cfg, db, category):",
        new="    for name in []:",
        test="tests/test_freshrss_admin.py"
        "::test_a_category_run_subscribes_and_resolves_in_the_same_run",
    ),
    Probe(
        # Without the commit the row is rolled back when the pipeline closes
        # the connection: the log line still says "subscribed" every run.
        label="a subscription is committed, not just inserted",
        path="scripts/freshrss_admin.py",
        old="    db.commit()\n",
        new="",
        test="tests/test_freshrss_admin.py::test_ensure_subscription_inserts_missing_feed",
    ),
    Probe(
        # The original silent-loss bug: one failed source raised out of
        # finalization, so the whole category went missing from both sinks.
        label="a failed source does not take its category down",
        path="scripts/run_pipelines.py",
        old='        action = "partial" if collector.results else "failed"',
        new=(
            "        raise PublicationIntegrationError(\n"
            '            f"{category} publication not finalized: "\n'
            '            + "; ".join(collector.failures)\n'
            "        )"
        ),
        test=(
            "tests/test_publication_unified.py"
            "::test_source_failure_publishes_the_successful_part"
        ),
    ),
    Probe(
        # A repeat the collector keeps makes validate_bundle reject the whole
        # bundle, which costs the category both sinks at once.
        label="a repeated item is dropped before the bundle is validated",
        path="scripts/publication/pipeline.py",
        old="            if identity.item_id in seen:",
        new="            if False:",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_repeated_item_is_dropped_before_it_reaches_the_bundle"
        ),
    ),
    Probe(
        # Seen state never expires: committing a failed item drops it for good
        # instead of leaving it for the next run to retry.
        label="a failed item is not marked seen",
        path="scripts/run_pipelines.py",
        old="    collector.defer_seen(ds, [result.raw_item for result in published])",
        new="    collector.defer_seen(ds, items)",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_failed_item_is_not_marked_seen"
        ),
    ),
    Probe(
        # A source that could not be fetched is absent from the bundle, so
        # without this the gap is invisible and the run still exits 0.
        label="a fetch failure is recorded as a gap",
        path="scripts/run_pipelines.py",
        old=(
            '        collector.add_failure(f"{name}: fetch failed: {exc}")\n'
            "        _save_placeholder(\n"
            "            category,"
        ),
        new=("        _save_placeholder(\n" "            category,"),
        test=(
            "tests/test_publication_unified.py"
            "::test_a_fetch_failure_is_recorded_as_a_gap"
        ),
    ),
    Probe(
        # An unguarded fetch in the deep-content path aborts the category run
        # instead of recording one source's failure.
        label="a deep-content fetch failure is recorded, not raised",
        path="scripts/run_pipelines.py",
        old=(
            '        collector.add_failure(f"{name}: fetch failed: {exc}")\n'
            "        return 1"
        ),
        new="        raise",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_deep_content_fetch_failure_does_not_kill_the_category"
        ),
    ),
    Probe(
        # Without the per-run reset a gap from an earlier run in the same
        # process keeps the exit code non-zero forever.
        label="gaps are reset at the start of a run",
        path="scripts/run_pipelines.py",
        old="    PUBLICATION_GAPS = []",
        new="    pass",
        test=("tests/test_publication_unified.py::test_main_resets_gaps_between_runs"),
    ),
    Probe(
        label="a code fetch failure is recorded as a gap",
        path="scripts/run_pipelines.py",
        old='            collector.add_failure(f"{ds.name}: fetch failed")',
        new="            pass",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_code_source_fetch_failure_is_recorded_as_a_gap"
        ),
    ),
    Probe(
        label="a resource news fetch failure is recorded as a gap",
        path="scripts/run_pipelines.py",
        old=(
            '                collector.add_failure(f"{ds.name}: fetch failed: {exc}")\n'
            "                continue"
        ),
        new="                continue",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_resource_news_source_fetch_failure_is_recorded_as_a_gap"
        ),
    ),
    Probe(
        # The resume command only knows what it recovered through its own
        # collector; without the wiring it reports success and posts nothing.
        label="resume collects the run's own output",
        path="scripts/resume_publication.py",
        old="            collector=collector,",
        new="            collector=None,",
        test=("tests/test_resume_publication.py::test_resume_drives_the_real_dispatch"),
    ),
    Probe(
        # A chunk has to be judged by the items it rendered, not by its source
        # name: a re-run of a covered source still brings new prose.
        label="a chunk whose own items are new reaches the merged body",
        path="scripts/run_pipelines.py",
        old="        if set(part.item_ids) - bundle_ids",
        new="        if False",
        test=(
            "tests/test_publication_unified.py"
            "::test_new_items_of_a_covered_source_reach_the_body"
        ),
    ),
    Probe(
        # The delta is the recovered source's chunk, not everything the run
        # happened to render.
        label="the resume delta carries only the resumed source",
        path="scripts/resume_publication.py",
        old=("        if part.source_name == source and set(part.item_ids) & added"),
        new="        if set(part.item_ids) & added",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_posts_only_the_requested_source_chunk"
        ),
    ),
    Probe(
        # A failed write that still marks the item seen loses it for good.
        label="a failed write does not mark the item seen",
        path="scripts/run_pipelines.py",
        old="    collector.defer_seen(ds, [result.raw_item for result in published])",
        new="    collector.defer_seen(ds, [result.raw_item for result in structured_results])",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_write_failure_leaves_that_source_fetchable"
        ),
    ),
    Probe(
        # Resuming must not re-run (and re-bill) the sources it was not asked
        # about.
        label="a resume dispatches only the source it was given",
        path="scripts/run_pipelines.py",
        old=(
            '    for source_cfg in _filter_sources(cfg, category, "scrape", "api"):\n'
            "        ds = DataSource.create(source_cfg, defaults)\n"
            "        if only_source is not None and ds.name != only_source:\n"
            "            continue"
        ),
        new=(
            '    for source_cfg in _filter_sources(cfg, category, "scrape", "api"):\n'
            "        ds = DataSource.create(source_cfg, defaults)\n"
            "        if False:\n"
            "            continue"
        ),
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_dispatches_only_the_requested_source"
        ),
    ),
    Probe(
        # The delta is what the merge kept, not what the run rendered.
        label="the resume delta is what the merge kept",
        path="scripts/resume_publication.py",
        old="        if part.source_name == source and set(part.item_ids) & added",
        new="        if part.source_name == source",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_does_not_post_a_chunk_the_merge_dropped"
        ),
    ),
    Probe(
        # Without the force override, a low-frequency source is skipped by its
        # own recent archive and a resume silently does nothing.
        label="force bypasses the low-frequency skip",
        path="scripts/run_pipelines.py",
        old=(
            "    if _is_forced(name):\n"
            "        return False\n"
            "    pushed_dir = PUSHED_DIR / category"
        ),
        new=(
            "    if False:\n"
            "        return False\n"
            "    pushed_dir = PUSHED_DIR / category"
        ),
        test=(
            "tests/test_publication_unified.py"
            "::test_force_bypasses_the_low_frequency_skip"
        ),
    ),
    Probe(
        # Without the seed, a chunk mixing an already-published item with a new
        # one is appended whole and duplicates the old item's prose.
        label="the run is seeded with what the day already carries",
        path="scripts/run_pipelines.py",
        old=(
            '        log(f"  [publication] cannot read {category}-{DATE} for seeding: {exc}")\n'
            "        return set()\n"
            "    return {item.id for item in bundle.items}"
        ),
        new=(
            '        log(f"  [publication] cannot read {category}-{DATE} for seeding: {exc}")\n'
            "        return set()\n"
            "    return set()"
        ),
        test=(
            "tests/test_publication_unified.py"
            "::test_a_refetched_published_item_is_not_rendered_again"
        ),
    ),
    Probe(
        # Conservatively matching any "⚠️" lets a notice replace a real
        # briefing that happens to mention one.
        label="a notice does not overwrite a real briefing",
        path="scripts/run_pipelines.py",
        old="    if existing and not _is_placeholder_text(existing):",
        new="    if False:",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_notice_does_not_overwrite_a_real_briefing"
        ),
    ),
    Probe(
        # An unopenable database used to look exactly like an empty fetch.
        label="an unopenable FreshRSS DB is recorded as a gap",
        path="scripts/run_pipelines.py",
        old=(
            "            publication_collector.add_failure(\n"
            '                f"{category}: cannot open the FreshRSS DB ({e})"\n'
            "            )"
        ),
        new="            pass",
        test=(
            "tests/test_publication_unified.py"
            "::test_an_unopenable_freshrss_db_is_recorded_as_a_gap"
        ),
    ),
    Probe(
        # A source with no type is "known" but never dispatched, so a resume
        # would report a successful no-op.
        label="resume rejects a source it cannot dispatch",
        path="scripts/resume_publication.py",
        old='        and source.get("type") in ("rss", "scrape", "api")',
        new="        and True",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_rejects_a_source_it_cannot_dispatch"
        ),
    ),
    Probe(
        # Returning on the first failure skipped the delivery of the part that
        # had already merged.
        label="a partial resume still delivers what merged",
        path="scripts/resume_publication.py",
        old="    elif delta:",
        new="    elif delta and not collector.failures:",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_delivers_the_part_that_merged_before_reporting_failure"
        ),
    ),
    Probe(
        # Without the lock two writers load the same base and one contribution
        # is lost.
        label="the store lock excludes a second writer",
        path="scripts/run_pipelines.py",
        old=(
            "                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "                break"
        ),
        new="                pass\n                break",
        test=(
            "tests/test_publication_unified.py"
            "::test_the_store_lock_excludes_a_second_writer"
        ),
    ),
    Probe(
        # Without the void, a briefing that gained content after delivery stays
        # "success" and neither sink ever sends it.
        label="a merged briefing is delivered again",
        path="scripts/run_pipelines.py",
        old="                for sink in _void_delivery_state(publication_id):",
        new="                for sink in []:",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_merged_briefing_is_actually_delivered"
        ),
    ),
    Probe(
        # Voiding an unchanged briefing queues a full repost for nothing.
        label="an unchanged re-publication is not voided",
        path="scripts/run_pipelines.py",
        old="            if existing is not None and _content_changed(existing, bundle):",
        new="            if existing is not None:",
        test=(
            "tests/test_publication_unified.py"
            "::test_an_unchanged_briefing_stays_delivered"
        ),
    ),
    Probe(
        # Without the attempt check, a send that started before a merge records
        # its pre-merge outcome and the tombstone is gone.
        label="an in-flight send cannot overwrite a merge",
        path="scripts/publication/publishers.py",
        old="        self.store.record_result(result, expected=pending)",
        new="        self.store.record_result(result)",
        test=(
            "tests/test_publication_unified.py"
            "::test_an_inflight_send_cannot_overwrite_a_merge"
        ),
    ),
    Probe(
        # A void that failed silently leaves the day marked delivered forever.
        label="a failed void is recorded as a gap",
        path="scripts/run_pipelines.py",
        old='            log(f"  [delivery] could not void {briefing_id}:{sink}: {exc}")\n            failed.append(sink)',
        new='            log(f"  [delivery] could not void {briefing_id}:{sink}: {exc}")',
        test=(
            "tests/test_publication_unified.py"
            "::test_a_void_failure_is_recorded_as_a_gap"
        ),
    ),
    Probe(
        # Resume's own record write is the one sink write that could bypass the
        # attempt check, so a merge landing mid-send was erased by it.
        label="the resume delta records its own attempt",
        path="scripts/resume_publication.py",
        old="            expected=pending,\n        )",
        new="        )",
        test=(
            "tests/test_resume_publication.py"
            "::test_a_merge_during_the_resume_send_wins"
        ),
    ),
    Probe(
        # A resume that merged everything but delivered nothing must not report
        # success: the day still has no Discord content.
        label="a resume reports a delivery that is still missing",
        path="scripts/resume_publication.py",
        old="        if missing:",
        new="        if False:",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_signals_a_delivery_that_is_still_missing"
        ),
    ),
    Probe(
        # A delta cannot speak for content another writer merged while the
        # resume was working; claiming it loses that content.
        label="a resume does not claim a day another writer extended",
        path="scripts/resume_publication.py",
        old="    if delta and carries_the_day and uncovered:",
        new="    if False:",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_does_not_claim_a_day_another_writer_extended"
        ),
    ),
    Probe(
        # Counting a transport failure as "skipped" reported the day as
        # delivered and exited 0.
        label="a failed send is counted as failed",
        path="scripts/push_to_discord.py",
        old=(
            "                failed += 1\n"
            '                detail = f": {result.error}" if result.error else ""'
        ),
        new=(
            "                pass\n"
            '                detail = f": {result.error}" if result.error else ""'
        ),
        test=(
            "tests/test_publication_unified.py"
            "::test_push_reports_a_failed_send_as_failed"
        ),
    ),
    Probe(
        # The coverage check ran before the Web render; a co-writer merging in
        # that window was adopted by begin_attempt and claimed as delivered.
        label="coverage is re-checked right before the send",
        path="scripts/resume_publication.py",
        old="    if current - covered_ids:",
        new="    if False:",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_rechecks_coverage_right_before_posting"
        ),
    ),
    Probe(
        # The refusal path refreshes the one sink that can be repaired without
        # another command; dropping it leaves the site stale and silent.
        label="a refused resume still refreshes the Web sink",
        path="scripts/resume_publication.py",
        old=(
            "        # The site renders the whole bundle, so refreshing it here "
            "is safe and\n"
            "        # useful even though Discord cannot be claimed.\n"
            "        _publish_web(category)"
        ),
        new="        pass",
        test=(
            "tests/test_resume_publication.py"
            "::test_resume_does_not_claim_a_day_another_writer_extended"
        ),
    ),
    Probe(
        # Joining an item's lines with the item separator puts a blank line
        # between a title and its quote, which Discord renders as two blocks.
        label="an item's title and quote stay together",
        path="scripts/run_pipelines.py",
        old='        blocks.append("\\n".join(lines))',
        new='        blocks.append("\\n\\n".join(lines))',
        test=(
            "tests/test_publication_unified.py"
            "::test_rendered_items_keep_the_title_and_its_quote_together"
        ),
    ),
    Probe(
        # An unreadable store used to escape as a traceback from mid-command.
        label="an unreadable store is reported, not raised",
        path="scripts/resume_publication.py",
        old='    except Exception as exc:\n        log(f"cannot read {briefing_id}: {exc}")\n        return None',
        new="    except Exception as exc:\n        raise",
        test=(
            "tests/test_resume_publication.py"
            "::test_the_helper_reports_an_unreadable_store_instead_of_raising"
        ),
    ),
    Probe(
        # A gap that does not reach the exit code is a gap cron cannot see.
        label="a canonical gap keeps the run non-zero",
        path="scripts/run_pipelines.py",
        old=(
            "0 if total_saved > 0 and failed_pipelines == 0 "
            "and not PUBLICATION_GAPS else 1"
        ),
        new="0 if total_saved > 0 and failed_pipelines == 0 else 1",
        test=(
            "tests/test_publication_unified.py"
            "::test_a_canonical_gap_makes_run_exit_nonzero"
        ),
    ),
    Probe(
        # Folding failed sources into "missing" (or dropping the count) was
        # the shape of the old summary; the point of this one is that a
        # source that failed is named as failed, not just absent.
        label="the summary lists the sources that failed to fetch or summarize",
        path="scripts/push_to_discord.py",
        old='                f"⚠️ 抓取或摘要失败 ({len(failed_list)}):",',
        new='                f"⚠️ 抓取或摘要失败:",',
        test=(
            "tests/test_push_to_discord.py"
            "::test_build_push_summary_lists_failed_sources_apart_from_missing"
        ),
    ),
    Probe(
        # Posting the summary outside the success gate would double it on
        # every later run of the day (skipped deliveries included).
        label="the source summary is only posted by the run that delivered",
        path="scripts/push_to_discord.py",
        old='            if result.status == "success" and category in SUMMARY_CATEGORIES:',
        new="            if category in SUMMARY_CATEGORIES:",
        test=(
            "tests/test_push_to_discord.py" "::test_no_summary_when_the_delivery_fails"
        ),
    ),
    Probe(
        # The archive consumes the day's evidence; a forced redelivery that
        # rescans it reports every bundle-less source as "missing" and
        # overwrites the good sidecar with that.
        label="an archived day is not rescanned for the summary",
        path="scripts/push_to_discord.py",
        old="                if not already_archived:",
        new="                if True:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_forced_redelivery_does_not_rescan_an_archived_day"
        ),
    ),
    Probe(
        # A delivered day must not re-post its summary on every forced
        # redelivery: the record's posted flag is what keeps it silent.
        label="an archived day does not re-post a delivered summary",
        path="scripts/push_to_discord.py",
        old="                    elif record.summary_posted:",
        new="                    elif False:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_forced_redelivery_does_not_rescan_an_archived_day"
        ),
    ),
    Probe(
        # The record also carries the repair path: a summary that never went
        # out is rebuilt from it instead of staying missing forever.
        label="a summary that never went out is repaired from the record",
        path="scripts/push_to_discord.py",
        old="                    if record is None:",
        new="                    if True:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_missing_summary_is_repaired_by_a_forced_redelivery"
        ),
    ),
    Probe(
        # The retry rides the same relaxed contract as the first call; a
        # digest that only arrived on the retry must not be rejected by a
        # stricter parse and degrade to a placeholder.
        label="the deep-content retry keeps the relaxed contract",
        path="scripts/run_pipelines.py",
        old="                    allow_literal_newlines=True,",
        new="                    allow_literal_newlines=False,",
        test=(
            "tests/test_run_pipelines.py"
            "::test_the_deep_content_retry_keeps_markdown_in_the_summary"
        ),
    ),
    Probe(
        # A repair that cannot render (config broken after delivery) used to
        # exit 0 with no summary and no record of why.
        label="a repair without a readable config is reported",
        path="scripts/push_to_discord.py",
        old='        errors.append("来源配置不可读，来源总结无法生成")',
        new="        pass",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_repair_without_a_readable_config_is_reported"
        ),
    ),
    Probe(
        # The header must count what the list renders: a stored record can
        # name a source the config no longer has.
        label="the summary header counts the rendered list",
        path="scripts/push_to_discord.py",
        old="    pushed_list = [n for n in configured_names if n in pushed_set]",
        new="    pushed_list = list(pushed_set)",
        test=(
            "tests/test_push_to_discord.py"
            "::test_the_summary_count_reflects_the_rendered_list"
        ),
    ),
    Probe(
        # A record with an unknown status value must be dropped whole; kept,
        # the row silently disappears from every bucket.
        label="an untrusted source-status record is dropped",
        path="scripts/push_to_discord.py",
        old=(
            '            or row.get("status")'
            ' not in ("pushed", "no_update", "failed", "missing")'
        ),
        new="            or False",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_malformed_source_status_record_is_not_used"
        ),
    ),
    Probe(
        # Same defect class as the scan and archive read sites: an unreadable
        # leftover in the bundleless branch used to raise out of main() and
        # kill every later category.
        label="an unreadable leftover counts as a real pending file",
        path="scripts/push_to_discord.py",
        old="        except (OSError, UnicodeDecodeError):",
        new="        except OSError:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_non_utf8_leftover_does_not_crash_a_bundleless_category"
        ),
    ),
    Probe(
        # The handler itself used to raise (one_line without its secret
        # argument), turning a reported failure into a mislabelled delivery
        # failure -- the exact shape this branch's reviews keep finding.
        label="the sidecar writer reports its own failure",
        path="scripts/push_to_discord.py",
        old="源状态文件写入失败: {one_line(str(exc), DISCORD_BOT_TOKEN)}",
        new="源状态文件写入失败: {one_line(str(exc))}",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_real_sidecar_write_error_is_reported_not_raised"
        ),
    ),
    Probe(
        # The two marker tuples live in different modules and are kept in
        # sync by hand; a rename on either side must fail this test rather
        # than silently reclassify failed sources as missing.
        label="the failure markers stay in sync with the run notices",
        path="scripts/push_to_discord.py",
        old='    ("⚠️ 获取失败", "fetch_failed"),',
        new='    ("⚠️ 取回失败", "fetch_failed"),',
        test=(
            "tests/test_push_to_discord.py"
            "::test_failure_markers_match_the_run_notices"
        ),
    ),
    Probe(
        # A low-frequency source the run skipped has no file today; without
        # the lookback check the summary reports it as a missing briefing.
        label="a low-frequency skip reads as no_update, not missing",
        path="scripts/push_to_discord.py",
        old=(
            "            if lookback > 24 and "
            "_pushed_within_lookback(category, name, lookback):"
        ),
        new="            if False:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_low_frequency_source_skipped_by_the_run_reads_as_no_update"
        ),
    ),
    Probe(
        # An unreadable config used to vanish the summary silently: no
        # message, no sidecar, exit 0.
        label="an unreadable source config is reported",
        path="scripts/push_to_discord.py",
        old="    if not statuses:",
        new="    if False:",
        test=(
            "tests/test_push_to_discord.py"
            "::test_a_sources_config_failure_is_reported"
        ),
    ),
    Probe(
        # The deep-content contract asks for multi-line Markdown; without
        # strict=False a literal newline in the JSON string loses the digest
        # to a retry that produces the same bytes.
        label="a literal newline inside the summary still parses",
        path="scripts/publication/pipeline.py",
        old="json.loads(_strip_json_fence(raw), strict=not allow_literal_newlines)",
        new="json.loads(_strip_json_fence(raw))",
        test=(
            "tests/test_publication_pipeline.py"
            "::test_a_literal_newline_inside_the_summary_parses"
        ),
    ),
    Probe(
        # The legacy Markdown files are archived at delivery, so the bundle is
        # the only surviving record of which sources had content.
        label="the pushed list comes from the canonical bundle",
        path="scripts/push_to_discord.py",
        old="    pushed = {item.source.name for item in bundle.items}",
        new="    pushed = set()",
        test=(
            "tests/test_push_to_discord.py"
            "::test_the_summary_counts_a_source_whose_file_was_archived"
        ),
    ),
    Probe(
        # An empty sidecar still parses; only the row assertion catches a
        # writer that stopped recording the sources.
        label="the source-status sidecar records a row per source",
        path="scripts/push_to_discord.py",
        old='        "sources": [status.to_dict() for status in statuses],',
        new='        "sources": [],',
        test=(
            "tests/test_push_to_discord.py"
            "::test_the_source_status_sidecar_records_every_source"
        ),
    ),
    Probe(
        # Relaxing the wrong call site (or all of them) would let model
        # Markdown leak into papers/code summaries, which the renderers there
        # do not expect; only the deep-content call may keep it.
        label="the deep-content prompt keeps Markdown inside the summary",
        path="scripts/run_pipelines.py",
        old=(
            "        prompt = structured_prompt("
            "base, entries, [ref], markdown_summary=True)"
        ),
        new="        prompt = structured_prompt(base, entries, [ref])",
        test=(
            "tests/test_run_pipelines.py"
            "::test_only_the_deep_content_prompt_keeps_markdown_in_the_summary"
        ),
    ),
)


def _run_test(node: str) -> tuple[int, str]:
    """Run one test under a mutation; return its exit code and output.

    The output matters: pytest exits 4 for a test that no longer exists, and
    treating any non-zero exit as "the mutation turned it red" would report a
    probe as working when its test was renamed or deleted.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-rf", node],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _working_tree_changes() -> str:
    """Modified tracked files, ignoring untracked ones.

    Only tracked files are edited here, and they are restored from their
    current content, so uncommitted work is preserved -- but a run killed
    mid-probe would leave the mutation sitting in it, which is worth refusing.
    Untracked files are never at risk, so they do not block.
    """
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main() -> int:
    dirty = _working_tree_changes()
    if dirty:
        print("refusing to run: this script edits files and restores them,")
        print("and the working tree has uncommitted changes:\n")
        print(dirty)
        return 2

    failures: list[str] = []
    for probe in PROBES:
        target = REPO_ROOT / probe.path
        original = target.read_text(encoding="utf-8")
        found = original.count(probe.old)
        if found != 1:
            failures.append(
                f"{probe.label}: anchor occurs {found} times in {probe.path}, "
                "expected 1 -- the code moved, so the probe no longer tests it"
            )
            continue

        try:
            target.write_text(original.replace(probe.old, probe.new), encoding="utf-8")
            returncode, output = _run_test(probe.test)
        finally:
            target.write_text(original, encoding="utf-8")

        if returncode == 0 or not re.search(r"\b\d+ (failed|error)", output):
            failures.append(
                f"{probe.label}: {probe.test} did not fail under the mutation "
                f"(exit {returncode}): it either passed, or the test it names is "
                "gone"
            )
        else:
            print(f"ok    {probe.label}")

    if failures:
        print()
        for failure in failures:
            print(f"FAIL  {failure}")
        return 1

    print(f"\n{len(PROBES)} probes, each turned its test red")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
