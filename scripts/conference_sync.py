"""Incremental OpenReview submission ingestion.

The sync layer owns the schedule and the no-loss contract:

* every page is stored and its pagination cursor committed before the next
  page is requested;
* the source watermark advances only after the page iterator is exhausted;
* a failure records an outcome and leaves the cursor where it was, so the
  next run resumes instead of re-reading from the start.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from conference_ai_briefing import render_pending_events
from conference_briefing import (
    baseline_path,
    briefing_path,
    render_baseline,
    render_briefing,
    write_briefing,
)
from conference_events import (
    build_event,
    build_snapshot,
    detect_event_types,
    is_ai_event,
    snapshot_signature,
)
from conference_reviews import build_page_review_data
from conference_store import ConferenceStore, SyncRun, VenueState
from logsafe import one_line
from openreview_provider import (
    OpenReviewNotPublic,
    OpenReviewProvider,
    VenueCapabilities,
    classify_openreview_error,
)
from paths import _read_env_value


@dataclass(frozen=True)
class SourceResult:
    source: str
    outcome: str
    new_papers: int = 0
    new_replies: int = 0
    briefing_path: str | None = None
    ai_briefings: int = 0
    ai_briefing_paths: tuple[str, ...] = ()
    message: str = ""
    fatal: bool = False


def _secret() -> str:
    return os.environ.get("OPENREVIEW_PASSWORD", "") or _read_env_value(
        "OPENREVIEW_PASSWORD"
    )


def _safe_error(exc: BaseException) -> str:
    """Flatten an exception and redact every credential this pipeline holds."""

    text = one_line(str(exc), "")
    for key in ("OPENREVIEW_PASSWORD", "DEEPSEEK_API_KEY", "GLM_API_KEY"):
        secret = os.environ.get(key, "") or _read_env_value(key)
        if secret:
            text = one_line(text, secret)
    return text


def _config_hash(
    *,
    capabilities: VenueCapabilities,
    mode: str,
    min_cdate: int | None,
    page_size: int,
) -> str:
    payload = {
        "venue_id": capabilities.venue_id,
        "invitation": capabilities.submission_invitation,
        "mode": mode,
        "min_cdate": min_cdate,
        "page_size": page_size,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _not_due(state: VenueState, interval_hours: float, now_ms: int) -> bool:
    if state.last_poll_ms is None:
        return False
    interval_ms = max(0.0, float(interval_hours)) * 3_600_000
    return now_ms - int(state.last_poll_ms) < interval_ms


def _select_mode(
    state: VenueState,
    *,
    full_rescan_interval_days: float,
    now_ms: int,
) -> str:
    if state.bootstrap_completed_ms is None:
        return "bootstrap"
    if state.last_full_sync_ms is None:
        return "full"
    interval_ms = max(0.0, float(full_rescan_interval_days)) * 86_400_000
    if now_ms - int(state.last_full_sync_ms) >= interval_ms:
        return "full"
    return "incremental"


def _min_cdate(state: VenueState, *, mode: str, overlap_hours: float) -> int | None:
    if mode != "incremental" or state.watermark_ms is None:
        return None
    overlap_ms = max(0.0, float(overlap_hours)) * 3_600_000
    return max(0, int(state.watermark_ms - overlap_ms))


def _flush_pending(
    *,
    store: ConferenceStore,
    source: str,
    display_name: str,
    briefings_dir: str | Path,
    date: str,
) -> str | None:
    """Write and mark today's pending papers; safe to call on every run."""

    pending = store.pending_papers(source)
    if not pending:
        return None
    path = briefing_path(briefings_dir, source, date)
    content = render_briefing(
        display_name=display_name,
        source=source,
        date=date,
        papers=pending,
    )
    write_briefing(path, content)
    store.mark_briefed(
        source,
        date,
        [str(paper["forum_id"]) for paper in pending],
    )
    return str(path)


