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
    ):
        self.page_calls.append(
            {
                "min_cdate": min_cdate,
                "after_id": after_id,
                "page_size": page_size,
                "total_hint": total_hint,
            }
        )
        yield from self.pages


def _sync(
    tmp_path, provider, *, now_ms=10_000, force=True, defaults=None, date="2026-11-07"
):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
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
