from conference_store import ConferenceStore
from conference_sync import sync_source
from openreview_provider import (
    OpenReviewNotPublic,
    SubmissionPage,
    VenueCapabilities,
)

SOURCE = {
    "name": "iclr",
    "display_name": "ICLR 2027",
    "category": "conference",
    "type": "api",
    "provider": "openreview",
    "venue_id": "ICLR.cc/2027/Conference",
    "enabled": True,
}
DEFAULTS = {
    "conference_defaults": {
        "page_size": 1000,
        "poll_interval_hours": 24,
        "full_rescan_interval_days": 7,
        "watermark_overlap_hours": 48,
    }
}
CAPABILITIES = VenueCapabilities(
    venue_id="ICLR.cc/2027/Conference",
    submission_invitation="ICLR.cc/2027/Conference/-/Submission",
    submission_venue_id="ICLR.cc/2027/Conference/Submission",
)


def _paper(index, cdate):
    return {
        "forum_id": f"forum-{index}",
        "note_id": f"note-{index}",
        "title": f"Paper {index}",
        "abstract": "",
        "authors": ["A"],
        "keywords": ["water"],
        "venue": "ICLR 2027",
        "venue_id": "ICLR.cc/2027/Conference/Submission",
        "status": "under_review",
        "pdf": "",
        "landing_url": f"https://openreview.net/forum?id=forum-{index}",
        "cdate": cdate,
        "mdate": cdate,
    }


def _page(papers, cursor, total=None, number=1):
    return SubmissionPage(
        papers=papers,
        cursor_after=cursor,
        total=total,
        page_number=number,
        raw_count=len(papers),
    )


class FakeProvider:
    def __init__(self, pages, *, discover_error=None):
        self.pages = pages
        self.discover_error = discover_error
        self.discover_calls = 0
        self.page_calls = []

    def discover_venue(self):
        self.discover_calls += 1
        if self.discover_error is not None:
            raise self.discover_error
        return CAPABILITIES

    def iter_submission_pages(
        self,
        capabilities,
        min_cdate=None,
        after_id=None,
        page_size=1000,
        total_hint=None,
        **kwargs,
    ):
        self.page_calls.append(
            {
                "min_cdate": min_cdate,
                "after_id": after_id,
                "page_size": page_size,
                "total_hint": total_hint,
                **kwargs,
            }
        )
        yield from self.pages


def _sync(
    tmp_path,
    provider,
    *,
    now_ms=10_000,
    force=True,
    defaults=None,
    date="2026-11-07",
    store=None,
):
    store = store or ConferenceStore(tmp_path / "conference.sqlite3")
    result = sync_source(
        source_cfg=SOURCE,
        defaults=defaults or DEFAULTS,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date=date,
        now_ms=now_ms,
        force=force,
    )
    return store, result


def test_sync_stores_pages_and_advances_watermark(tmp_path):
    provider = FakeProvider(
        [_page([_paper(1, 11_000), _paper(2, 12_000)], "forum-2", total=2)]
    )
    store, result = _sync(tmp_path, provider)

    assert result.outcome == "SUCCESS"
    assert result.new_papers == 2
    assert store.papers_count("iclr") == 2
    assert store.venue_state("iclr").watermark_ms == 12_000
    assert result.briefing_path is not None
    assert "Paper 1" in (
        tmp_path / "briefings" / "conference" / "iclr_2026-11-07.md"
    ).read_text(encoding="utf-8")
    store.close()