def _write_baseline(
    *,
    store: ConferenceStore,
    source: str,
    display_name: str,
    briefings_dir: str | Path,
    date: str,
) -> str:
    path = baseline_path(briefings_dir, source, date)
    write_briefing(
        path,
        render_baseline(
            display_name=display_name,
            source=source,
            date=date,
            status_counts=store.status_counts(source),
        ),
    )
    return str(path)


def _merge_config(defaults: dict, source_cfg: dict) -> dict:
    """Merge conference defaults with one source, keeping review defaults."""

    base = defaults.get("conference_defaults", {}) or {}
    cfg = {**base, **source_cfg}
    cfg["reviews"] = {
        **(base.get("reviews", {}) or {}),
        **(source_cfg.get("reviews", {}) or {}),
    }
    cfg["ai_briefing"] = {
        **(base.get("ai_briefing", {}) or {}),
        **(source_cfg.get("ai_briefing", {}) or {}),
    }
    return cfg


def _reviews_enabled(reviews_cfg: dict) -> bool:
    return bool(reviews_cfg.get("enabled", False))


def _ai_enabled(cfg: dict, call_ai: Callable[..., str] | None) -> bool:
    return (
        bool((cfg.get("ai_briefing") or {}).get("enabled", False))
        and call_ai is not None
    )


def _build_page_events(
    *,
    store: ConferenceStore,
    source: str,
    run: SyncRun,
    papers: list[dict],
    reply_rows: list[dict],
    summaries: list[dict],
    include_replies: bool,
    cfg: dict,
    now_ms: int,
) -> list[dict]:
    """Build events for one page from the before/after snapshots.

    Only a real review/decision/status change becomes an AI event by default;
    a plain new submission remains the deterministic listing's job.
    """

    review_cfg = cfg.get("reviews", {}) or {}
    ai_cfg = cfg.get("ai_briefing") or {}
    max_review_chars = int(ai_cfg.get("max_review_chars", 4_000) or 4_000)
    max_abstract_chars = int(ai_cfg.get("max_abstract_chars", 4_000) or 4_000)
    include_new_submissions = bool(ai_cfg.get("include_new_submissions", False))
    baseline_cutoff = run.started_ms if run.mode == "bootstrap" else None
    if baseline_cutoff is not None:
        # Baseline papers were already known when the venue was first
        # imported; filtering them before the per-forum lookup avoids tens of
        # thousands of pointless queries during a bootstrap.
        papers = [
            paper for paper in papers if int(paper.get("cdate") or 0) >= baseline_cutoff
        ]
    if not papers:
        return []

    forum_ids = [
        str(paper.get("forum_id") or paper.get("note_id") or "") for paper in papers
    ]
    before_states = store.forum_states(source, forum_ids)
    replies_by_forum: dict[str, list[dict]] = {}
    for row in reply_rows:
        replies_by_forum.setdefault(str(row.get("forum_id") or ""), []).append(row)
    summaries_by_forum = {
        str(summary.get("forum_id") or ""): summary for summary in summaries
    }
    events: list[dict] = []

    for paper in papers:
        forum_id = str(paper.get("forum_id") or paper.get("note_id") or "")
        if not forum_id:
            continue
        before = before_states.get(forum_id)
        before_signature = None
        if before is not None:
            before_snapshot = build_snapshot(
                before["paper"],
                before["reviews"],
                before["summary"],
                review_cfg=review_cfg,
                max_review_chars=max_review_chars,
                max_abstract_chars=max_abstract_chars,
                # Signature building must see every review; the prompt is
                # capped later by the renderer.
                max_reviews_per_paper=0,
            )
            before_signature = snapshot_signature(before_snapshot)

        if include_replies:
            after_reviews = replies_by_forum.get(forum_id)
            if not after_reviews:
                # No public reply in this page: keep the previously stored
                # reviews rather than treating an empty page as a deletion.
                after_reviews = before["reviews"] if before else []
            after_summary = summaries_by_forum.get(forum_id)
            if after_summary is None:
                after_summary = before["summary"] if before else None
        else:
            after_reviews = before["reviews"] if before else []
            after_summary = before["summary"] if before else None

        after_snapshot = build_snapshot(
            paper,
            after_reviews,
            after_summary,
            review_cfg=review_cfg,
            max_review_chars=max_review_chars,
            max_abstract_chars=max_abstract_chars,
            max_reviews_per_paper=0,
        )
        after_signature = snapshot_signature(after_snapshot)
        event_types = detect_event_types(before_signature, after_signature)
        if not is_ai_event(
            event_types,
            after_snapshot,
            include_new_submissions=include_new_submissions,
        ):
            continue
        events.append(
            build_event(
                source=source,
                forum_id=forum_id,
                event_types=event_types,
                signature=after_signature,
                now_ms=now_ms,
            )
        )
    return events


