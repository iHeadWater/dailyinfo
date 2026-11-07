import sqlite3

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
        ).new_papers
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
        ).new_papers
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


V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE venues (
    source TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    submission_invitation TEXT NOT NULL DEFAULT '',
    last_poll_ms INTEGER,
    last_full_sync_ms INTEGER,
    watermark_ms INTEGER,
    bootstrap_completed_ms INTEGER,
    last_outcome TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE papers (
    source TEXT NOT NULL,
    forum_id TEXT NOT NULL,
    note_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    abstract TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    venue TEXT NOT NULL DEFAULT '',
    venueid TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    pdf TEXT NOT NULL DEFAULT '',
    landing_url TEXT NOT NULL DEFAULT '',
    cdate INTEGER NOT NULL DEFAULT 0,
    mdate INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    first_seen_ms INTEGER NOT NULL,
    first_seen_run TEXT NOT NULL,
    last_seen_ms INTEGER NOT NULL,
    briefed_date TEXT,
    PRIMARY KEY (source, forum_id)
);
CREATE TABLE sync_runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'fetching',
    status TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    invitation TEXT NOT NULL DEFAULT '',
    base_watermark_ms INTEGER,
    cursor_after TEXT,
    watermark_candidate_ms INTEGER,
    total_expected INTEGER,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    scanned_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    started_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    heartbeat_ms INTEGER NOT NULL
);
"""


def _create_v1_database(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
    conn.execute(
        "INSERT INTO venues(source, venue_id, submission_invitation, "
        "watermark_ms, bootstrap_completed_ms) VALUES(?, ?, ?, ?, ?)",
        ("iclr", "ICLR.cc/2027/Conference", "inv", 1234, 1000),
    )
    conn.execute(
        "INSERT INTO papers(source, forum_id, title, cdate, first_seen_ms, "
        "first_seen_run, last_seen_ms, briefed_date) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        ("iclr", "forum-1", "Paper 1", 1, 10, "run-1", 20, "baseline"),
    )
    conn.commit()
    conn.close()


def _review(index: int, forum_id: str | None = None):
    forum = forum_id or f"forum-{index}"
    return {
        "reply_id": f"reply-{index}",
        "forum_id": forum,
        "invitation": "ICLR.cc/2027/Conference/Submission1/-/Official_Review",
        "reply_type": "review",
        "rating": "3",
        "confidence": "4",
        "soundness": "2",
        "presentation": "3",
        "informativeness": "2",
        "summary": "summary",
        "strengths": "strengths",
        "weaknesses": "weaknesses",
        "questions": "questions",
        "decision": "",
        "raw_json": '{"id": "reply-%d"}' % index,
        "content_hash": f"hash-{index}",
        "reply_cdate": 1000 + index,
        "reply_mdate": 2000 + index,
    }


def _summary(forum_id: str = "forum-1"):
    return {
        "forum_id": forum_id,
        "review_count": 1,
        "rating_count": 1,
        "rating_mean": 0.667,
        "rating_median": 0.667,
        "rating_std": 0.0,
        "rating_min": 0.667,
        "rating_max": 0.667,
        "rating_range": 0.0,
        "confidence_mean": 4.0,
        "rating_raw_values": ["3"],
        "confidence_raw_values": ["4"],
        "decision": "",
        "presentation": "poster",
        "strong_signal": False,
        "controversial": False,
    }


def test_v1_database_migrates_to_v3_and_preserves_papers(tmp_path):
    path = tmp_path / "conference.sqlite3"
    _create_v1_database(path)

    store = ConferenceStore(path)

    assert store.papers_count("iclr") == 1
    paper = store.conn.execute(
        "SELECT first_seen_ms, briefed_date FROM papers WHERE forum_id = 'forum-1'"
    ).fetchone()
    assert paper["first_seen_ms"] == 10
    assert paper["briefed_date"] == "baseline"
    assert (
        store.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        == "3"
    )
    venue_columns = {
        row["name"] for row in store.conn.execute("PRAGMA table_info(venues)")
    }
    assert {
        "reviews_watch_from_ms",
        "last_review_rescan_ms",
        "reviews_sample_completed_ms",
        "ai_initialized_ms",
    } <= venue_columns
    run_columns = {
        row["name"] for row in store.conn.execute("PRAGMA table_info(sync_runs)")
    }
    assert "new_replies" in run_columns
    store.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()
    store.conn.execute("SELECT COUNT(*) FROM paper_review_summary").fetchone()
    store.conn.execute("SELECT COUNT(*) FROM events").fetchone()
    store.close()


def test_v2_database_migrates_to_v3(tmp_path):
    path = tmp_path / "conference.sqlite3"
    store = ConferenceStore(path)
    store.conn.execute("DROP TABLE events")
    store.conn.execute("ALTER TABLE venues DROP COLUMN ai_initialized_ms")
    store.conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    store.conn.commit()
    store.close()

    migrated = ConferenceStore(path)

    assert (
        migrated.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        == "3"
    )
    venue_columns = {
        row["name"] for row in migrated.conn.execute("PRAGMA table_info(venues)")
    }
    assert "ai_initialized_ms" in venue_columns
    migrated.conn.execute("SELECT COUNT(*) FROM events").fetchone()
    migrated.close()


def test_review_upsert_preserves_first_seen_and_counts_new(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="incremental", now_ms=5_000)

    first = store.commit_page(
        run_id=run.run_id,
        papers=[],
        replies=[_review(1), _review(2)],
        summaries=[_summary()],
        cursor_after="forum-1",
        candidate_watermark_ms=2_000,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )
    assert first.new_replies == 2

    second = store.commit_page(
        run_id=run.run_id,
        papers=[],
        replies=[_review(1), _review(2)],
        summaries=[_summary()],
        cursor_after="forum-1",
        candidate_watermark_ms=2_000,
        total_expected=1,
        raw_count=1,
        now_ms=6_000,
    )
    assert second.new_replies == 0
    row = store.conn.execute(
        "SELECT first_seen_ms, last_seen_ms FROM reviews WHERE reply_id = 'reply-1'"
    ).fetchone()
    assert row["first_seen_ms"] == 5_000
    assert row["last_seen_ms"] == 6_000
    store.close()


def test_paper_review_summary_is_upserted(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="review_sample", now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[],
        replies=[_review(1)],
        summaries=[_summary("forum-1")],
        cursor_after="forum-1",
        candidate_watermark_ms=None,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )

    row = store.conn.execute(
        "SELECT * FROM paper_review_summary WHERE forum_id = 'forum-1'"
    ).fetchone()
    assert row["review_count"] == 1
    assert row["rating_mean"] == 0.667
    assert row["presentation"] == "poster"
    store.close()


def test_review_sample_finish_marks_sample_without_advancing_watermark(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="review_sample", now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[],
        replies=[_review(1)],
        summaries=[_summary()],
        cursor_after="forum-1",
        candidate_watermark_ms=9_000,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )

    store.finish_run(run.run_id, now_ms=6_000)

    state = store.venue_state("iclr")
    assert state.reviews_sample_completed_ms == 6_000
    assert state.watermark_ms is None
    store.close()


def test_review_rescan_finish_updates_rescan_timestamp_and_watermark(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, mode="review_rescan", now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[],
        replies=[_review(1)],
        summaries=[_summary()],
        cursor_after="forum-1",
        candidate_watermark_ms=9_000,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )

    store.finish_run(run.run_id, now_ms=6_000)

    state = store.venue_state("iclr")
    assert state.last_review_rescan_ms == 6_000
    assert state.watermark_ms == 9_000
    store.close()


def test_v1_tables_without_meta_row_still_migrate(tmp_path):
    path = tmp_path / "conference.sqlite3"
    _create_v1_database(path)
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM meta")
    conn.commit()
    conn.close()

    store = ConferenceStore(path)

    assert store.papers_count("iclr") == 1
    assert (
        store.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        == "3"
    )
    venue_columns = {
        row["name"] for row in store.conn.execute("PRAGMA table_info(venues)")
    }
    assert "reviews_watch_from_ms" in venue_columns
    assert "ai_initialized_ms" in venue_columns
    store.close()


def _event(index: int, forum_id: str | None = None, event_type="REVIEWS_CHANGED"):
    forum = forum_id or f"forum-{index}"
    return {
        "event_id": f"event-{index}",
        "source": "iclr",
        "forum_id": forum,
        "event_types": [event_type],
        "fingerprint": f"fingerprint-{index}",
        "created_ms": 5_000 + index,
    }


def test_commit_page_persists_events_and_supersedes_pending(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)

    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        replies=[_review(1)],
        summaries=[_summary()],
        events=[_event(1)],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )
    pending = store.pending_events("iclr", limit=10)
    assert [event["event_id"] for event in pending] == ["event-1"]

    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        replies=[_review(1)],
        summaries=[_summary()],
        events=[_event(2, "forum-1")],
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
        now_ms=6_000,
    )

    statuses = {
        row["event_id"]: row["status"]
        for row in store.conn.execute("SELECT event_id, status FROM events")
    }
    assert statuses == {"event-1": "superseded", "event-2": "pending"}
    assert [event["event_id"] for event in store.pending_events("iclr", 10)] == [
        "event-2"
    ]
    store.close()


def test_mark_rendered_clears_pending_events(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1)],
        replies=[_review(1)],
        summaries=[_summary()],
        events=[_event(1), _event(2)],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=2,
        raw_count=2,
        now_ms=5_000,
    )

    assert store.mark_rendered(["event-1", "event-2"], "briefing.md", now_ms=7_000) == 2

    assert store.pending_events("iclr", 10) == []
    row = store.conn.execute(
        "SELECT status, briefing_filename FROM events WHERE event_id = 'event-1'"
    ).fetchone()
    assert row["status"] == "rendered"
    assert row["briefing_filename"] == "briefing.md"
    store.close()


def test_forum_states_returns_paper_reviews_and_summary(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1), _paper(2)],
        replies=[_review(1)],
        summaries=[_summary()],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=2,
        raw_count=2,
        now_ms=5_000,
    )

    states = store.forum_states("iclr", ["forum-1", "forum-2", "missing"])

    assert set(states) == {"forum-1", "forum-2"}
    assert states["forum-1"]["paper"]["title"] == "Paper 1"
    assert states["forum-1"]["reviews"][0]["rating"] == "3"
    assert states["forum-1"]["summary"]["rating_raw_values"] == ["3"]
    assert states["forum-2"]["reviews"] == []
    assert states["forum-2"]["summary"] is None
    store.close()


def test_seed_review_events_queues_existing_review_papers_once(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1), _paper(2)],
        replies=[_review(1)],
        summaries=[_summary()],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=2,
        raw_count=2,
        now_ms=5_000,
    )

    assert store.seed_review_events("iclr", now_ms=6_000) == 1
    assert store.seed_review_events("iclr", now_ms=7_000) == 0
    pending = store.pending_events("iclr", 10)
    assert [event["forum_id"] for event in pending] == ["forum-1"]

    store.mark_ai_initialized("iclr", now_ms=8_000)
    assert store.venue_state("iclr").ai_initialized_ms == 8_000
    store.close()


def test_seed_review_events_honors_the_limit(tmp_path):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    run = _start(store, now_ms=5_000)
    store.commit_page(
        run_id=run.run_id,
        papers=[_paper(1), _paper(2)],
        replies=[_review(1), _review(2, "forum-2")],
        summaries=[_summary("forum-1"), _summary("forum-2")],
        cursor_after="forum-2",
        candidate_watermark_ms=2_000,
        total_expected=2,
        raw_count=2,
        now_ms=5_000,
    )

    assert store.seed_review_events("iclr", now_ms=6_000, limit=1) == 1
    assert len(store.pending_events("iclr", 10)) == 1
    store.close()


def test_v3_database_without_attempt_columns_is_upgraded(tmp_path):
    path = tmp_path / "conference.sqlite3"
    store = ConferenceStore(path)
    store.conn.execute("ALTER TABLE events DROP COLUMN attempts")
    store.conn.execute("ALTER TABLE events DROP COLUMN last_error")
    store.conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    store.conn.commit()
    store.close()

    reopened = ConferenceStore(path)
    columns = {
        row["name"] for row in reopened.conn.execute("PRAGMA table_info(events)")
    }
    assert {"attempts", "last_error"} <= columns
    reopened.close()