def test_failed_page_does_not_advance_watermark_and_resumes(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")

    class FlakyProvider(FakeProvider):
        def iter_submission_pages(self, *args, **kwargs):
            after_id = kwargs.get("after_id")
            self.page_calls.append({"after_id": after_id})
            if after_id == "forum-1":
                yield _page([_paper(2, 12_000)], "forum-2", total=2, number=2)
                return
            yield _page([_paper(1, 11_000)], "forum-1", total=2, number=1)
            raise RuntimeError("page 2 exploded")

    first = sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=FlakyProvider([]),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=10_000,
        force=True,
    )

    assert first.fatal is True
    assert first.outcome == "API_ERROR"
    assert store.papers_count("iclr") == 1
    assert store.venue_state("iclr").watermark_ms is None
    run = store.last_run("iclr")
    assert run.cursor_after == "forum-1"

    retry_provider = FlakyProvider([])
    second = sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=retry_provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=20_000,
        force=True,
    )

    assert second.outcome == "SUCCESS"
    assert retry_provider.page_calls[0]["after_id"] == "forum-1"
    assert store.papers_count("iclr") == 2
    assert store.venue_state("iclr").watermark_ms == 12_000
    store.close()


def test_not_due_skips_the_api_and_flushes_pending(tmp_path):
    provider = FakeProvider([_page([_paper(1, 11_000)], "forum-1", total=1)])
    store, first = _sync(tmp_path, provider, now_ms=10_000)
    assert first.outcome == "SUCCESS"

    calls_before = provider.discover_calls
    second = sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=10_000 + 3_600_000,
        force=False,
    )

    assert second.outcome == "NOT_DUE"
    assert provider.discover_calls == calls_before
    store.close()


def test_incremental_run_uses_overlap_window(tmp_path):
    start = 1_700_000_000_000
    provider = FakeProvider([_page([_paper(1, start + 1_000)], "forum-1", total=1)])
    store, first = _sync(tmp_path, provider, now_ms=start)
    assert first.outcome == "SUCCESS"

    incremental = FakeProvider([])
    sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=incremental,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=start + 86_400_000,
        force=True,
    )

    assert incremental.page_calls[0]["min_cdate"] == (start + 1_000) - 48 * 3_600_000
    store.close()


def test_full_rescan_inserts_missing_without_duplicates(tmp_path):
    first_provider = FakeProvider([_page([_paper(1, 11_000)], "forum-1", total=1)])
    store, _ = _sync(tmp_path, first_provider, now_ms=10_000)

    defaults = {
        "conference_defaults": {
            **DEFAULTS["conference_defaults"],
            "full_rescan_interval_days": 0,
        }
    }
    rescan = FakeProvider(
        [_page([_paper(1, 11_000), _paper(2, 12_000)], "forum-2", total=2)]
    )
    _, result = _sync(tmp_path, rescan, now_ms=20_000, defaults=defaults)

    assert result.outcome == "SUCCESS"
    assert store.papers_count("iclr") == 2
    store.close()


def test_not_open_is_non_fatal_and_does_not_advance_watermark(tmp_path):
    provider = FakeProvider([], discover_error=OpenReviewNotPublic("not open"))
    store, result = _sync(tmp_path, provider)

    assert result.outcome == "NOT_OPEN"
    assert result.fatal is False
    assert store.venue_state("iclr").watermark_ms is None
    store.close()


def test_auth_failure_is_fatal_and_does_not_advance_watermark(tmp_path):
    provider = FakeProvider(
        [], discover_error=RuntimeError("Challenge verification required")
    )
    store, result = _sync(tmp_path, provider)

    assert result.outcome == "AUTH_REQUIRED"
    assert result.fatal is True
    assert store.venue_state("iclr").watermark_ms is None
    store.close()


def test_bootstrap_writes_baseline_and_briefs_only_new_papers(tmp_path):
    provider = FakeProvider(
        [
            _page(
                [_paper(1, 5_000), _paper(2, 20_000)],
                "forum-2",
                total=2,
            )
        ]
    )
    store, result = _sync(tmp_path, provider, now_ms=10_000)

    assert result.outcome == "SUCCESS"
    baseline = tmp_path / "briefings" / "conference" / "iclr_baseline_2026-11-07.md"
    briefing = tmp_path / "briefings" / "conference" / "iclr_2026-11-07.md"
    assert baseline.exists()
    assert "首次同步已入库 2 篇投稿" in baseline.read_text(encoding="utf-8")
    text = briefing.read_text(encoding="utf-8")
    assert "Paper 2" in text
    assert "Paper 1" not in text
    assert store.pending_papers("iclr") == []
    store.close()


