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
import time
from pathlib import Path

from conference_briefing import (
    baseline_path,
    briefing_path,
    render_baseline,
    render_briefing,
    write_briefing,
)
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
    briefing_path: str | None = None
    message: str = ""
    fatal: bool = False


def _secret() -> str:
    return os.environ.get("OPENREVIEW_PASSWORD", "") or _read_env_value(
        "OPENREVIEW_PASSWORD"
    )


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
) -> SourceResult:
    """Ingest one OpenReview source with resumable, loss-free pagination."""

    cfg = {**defaults.get("conference_defaults", {}), **source_cfg}
    source = str(cfg.get("name") or "").strip()
    venue_id = str(cfg.get("venue_id") or "").strip()
    display_name = str(cfg.get("display_name") or source)
    if not source or not venue_id:
        return SourceResult(
            source=source or "<unknown>",
            outcome="INVALID_CONFIG",
            message="conference source needs name and venue_id",
            fatal=True,
        )

    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    store.ensure_venue(source, venue_id)
    state = store.venue_state(source)
    assert state is not None

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
        return SourceResult(
            source=source,
            outcome="NOT_DUE",
            briefing_path=flushed,
            message=message,
        )

    mode = _select_mode(
        state,
        full_rescan_interval_days=float(cfg.get("full_rescan_interval_days", 7)),
        now_ms=now_ms,
    )
    page_size = max(1, min(int(cfg.get("page_size", 1000) or 1000), 1000))
    run: SyncRun | None = None

    try:
        capabilities = provider.discover_venue()
        store.ensure_venue(source, venue_id, capabilities.submission_invitation)
        min_cdate = _min_cdate(
            state,
            mode=mode,
            overlap_hours=float(cfg.get("watermark_overlap_hours", 48)),
        )
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
        total = run.total_expected
        for page in provider.iter_submission_pages(
            capabilities,
            min_cdate=min_cdate,
            after_id=run.cursor_after,
            page_size=page_size,
            total_hint=run.total_expected,
        ):
            candidate = max(
                (int(paper.get("cdate") or 0) for paper in page.papers),
                default=None,
            )
            new_count += store.commit_page(
                run_id=run.run_id,
                papers=page.papers,
                cursor_after=page.cursor_after,
                candidate_watermark_ms=candidate,
                total_expected=page.total,
                raw_count=page.raw_count,
                now_ms=now_ms,
            )
            if page.total is not None:
                total = page.total

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
        message = (
            f"{mode}: {new_count} new, " f"{total if total is not None else '?'} total"
        )
        if briefing_error:
            message = f"{message}; briefing failed: {briefing_error}"
        return SourceResult(
            source=source,
            outcome="SUCCESS" if not briefing_error else "SUCCESS_BRIEFING_FAILED",
            new_papers=new_count,
            briefing_path=flushed,
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
            message = f"{message}; pending briefing failed: {one_line(str(flush_exc), _secret())}"
            flushed = None
        return SourceResult(
            source=source,
            outcome="NOT_OPEN",
            briefing_path=flushed,
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