def _seed_ai_events(
    *,
    store: ConferenceStore,
    source: str,
    state: VenueState,
    cfg: dict,
    call_ai: Callable[..., str] | None,
    now_ms: int,
) -> str:
    """Queue existing reviews once, when the AI layer is first enabled."""

    if not _ai_enabled(cfg, call_ai) or state.ai_initialized_ms is not None:
        return ""
    ai_cfg = cfg.get("ai_briefing") or {}
    try:
        store.seed_review_events(
            source,
            now_ms,
            max_review_chars=int(ai_cfg.get("max_review_chars", 4_000) or 4_000),
            max_abstract_chars=int(ai_cfg.get("max_abstract_chars", 4_000) or 4_000),
            # The signature sees every reply; only the prompt is capped.
            max_reviews_per_paper=0,
            limit=int(ai_cfg.get("seed_limit", 200) or 200),
        )
        store.mark_ai_initialized(source, now_ms)
    except Exception as exc:
        return _safe_error(exc)
    return ""


def _render_ai(
    *,
    store: ConferenceStore,
    source: str,
    display_name: str,
    cfg: dict,
    defaults: dict,
    call_ai: Callable[..., str] | None,
    briefings_dir: str | Path,
    date: str,
    now_ms: int,
) -> tuple[list[str], str]:
    """Render pending AI briefings; a failure never fails ingestion."""

    if not _ai_enabled(cfg, call_ai):
        return [], ""
    try:
        paths = render_pending_events(
            store=store,
            source=source,
            display_name=display_name,
            config=cfg,
            defaults=defaults,
            call_ai=call_ai,
            briefings_dir=briefings_dir,
            date=date,
            now_ms=now_ms,
        )
        failed = store.failed_event_count(source)
        retrying = store.retrying_event_count(source)
        if failed:
            return paths, f"{failed} AI event(s) failed permanently"
        if retrying:
            return paths, f"{retrying} AI event(s) failed; will retry"
        return paths, ""
    except SystemExit:
        # ``load_deepseek_key`` exits the process when the key is absent.
        # The conference store is already durable; do not take pipeline 6
        # down because the optional AI layer is unconfigured.
        return [], "DEEPSEEK_API_KEY not configured"
    except Exception as exc:
        return [], _safe_error(exc)


def _select_review_mode(
    state: VenueState,
    base_mode: str,
    reviews_cfg: dict,
    now_ms: int,
) -> str:
    """Pick the sample/rescan pass before the metadata pass."""

    if not _reviews_enabled(reviews_cfg) or base_mode == "bootstrap":
        return base_mode
    sample_limit = int(reviews_cfg.get("sample_limit", 0) or 0)
    if state.reviews_sample_completed_ms is None and sample_limit > 0:
        return "review_sample"
    # A due metadata full rescan must not be starved by a review rescan that
    # shares the same interval; run it first, then review_rescan next time.
    if base_mode == "full":
        return base_mode
    last_rescan = state.last_review_rescan_ms
    interval_ms = (
        max(0.0, float(reviews_cfg.get("rescan_interval_days", 7) or 0)) * 86_400_000
    )
    if last_rescan is None or now_ms - int(last_rescan) >= interval_ms:
        return "review_rescan"
    return base_mode