def test_briefing_failure_leaves_rows_pending_but_keeps_watermark(
    tmp_path, monkeypatch
):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    first = FakeProvider([_page([_paper(1, 11_000)], "forum-1", total=1)])
    first_result = sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=first,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-06",
        now_ms=10_000,
        force=True,
    )
    assert first_result.outcome == "SUCCESS"

    import conference_sync

    def boom(_path, _content):
        raise OSError("disk full")

    monkeypatch.setattr(conference_sync, "write_briefing", boom)
    second = FakeProvider([_page([_paper(2, 12_000)], "forum-2", total=1)])
    result = sync_source(
        source_cfg=SOURCE,
        defaults=DEFAULTS,
        store=store,
        provider=second,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=10_000 + 86_400_000,
        force=True,
    )

    assert result.outcome == "SUCCESS_BRIEFING_FAILED"
    assert store.papers_count("iclr") == 2
    assert [paper["forum_id"] for paper in store.pending_papers("iclr")] == ["forum-2"]
    assert store.venue_state("iclr").watermark_ms == 12_000
    store.close()


def test_unknown_source_config_is_fatal(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    result = sync_source(
        source_cfg={"name": "", "venue_id": ""},
        defaults=DEFAULTS,
        store=store,
        provider=FakeProvider([]),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=10_000,
    )

    assert result.fatal is True
    assert result.outcome == "INVALID_CONFIG"
    store.close()


REVIEW_DEFAULTS = {
    "conference_defaults": {
        **DEFAULTS["conference_defaults"],
        "reviews": {
            "enabled": True,
            "sample_limit": 2,
            "page_size": 2,
            "request_interval_seconds": 0,
            "max_pages_per_run": 100,
            "rescan_interval_days": 7,
            "rating_options": ["1", "2", "3", "4"],
            "min_reviews_for_signal": 1,
        },
    }
}


def _reply(index: int, forum_id: str, *, readers=("everyone",), rating="4"):
    return {
        "id": f"reply-{index}",
        "reply_id": f"reply-{index}",
        "forum_id": forum_id,
        "invitations": ["ICLR.cc/2027/Conference/Submission1/-/Official_Review"],
        "readers": list(readers),
        "content": {
            "rating": {"value": rating},
            "confidence": {"value": "3"},
            "paper_summary": {"value": f"summary {index}"},
        },
        "cdate": 1_000 + index,
        "mdate": 1_000 + index,
    }


def _page_with_replies(papers, replies, cursor, total=None, number=1):
    return SubmissionPage(
        papers=papers,
        cursor_after=cursor,
        total=total,
        page_number=number,
        raw_count=len(papers),
        replies=replies,
    )


def _bootstrap(store, tmp_path, *, now_ms=10_000):
    provider = FakeProvider([_page([_paper(1, 11_000)], "forum-1", total=1)])
    return _sync(
        tmp_path,
        provider,
        now_ms=now_ms,
        defaults=DEFAULTS,
        store=store,
    )


def _sample(store, tmp_path, *, now_ms=20_000, defaults=None):
    provider = FakeProvider(
        [
            _page_with_replies(
                [_paper(1, 11_000)],
                [_reply(1, "forum-1"), _reply(2, "forum-1")],
                "forum-1",
                total=1,
            )
        ]
    )
    result = sync_source(
        source_cfg=SOURCE,
        defaults=defaults or REVIEW_DEFAULTS,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=now_ms,
        force=True,
    )
    return provider, result


def test_review_sample_uses_latest_n_and_marks_completed(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)

    provider, result = _sample(store, tmp_path)

    assert result.outcome == "SUCCESS"
    assert result.new_replies == 2
    call = provider.page_calls[0]
    assert call["sort"] == "cdate"
    assert call["min_cdate"] is None
    assert call["page_size"] == 2
    assert call["include_replies"] is True
    state = store.venue_state("iclr")
    assert state.reviews_sample_completed_ms == 20_000
    assert store.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2
    store.close()


def test_review_sample_does_not_repeat(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path)
    sample_completed = store.venue_state("iclr").reviews_sample_completed_ms

    provider = FakeProvider([])
    result = sync_source(
        source_cfg=SOURCE,
        defaults=REVIEW_DEFAULTS,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 86_400_000,
        force=True,
    )

    assert result.outcome == "SUCCESS"
    assert provider.page_calls[0]["sort"] == "id"
    assert provider.page_calls[0]["include_replies"] is True
    assert store.venue_state("iclr").reviews_sample_completed_ms == sample_completed
    store.close()


def test_review_rescan_uses_watch_from_cutoff(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path)
    watch_from = store.venue_state("iclr").reviews_watch_from_ms

    defaults = {
        "conference_defaults": {
            **REVIEW_DEFAULTS["conference_defaults"],
            "reviews": {
                **REVIEW_DEFAULTS["conference_defaults"]["reviews"],
                "rescan_interval_days": 0,
            },
        }
    }
    provider = FakeProvider([])
    result = sync_source(
        source_cfg=SOURCE,
        defaults=defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 1_000,
        force=True,
    )

    assert result.outcome == "SUCCESS"
    assert provider.page_calls[0]["min_cdate"] == watch_from
    assert store.venue_state("iclr").last_review_rescan_ms is not None
    store.close()


class PagedProvider(FakeProvider):
    def __init__(self, pages_by_after):
        super().__init__([])
        self.pages_by_after = pages_by_after
        self.sequence = [None, "forum-2"]

    def iter_submission_pages(self, capabilities, after_id=None, **kwargs):
        self.page_calls.append({"after_id": after_id, **kwargs})
        keys = [None, "forum-2"] if after_id is None else [after_id]
        for key in keys:
            page = self.pages_by_after.get(key)
            if page is not None:
                yield page


def test_max_pages_pauses_and_next_run_resumes_cursor(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path)
    defaults = {
        "conference_defaults": {
            **REVIEW_DEFAULTS["conference_defaults"],
            "reviews": {
                **REVIEW_DEFAULTS["conference_defaults"]["reviews"],
                "rescan_interval_days": 0,
                "max_pages_per_run": 1,
            },
        }
    }
    provider = PagedProvider(
        {
            None: _page_with_replies(
                [_paper(2, 12_000)],
                [_reply(3, "forum-2")],
                "forum-2",
                total=2,
            ),
            "forum-2": _page_with_replies(
                [_paper(3, 13_000)],
                [_reply(4, "forum-3")],
                "forum-3",
                total=2,
            ),
        }
    )

    first = sync_source(
        source_cfg=SOURCE,
        defaults=defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 1_000,
        force=True,
    )

    assert first.outcome == "PAUSED"
    run = store.last_run("iclr")
    assert run.status == "RUNNING"
    assert run.cursor_after == "forum-2"
    assert store.venue_state("iclr").watermark_ms == 11_000
    assert store.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3

    resume_defaults = {
        "conference_defaults": {
            **defaults["conference_defaults"],
            "reviews": {
                **defaults["conference_defaults"]["reviews"],
                "max_pages_per_run": 2,
            },
        }
    }
    second = sync_source(
        source_cfg=SOURCE,
        defaults=resume_defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 2_000,
        force=True,
    )

    assert second.outcome == "SUCCESS"
    assert provider.page_calls[-1]["after_id"] == "forum-2"
    assert store.venue_state("iclr").watermark_ms == 13_000
    assert store.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 4
    store.close()


def test_request_interval_between_review_pages(tmp_path, monkeypatch):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path)
    defaults = {
        "conference_defaults": {
            **REVIEW_DEFAULTS["conference_defaults"],
            "reviews": {
                **REVIEW_DEFAULTS["conference_defaults"]["reviews"],
                "rescan_interval_days": 0,
                "request_interval_seconds": 0.5,
            },
        }
    }
    provider = PagedProvider(
        {
            None: _page_with_replies(
                [_paper(2, 12_000)],
                [_reply(3, "forum-2")],
                "forum-2",
                total=2,
            ),
            "forum-2": _page_with_replies(
                [_paper(3, 13_000)],
                [_reply(4, "forum-3")],
                "forum-3",
                total=2,
            ),
        }
    )
    waits = []
    monkeypatch.setattr("conference_sync.time.sleep", waits.append)

    result = sync_source(
        source_cfg=SOURCE,
        defaults=defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 1_000,
        force=True,
    )

    assert result.outcome == "SUCCESS"
    assert waits == [0.5, 0.5]
    store.close()


