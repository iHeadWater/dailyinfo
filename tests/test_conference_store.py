import pytest

from conference_store import ConferenceSchemaError, ConferenceStore


def _paper(index: int, cdate: int | None = None):
    return {
        "forum_id": f"forum-{index}",
        "note_id": f"note-{index}",
        "title": f"Paper {index}",
        "abstract": f"Abstract {index}",
        "authors": ["A", "B"],
        "keywords": ["hydrology"],
        "venue": "ICLR 2027",
        "venue_id": "ICLR.cc/2027/Conference/Submission",
        "status": "under_review",
        "pdf": f"https://openreview.net/pdf/{index}.pdf",
        "landing_url": f"https://openreview.net/forum?id=forum-{index}",
        "cdate": cdate if cdate is not None else index * 1000,
        "mdate": index * 1000,
    }


def _start(store, *, mode="incremental", config_hash="cfg", now_ms=10_000):
    store.ensure_venue("iclr", "ICLR.cc/2027/Conference", "inv")
    return store.start_run(
        source="iclr",
        venue_id="ICLR.cc/2027/Conference",
        invitation="inv",
        mode=mode,
        config_hash=config_hash,
        now_ms=now_ms,
    )


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "conference.sqlite3"
    store = ConferenceStore(path)
    store.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    store.conn.commit()
    store.close()

    with pytest.raises(ConferenceSchemaError, match="newer than this code"):
        ConferenceStore(path)


def test_upsert_is_idempotent_and_preserves_first_seen(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)

    assert (
        store.commit_page(
            run_id=run.run_id,
            papers=[_paper(1), _paper(2)],
            cursor_after="forum-2",
            candidate_watermark_ms=2_000,
            total_expected=2,
            raw_count=2,
            now_ms=5_000,
        )
        == 2
    )
    assert store.papers_count("iclr") == 2

    assert (
        store.commit_page(
            run_id=run.run_id,
            papers=[_paper(1), _paper(2)],
            cursor_after="forum-2",
            candidate_watermark_ms=2_000,
            total_expected=2,
            raw_count=2,
            now_ms=6_000,
        )
        == 0
    )
    assert store.papers_count("iclr") == 2
    row = store.conn.execute(
        "SELECT first_seen_ms, last_seen_ms FROM papers WHERE forum_id = 'forum-1'"
    ).fetchone()
    assert row["first_seen_ms"] == 5_000
    assert row["last_seen_ms"] == 6_000
    store.close()


def test_watermark_advances_only_on_finish(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
        now_ms=1_000,
    )
    assert store.venue_state("iclr").watermark_ms is None

    store.finish_run(run.run_id, now_ms=2_000)

    state = store.venue_state("iclr")
    assert state.watermark_ms == 1_000
    assert state.last_poll_ms == 2_000
    assert state.bootstrap_completed_ms is None
    store.close()


def test_failed_run_does_not_advance_watermark_and_resumes_cursor(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
    )
    store.fail_run(run.run_id, "API_ERROR", "boom", now_ms=2_000)

    state = store.venue_state("iclr")
    assert state.watermark_ms is None
    assert state.last_outcome == "API_ERROR"

    resumed = _start(store, now_ms=3_000)
    assert resumed.run_id == run.run_id
    assert resumed.cursor_after == "forum-1"
    assert store.papers_count("iclr") == 1
    store.close()


def test_different_config_obsoletes_running_run(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    first = _start(store, config_hash="cfg-a")
    second = _start(store, config_hash="cfg-b", now_ms=10_001)

    assert second.run_id != first.run_id
    row = store.conn.execute(
        "SELECT status FROM sync_runs WHERE run_id = ?", (first.run_id,)
    ).fetchone()
    assert row["status"] == "OBSOLETE"
    store.close()


def test_bootstrap_marks_old_papers_baseline_and_keeps_new_pending(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="bootstrap", now_ms=10_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1, cdate=5_000), _paper(2, cdate=20_000)],
        cursor_after="forum-2",
        candidate_watermark_ms=20_000,
        total_expected=2,
        raw_count=2,
        now_ms=10_000,
    )
    store.finish_run(run.run_id, now_ms=10_000)

    pending = store.pending_papers("iclr")
    assert [paper["forum_id"] for paper in pending] == ["forum-2"]
    assert store.venue_state("iclr").bootstrap_completed_ms == 10_000
    store.close()


def test_full_rescan_never_deletes(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    first = _start(store, mode="bootstrap", config_hash="first")
    store.commit_page(
        run_id=first.run_id,
        papers=[_paper(1)],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
    )
    store.finish_run(first.run_id)
    second = _start(store, mode="full", config_hash="second", now_ms=20_000)
    store.commit_page(
        run_id=second.run_id,
        papers=[_paper(2)],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=1,
        raw_count=1,
    )
    store.finish_run(second.run_id)

    assert store.papers_count("iclr") == 2
    store.close()


def test_mark_briefed_only_touches_named_pending_rows(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1), _paper(2)],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=2,
        raw_count=2,
    )

    assert store.mark_briefed("iclr", "2026-11-07", ["forum-1"]) == 1
    pending = store.pending_papers("iclr")
    assert [paper["forum_id"] for paper in pending] == ["forum-2"]
    store.close()


def test_venue_change_resets_watermarks_but_keeps_papers(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="bootstrap", config_hash="one")
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
    )
    store.finish_run(run.run_id)
    assert store.venue_state("iclr").bootstrap_completed_ms is not None

    store.ensure_venue("iclr", "ICLR.cc/2028/Conference", "inv2")

    state = store.venue_state("iclr")
    assert state.venue_id == "ICLR.cc/2028/Conference"
    assert state.watermark_ms is None
    assert state.bootstrap_completed_ms is None
    assert store.papers_count("iclr") == 1
    store.close()


def test_unknown_run_raises(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    with pytest.raises(Exception, match="unknown sync run"):
        store.commit_page(
            run_id="missing",
            papers=[],
            cursor_after=None,
            candidate_watermark_ms=None,
            total_expected=None,
            raw_count=0,
        )
    store.close()