def _review_min_cdate(
    state: VenueState,
    *,
    mode: str,
    watch_from_ms: int | None,
    overlap_hours: float,
) -> int | None:
    if mode == "review_rescan":
        return watch_from_ms
    if mode == "incremental":
        computed = _min_cdate(state, mode="incremental", overlap_hours=overlap_hours)
        if watch_from_ms is not None:
            # The streaming watermark can predate the review cutoff (the
            # existing ICLR bootstrap ended in September).  Without this
            # clamp, the first incremental review run would pull the entire
            # deadline week again.
            return (
                max(watch_from_ms, computed) if computed is not None else watch_from_ms
            )
        return computed
    return None


def sync_source(
    *,
    source_cfg: dict,
    defaults: dict,
    store: ConferenceStore,
    provider: OpenReviewProvider,
    briefings_dir: str | Path,
    date: str,
    now_ms: int | None = None,
    force: bool = False,
    call_ai: Callable[..., str] | None = None,
) -> SourceResult:
    """Ingest one OpenReview source with resumable, loss-free pagination."""

    cfg = _merge_config(defaults, source_cfg)
    reviews_cfg = cfg.get("reviews", {})
    source = str(cfg.get("name") or "").strip()
    venue_id = str(cfg.get("venue_id") or "").strip()
    display_name = str(cfg.get("display_name") or source)
    if (
        not source
        or not venue_id
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", source)
        or source in {".", ".."}
    ):
        return SourceResult(
            source=source or "<unknown>",
            outcome="INVALID_CONFIG",
            message="conference source needs a safe name and venue_id",
            fatal=True,
        )

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    store.ensure_venue(source, venue_id)
    state = store.venue_state(source)
    assert state is not None
    watch_from = None
    if _reviews_enabled(reviews_cfg):
        watch_from = store.ensure_reviews_watch_from(source, now_ms)
    seed_error = _seed_ai_events(
        store=store,
        source=source,
        state=state,
        cfg=cfg,
        call_ai=call_ai,
        now_ms=now_ms,
    )

    if not force and _not_due(
        state,
        float(cfg.get("poll_interval_hours", 24)),
        now_ms,
    ):
        # Pending rows can exist after a briefing failure; a not-due run is
        # still a chance to flush them without touching the API.
        try:
            flushed = _flush_pending(
                store=store,
                source=source,
                display_name=display_name,
                briefings_dir=briefings_dir,
                date=date,
            )
            message = f"next poll in {cfg.get('poll_interval_hours', 24)}h"
        except Exception as exc:
            flushed = None
            message = f"pending briefing failed: {one_line(str(exc), _secret())}"
        ai_paths, ai_error = _render_ai(
            store=store,
            source=source,
            display_name=display_name,
            cfg=cfg,
            defaults=defaults,
            call_ai=call_ai,
            briefings_dir=briefings_dir,
            date=date,
            now_ms=now_ms,
        )
        if seed_error:
            message = f"{message}; AI event seed failed: {seed_error}"
        if ai_paths:
            message = f"{message}; AI briefings: {len(ai_paths)}"
        if ai_error:
            message = f"{message}; AI briefing failed: {ai_error}"
        return SourceResult(
            source=source,
            outcome="NOT_DUE",
            briefing_path=flushed,
            ai_briefings=len(ai_paths),
            ai_briefing_paths=tuple(ai_paths),
            message=message,
        )

    base_mode = _select_mode(
        state,
        full_rescan_interval_days=float(cfg.get("full_rescan_interval_days", 7)),
        now_ms=now_ms,
    )
    mode = _select_review_mode(state, base_mode, reviews_cfg, now_ms)
    include_replies = _reviews_enabled(reviews_cfg) and mode in {
        "incremental",
        "review_rescan",
        "review_sample",
    }
    sample_limit = int(reviews_cfg.get("sample_limit", 0) or 0)
    if mode == "review_sample":
        page_size = max(1, min(sample_limit, 1000))
    elif include_replies:
        page_size = max(1, min(int(reviews_cfg.get("page_size", 200) or 200), 1000))
    else:
        page_size = max(1, min(int(cfg.get("page_size", 1000) or 1000), 1000))
    min_cdate = _review_min_cdate(
        state,
        mode=mode,
        watch_from_ms=watch_from,
        overlap_hours=float(cfg.get("watermark_overlap_hours", 48)),
    )
    sort = "cdate" if mode == "review_sample" else "id"
    max_pages: int | None = None
    if mode != "review_sample":
        # Cap metadata scans too: a shifting API window must not loop forever.
        if include_replies:
            configured = int(
                reviews_cfg.get("max_pages_per_run", cfg.get("max_pages_per_run", 100))
                or 0
            )
        else:
            configured = int(cfg.get("max_pages_per_run", 100) or 0)
        max_pages = configured if configured > 0 else None
    interval = (
        max(0.0, float(reviews_cfg.get("request_interval_seconds", 0) or 0))
        if include_replies
        else 0.0
    )
    run: SyncRun | None = None

    try:
        capabilities = provider.discover_venue()
        store.ensure_venue(source, venue_id, capabilities.submission_invitation)
        config_hash = _config_hash(
            capabilities=capabilities,
            mode=mode,
            min_cdate=min_cdate,
            page_size=page_size,
        )
        run = store.start_run(
            source=source,
            venue_id=venue_id,
            invitation=capabilities.submission_invitation,
            mode=mode,
            config_hash=config_hash,
            now_ms=now_ms,
        )

        new_count = 0
        new_replies = 0
        total = run.total_expected
        pages = 0
        paused = False
        event_error = ""
        iterator = provider.iter_submission_pages(
            capabilities,
            min_cdate=min_cdate,
            after_id=run.cursor_after,
            page_size=page_size,
            total_hint=run.total_expected,
            include_replies=include_replies,
            sort=sort,
        )
        page = next(iterator, None)
        while page is not None:
            pages += 1
            candidate = max(
                (int(paper.get("cdate") or 0) for paper in page.papers),
                default=None,
            )
            reply_rows: list[dict] = []
            summaries: list[dict] = []
            if include_replies and page.replies:
                reply_rows, summaries = build_page_review_data(
                    page.replies, capabilities, cfg
                )
            page_events: list[dict] = []
            if _ai_enabled(cfg, call_ai):
                try:
                    page_events = _build_page_events(
                        store=store,
                        source=source,
                        run=run,
                        papers=page.papers,
                        reply_rows=reply_rows,
                        summaries=summaries,
                        include_replies=include_replies,
                        cfg=cfg,
                        now_ms=now_ms,
                    )
                except Exception as exc:
                    # Event detection is best-effort.  The page is durable;
                    # a detection failure is reported in the run message, but
                    # the AI briefing for this page may be lost because the
                    # committed before-state hides the diff on the next run.
                    event_error = _safe_error(exc)
            committed = store.commit_page(
                run_id=run.run_id,
                papers=page.papers,
                replies=reply_rows,
                summaries=summaries,
                events=page_events,
                cursor_after=page.cursor_after,
                candidate_watermark_ms=candidate,
                total_expected=page.total,
                raw_count=page.raw_count,
                now_ms=now_ms,
            )
            new_count += committed.new_papers
            new_replies += committed.new_replies
            if page.total is not None:
                total = page.total

            if mode == "review_sample":
                # The sample is exactly the latest N submissions; a second
                # page would silently turn it into a longer backfill.
                break
            if max_pages is not None and pages >= max_pages:
                paused = True
                break
            if interval:
                time.sleep(interval)
            page = next(iterator, None)

        if paused:
            store.pause_run(
                run.run_id,
                f"max_pages_per_run={max_pages}",
                now_ms=now_ms,
            )
            message = (
                f"{mode}: paused after {pages} pages, "
                f"{new_count} new papers, {new_replies} new replies"
            )
            ai_paths, ai_error = _render_ai(
                store=store,
                source=source,
                display_name=display_name,
                cfg=cfg,
                defaults=defaults,
                call_ai=call_ai,
                briefings_dir=briefings_dir,
                date=date,
                now_ms=now_ms,
            )
            if seed_error:
                message = f"{message}; AI event seed failed: {seed_error}"
            if event_error:
                message = f"{message}; AI event failed: {event_error}"
            if ai_paths:
                message = f"{message}; AI briefings: {len(ai_paths)}"
            if ai_error:
                message = f"{message}; AI briefing failed: {ai_error}"
            return SourceResult(
                source=source,
                outcome="PAUSED",
                new_papers=new_count,
                new_replies=new_replies,
                ai_briefings=len(ai_paths),
                ai_briefing_paths=tuple(ai_paths),
                message=message,
            )

        if mode == "bootstrap":
            # Written before finish_run: if the baseline cannot be written,
            # bootstrap must not be marked complete, because the next run
            # would never revisit it.
            _write_baseline(
                store=store,
                source=source,
                display_name=display_name,
                briefings_dir=briefings_dir,
                date=date,
            )
        store.finish_run(run.run_id, now_ms=now_ms)
        try:
            flushed = _flush_pending(
                store=store,
                source=source,
                display_name=display_name,
                briefings_dir=briefings_dir,
                date=date,
            )
            briefing_error = ""
        except Exception as exc:
            # The papers and watermark are already durable; leaving the rows
            # pending is what makes the next run retry the briefing.
            flushed = None
            briefing_error = one_line(str(exc), _secret())
        ai_paths, ai_error = _render_ai(
            store=store,
            source=source,
            display_name=display_name,
            cfg=cfg,
            defaults=defaults,
            call_ai=call_ai,
            briefings_dir=briefings_dir,
            date=date,
            now_ms=now_ms,
        )
        message = (
            f"{mode}: {new_count} new papers, {new_replies} new replies, "
            f"{total if total is not None else '?'} total"
        )
        if briefing_error:
            message = f"{message}; briefing failed: {briefing_error}"
        if seed_error:
            message = f"{message}; AI event seed failed: {seed_error}"
        if event_error:
            message = f"{message}; AI event failed: {event_error}"
        if ai_paths:
            message = f"{message}; AI briefings: {len(ai_paths)}"
        if ai_error:
            message = f"{message}; AI briefing failed: {ai_error}"
        return SourceResult(
            source=source,
            outcome="SUCCESS" if not briefing_error else "SUCCESS_BRIEFING_FAILED",
            new_papers=new_count,
            new_replies=new_replies,
            briefing_path=flushed,
            ai_briefings=len(ai_paths),
            ai_briefing_paths=tuple(ai_paths),
            message=message,
        )
    except OpenReviewNotPublic as exc:
        message = one_line(str(exc), _secret())
        store.record_outcome(source, "NOT_OPEN", message)
        try:
            flushed = _flush_pending(
                store=store,
                source=source,
                display_name=display_name,
                briefings_dir=briefings_dir,
                date=date,
            )
        except Exception as flush_exc:
            message = (
                f"{message}; pending briefing failed: "
                f"{one_line(str(flush_exc), _secret())}"
            )
            flushed = None
        ai_paths, ai_error = _render_ai(
            store=store,
            source=source,
            display_name=display_name,
            cfg=cfg,
            defaults=defaults,
            call_ai=call_ai,
            briefings_dir=briefings_dir,
            date=date,
            now_ms=now_ms,
        )
        if seed_error:
            message = f"{message}; AI event seed failed: {seed_error}"
        if ai_paths:
            message = f"{message}; AI briefings: {len(ai_paths)}"
        if ai_error:
            message = f"{message}; AI briefing failed: {ai_error}"
        return SourceResult(
            source=source,
            outcome="NOT_OPEN",
            briefing_path=flushed,
            ai_briefings=len(ai_paths),
            ai_briefing_paths=tuple(ai_paths),
            message=message,
        )
    except Exception as exc:
        outcome = classify_openreview_error(exc)
        message = one_line(str(exc), _secret())
        if run is not None:
            store.fail_run(run.run_id, outcome, message, now_ms=now_ms)
        else:
            store.record_outcome(source, outcome, message)
        return SourceResult(
            source=source,
            outcome=outcome,
            message=message,
            fatal=True,
        )