def test_incremental_reviews_never_reach_before_watch_from(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path)
    watch_from = store.venue_state("iclr").reviews_watch_from_ms

    provider = FakeProvider([])
    result = sync_source(
        source_cfg=SOURCE,
        defaults=REVIEW_DEFAULTS,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=20_000 + 86_400_000,
        force=True,
    )

    assert result.outcome == "SUCCESS"
    assert provider.page_calls[0]["min_cdate"] == watch_from
    store.close()


def test_full_rescan_is_not_starved_by_review_rescan(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    _bootstrap(store, tmp_path)
    _sample(store, tmp_path, now_ms=100_000_000)
    defaults = {
        "conference_defaults": {
            **REVIEW_DEFAULTS["conference_defaults"],
            "full_rescan_interval_days": 1,
            "reviews": {
                **REVIEW_DEFAULTS["conference_defaults"]["reviews"],
                "rescan_interval_days": 0,
            },
        }
    }
    provider = FakeProvider([])

    first = sync_source(
        source_cfg=SOURCE,
        defaults=defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=100_000_001,
        force=True,
    )
    assert first.outcome == "SUCCESS"
    assert store.last_run("iclr").mode == "full"

    second = sync_source(
        source_cfg=SOURCE,
        defaults=defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-08",
        now_ms=100_000_002,
        force=True,
    )
    assert second.outcome == "SUCCESS"
    assert store.last_run("iclr").mode == "review_rescan"
    store.close()


def test_bootstrap_page_cap_pauses_and_resumes(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    capped = {
        "conference_defaults": {
            **DEFAULTS["conference_defaults"],
            "max_pages_per_run": 1,
        }
    }
    provider = PagedProvider(
        {
            None: _page([_paper(1, 11_000)], "forum-1", total=2),
            "forum-1": _page([_paper(2, 12_000)], "forum-2", total=2),
        }
    )
    provider.sequence = [None, "forum-1"]

    first = sync_source(
        source_cfg=SOURCE,
        defaults=capped,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=10_000,
        force=True,
    )

    assert first.outcome == "PAUSED"
    run = store.last_run("iclr")
    assert run.mode == "bootstrap"
    assert run.cursor_after == "forum-1"
    assert store.venue_state("iclr").bootstrap_completed_ms is None

    resume_defaults = {
        "conference_defaults": {
            **DEFAULTS["conference_defaults"],
            "max_pages_per_run": 2,
        }
    }
    second = sync_source(
        source_cfg=SOURCE,
        defaults=resume_defaults,
        store=store,
        provider=provider,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=20_000,
        force=True,
    )

    assert second.outcome == "SUCCESS"
    assert provider.page_calls[-1]["after_id"] == "forum-1"
    assert store.venue_state("iclr").bootstrap_completed_ms == 20_000
    assert (
        tmp_path / "briefings" / "conference" / "iclr_baseline_2026-11-07.md"
    ).exists()
    store.close()
