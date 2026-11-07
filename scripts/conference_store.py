"""SQLite persistence for conference submissions.

The store is deliberately small: submissions are the only work item, so the
``papers`` table is both the durable queue and the record of what has been
briefed.  The two guarantees the ingestion path depends on live here:

* a page of papers and the pagination cursor commit in the same transaction;
* a run's watermark advances only in :meth:`finish_run`, which the sync layer
  calls after the page iterator is exhausted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any, Iterable

SCHEMA_VERSION = 3
# A RUNNING row whose heartbeat is older than this is assumed dead.  The sync
# layer resumes from its committed cursor instead of starting over.
STALE_RUN_MS = 5 * 60 * 1000
_BRIEF_CHUNK = 500


class ConferenceStoreError(RuntimeError):
    """Base error for the conference store."""


class ConferenceSchemaError(ConferenceStoreError):
    """Raised when the database was written by a newer schema version."""


@dataclass(frozen=True)
class VenueState:
    source: str
    venue_id: str
    submission_invitation: str
    last_poll_ms: int | None
    last_full_sync_ms: int | None
    watermark_ms: int | None
    bootstrap_completed_ms: int | None
    reviews_watch_from_ms: int | None
    last_review_rescan_ms: int | None
    reviews_sample_completed_ms: int | None
    ai_initialized_ms: int | None
    last_outcome: str
    last_message: str


@dataclass(frozen=True)
class PageCommit:
    """What one page added, so the sync layer can log and resume."""

    new_papers: int
    new_replies: int


@dataclass(frozen=True)
class SyncRun:
    run_id: str
    source: str
    venue_id: str
    mode: str
    status: str
    config_hash: str
    invitation: str
    cursor_after: str | None
    watermark_candidate_ms: int | None
    total_expected: int | None
    new_replies: int
    started_ms: int
    heartbeat_ms: int


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(paper: dict) -> str:
    return hashlib.sha256(_json(paper).encode("utf-8")).hexdigest()


class ConferenceStore:
    """Own the conference SQLite database and its schema."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        # executescript commits any open transaction first, so schema setup
        # deliberately uses the connection's own context manager rather than
        # _tx().
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS venues (
                    source TEXT PRIMARY KEY,
                    venue_id TEXT NOT NULL,
                    submission_invitation TEXT NOT NULL DEFAULT '',
                    last_poll_ms INTEGER,
                    last_full_sync_ms INTEGER,
                    watermark_ms INTEGER,
                    bootstrap_completed_ms INTEGER,
                    reviews_watch_from_ms INTEGER,
                    last_review_rescan_ms INTEGER,
                    reviews_sample_completed_ms INTEGER,
                    ai_initialized_ms INTEGER,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    last_message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS papers (
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
                CREATE TABLE IF NOT EXISTS reviews (
                    source TEXT NOT NULL,
                    reply_id TEXT NOT NULL,
                    forum_id TEXT NOT NULL,
                    invitation TEXT NOT NULL DEFAULT '',
                    reply_type TEXT NOT NULL DEFAULT 'other',
                    rating TEXT NOT NULL DEFAULT '',
                    confidence TEXT NOT NULL DEFAULT '',
                    soundness TEXT NOT NULL DEFAULT '',
                    presentation TEXT NOT NULL DEFAULT '',
                    informativeness TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    strengths TEXT NOT NULL DEFAULT '',
                    weaknesses TEXT NOT NULL DEFAULT '',
                    questions TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL DEFAULT '',
                    reply_cdate INTEGER NOT NULL DEFAULT 0,
                    reply_mdate INTEGER NOT NULL DEFAULT 0,
                    first_seen_ms INTEGER NOT NULL,
                    first_seen_run TEXT NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    PRIMARY KEY (source, reply_id)
                );
                CREATE TABLE IF NOT EXISTS paper_review_summary (
                    source TEXT NOT NULL,
                    forum_id TEXT NOT NULL,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    rating_count INTEGER NOT NULL DEFAULT 0,
                    rating_mean REAL,
                    rating_median REAL,
                    rating_std REAL,
                    rating_min REAL,
                    rating_max REAL,
                    rating_range REAL,
                    confidence_mean REAL,
                    rating_raw_values_json TEXT NOT NULL DEFAULT '[]',
                    confidence_raw_values_json TEXT NOT NULL DEFAULT '[]',
                    decision TEXT NOT NULL DEFAULT '',
                    presentation TEXT NOT NULL DEFAULT '',
                    strong_signal INTEGER NOT NULL DEFAULT 0,
                    controversial INTEGER NOT NULL DEFAULT 0,
                    updated_ms INTEGER NOT NULL,
                    PRIMARY KEY (source, forum_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    forum_id TEXT NOT NULL,
                    event_types_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    briefing_filename TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_runs (
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
                    new_replies INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    started_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    heartbeat_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS papers_pending
                    ON papers(source, briefed_date, first_seen_ms);
                CREATE INDEX IF NOT EXISTS papers_cdate
                    ON papers(source, cdate);
                CREATE INDEX IF NOT EXISTS reviews_forum
                    ON reviews(source, forum_id);
                CREATE INDEX IF NOT EXISTS reviews_type
                    ON reviews(source, reply_type);
                CREATE INDEX IF NOT EXISTS sync_runs_source_status
                    ON sync_runs(source, status, updated_ms);
                CREATE INDEX IF NOT EXISTS events_pending
                    ON events(source, status, created_ms);
                CREATE INDEX IF NOT EXISTS events_forum
                    ON events(source, forum_id, status);
                """
            )
        # Keep this additive even for a v3 database created by an earlier
        # build of this branch (the event queue shipped before attempts/
        # last_error were added).
        with self._tx():
            self._add_column("events", "attempts", "INTEGER NOT NULL DEFAULT 0")
            self._add_column("events", "last_error", "TEXT NOT NULL DEFAULT ''")
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            # A v1 database could crash after creating its tables but before
            # writing the version row.  Treat "tables exist without the v2
            # column" as v1 instead of stamping it as a fresh v2 database and
            # skipping the additive migration.
            columns = {
                info["name"]
                for info in self.conn.execute("PRAGMA table_info(venues)").fetchall()
            }
            if columns and "reviews_watch_from_ms" not in columns:
                self._migrate_v1_to_v2()
                self._migrate_v2_to_v3()
            elif columns and "ai_initialized_ms" not in columns:
                self._migrate_v2_to_v3()
            else:
                with self._tx():
                    self.conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
            return
        try:
            found = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise ConferenceSchemaError(
                f"invalid conference schema version: {row['value']!r}"
            ) from exc
        if found > SCHEMA_VERSION:
            raise ConferenceSchemaError(
                f"conference database schema v{found} is newer than this "
                f"code (v{SCHEMA_VERSION}); refusing to write"
            )
        if found == 1:
            self._migrate_v1_to_v2()
            self._migrate_v2_to_v3()
        elif found == 2:
            self._migrate_v2_to_v3()
        elif found < 1:
            raise ConferenceSchemaError(
                f"conference database schema v{found} is older than any "
                f"supported version (v1)"
            )

    def _migrate_v1_to_v2(self) -> None:
        """Additive migration: no existing paper row is rewritten."""

        with self._tx():
            self._add_column("venues", "reviews_watch_from_ms", "INTEGER")
            self._add_column("venues", "last_review_rescan_ms", "INTEGER")
            self._add_column("venues", "reviews_sample_completed_ms", "INTEGER")
            self._add_column("sync_runs", "new_replies", "INTEGER NOT NULL DEFAULT 0")
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', '2') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )

    def _migrate_v2_to_v3(self) -> None:
        """Add the AI briefing event queue; no existing row is rewritten."""

        with self._tx():
            self._add_column("venues", "ai_initialized_ms", "INTEGER")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    forum_id TEXT NOT NULL,
                    event_types_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    briefing_filename TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS events_pending "
                "ON events(source, status, created_ms)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS events_forum "
                "ON events(source, forum_id, status)"
            )
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', '3') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )

    def _add_column(self, table: str, name: str, ddl: str) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Venues
    # ------------------------------------------------------------------
    def ensure_venue(
        self, source: str, venue_id: str, invitation: str = ""
    ) -> VenueState:
        """Create the venue row, resetting watermarks when the venue changes."""

        with self._tx():
            row = self.conn.execute(
                "SELECT * FROM venues WHERE source = ?", (source,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO venues(source, venue_id, submission_invitation) "
                    "VALUES(?, ?, ?)",
                    (source, venue_id, invitation),
                )
            elif row["venue_id"] != venue_id:
                # A different venue means the old watermark and bootstrap
                # state describe a different submission stream.  Keep the
                # historical papers (never delete), but start the stream anew.
                self.conn.execute(
                    "UPDATE venues SET venue_id = ?, submission_invitation = ?, "
                    "watermark_ms = NULL, bootstrap_completed_ms = NULL, "
                    "last_full_sync_ms = NULL, last_poll_ms = NULL, "
                    "reviews_watch_from_ms = NULL, last_review_rescan_ms = NULL, "
                    "reviews_sample_completed_ms = NULL, ai_initialized_ms = NULL "
                    "WHERE source = ?",
                    (venue_id, invitation, source),
                )
            elif invitation and row["submission_invitation"] != invitation:
                self.conn.execute(
                    "UPDATE venues SET submission_invitation = ? WHERE source = ?",
                    (invitation, source),
                )
        state = self.venue_state(source)
        assert state is not None
        return state

    def venue_state(self, source: str) -> VenueState | None:
        row = self.conn.execute(
            "SELECT * FROM venues WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return None
        return VenueState(
            source=row["source"],
            venue_id=row["venue_id"],
            submission_invitation=row["submission_invitation"],
            last_poll_ms=row["last_poll_ms"],
            last_full_sync_ms=row["last_full_sync_ms"],
            watermark_ms=row["watermark_ms"],
            bootstrap_completed_ms=row["bootstrap_completed_ms"],
            reviews_watch_from_ms=row["reviews_watch_from_ms"],
            last_review_rescan_ms=row["last_review_rescan_ms"],
            reviews_sample_completed_ms=row["reviews_sample_completed_ms"],
            ai_initialized_ms=row["ai_initialized_ms"],
            last_outcome=row["last_outcome"],
            last_message=row["last_message"],
        )

    def ensure_reviews_watch_from(self, source: str, now_ms: int) -> int:
        """Set the no-history cutoff once, then return the stored value."""

        with self._tx():
            self.conn.execute(
                "UPDATE venues SET reviews_watch_from_ms = ? "
                "WHERE source = ? AND reviews_watch_from_ms IS NULL",
                (now_ms, source),
            )
        state = self.venue_state(source)
        assert state is not None and state.reviews_watch_from_ms is not None
        return state.reviews_watch_from_ms

    def record_outcome(self, source: str, outcome: str, message: str = "") -> None:
        """Record a non-run outcome (NOT_DUE / NOT_OPEN) without moving state."""

        with self._tx():
            self.conn.execute(
                "UPDATE venues SET last_outcome = ?, last_message = ? WHERE source = ?",
                (outcome, message, source),
            )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def start_run(
        self,
        *,
        source: str,
        venue_id: str,
        invitation: str,
        mode: str,
        config_hash: str,
        now_ms: int | None = None,
    ) -> SyncRun:
        """Return a resumable run, or create a new one.

        A RUNNING run with the same ``config_hash`` is resumed from its
        committed ``cursor_after``.  A stale RUNNING run is resumed too: its
        heartbeat says the process is gone, not that its committed pages are
        invalid.  A run with a different config hash is marked OBSOLETE so the
        new mode cannot inherit a cursor that means something else.
        """

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._tx():
            self.conn.execute(
                "UPDATE sync_runs SET status = 'INTERRUPTED', "
                "message = 'stale heartbeat' "
                "WHERE source = ? AND status = 'RUNNING' AND heartbeat_ms < ?",
                (source, now_ms - STALE_RUN_MS),
            )
            active = self.conn.execute(
                "SELECT * FROM sync_runs WHERE source = ? "
                "AND status IN ('RUNNING', 'FAILED', 'INTERRUPTED') "
                "ORDER BY updated_ms DESC LIMIT 1",
                (source,),
            ).fetchone()
            if active is not None and active["config_hash"] == config_hash:
                # Resume from the committed cursor.  A failed run is not a
                # reason to re-read pages that are already durable.
                self.conn.execute(
                    "UPDATE sync_runs SET status = 'RUNNING', phase = 'fetching', "
                    "message = '', heartbeat_ms = ?, updated_ms = ? "
                    "WHERE run_id = ?",
                    (now_ms, now_ms, active["run_id"]),
                )
                row = self.conn.execute(
                    "SELECT * FROM sync_runs WHERE run_id = ?", (active["run_id"],)
                ).fetchone()
                return self._run_from_row(row)
            if active is not None and active["status"] == "RUNNING":
                self.conn.execute(
                    "UPDATE sync_runs SET status = 'OBSOLETE', message = ? "
                    "WHERE run_id = ?",
                    ("superseded by a different sync configuration", active["run_id"]),
                )
            run_id = uuid.uuid4().hex
            self.conn.execute(
                "INSERT INTO sync_runs(run_id, source, venue_id, mode, status, "
                "config_hash, invitation, base_watermark_ms, started_ms, "
                "updated_ms, heartbeat_ms) "
                "VALUES(?, ?, ?, ?, 'RUNNING', ?, ?, "
                "(SELECT watermark_ms FROM venues WHERE source = ?), ?, ?, ?)",
                (
                    run_id,
                    source,
                    venue_id,
                    mode,
                    config_hash,
                    invitation,
                    source,
                    now_ms,
                    now_ms,
                    now_ms,
                ),
            )
            row = self.conn.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._run_from_row(row)

    def _run_from_row(self, row: sqlite3.Row) -> SyncRun:
        return SyncRun(
            run_id=row["run_id"],
            source=row["source"],
            venue_id=row["venue_id"],
            mode=row["mode"],
            status=row["status"],
            config_hash=row["config_hash"],
            invitation=row["invitation"],
            cursor_after=row["cursor_after"],
            watermark_candidate_ms=row["watermark_candidate_ms"],
            total_expected=row["total_expected"],
            new_replies=row["new_replies"],
            started_ms=row["started_ms"],
            heartbeat_ms=row["heartbeat_ms"],
        )

    def commit_page(
        self,
        *,
        run_id: str,
        papers: Iterable[dict],
        cursor_after: str | None,
        candidate_watermark_ms: int | None,
        total_expected: int | None,
        raw_count: int,
        replies: Iterable[dict] = (),
        summaries: Iterable[dict] = (),
        events: Iterable[dict] = (),
        now_ms: int | None = None,
    ) -> PageCommit:
        """Upsert one page, its replies, events and cursor atomically."""

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        papers = list(papers)
        replies = list(replies)
        summaries = list(summaries)
        events = list(events)
        new_count = 0
        new_replies = 0
        with self._tx():
            run = self.conn.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ConferenceStoreError(f"unknown sync run: {run_id}")
            source = run["source"]
            baseline_cutoff = run["started_ms"] if run["mode"] == "bootstrap" else None

            for paper in papers:
                forum_id = str(paper.get("forum_id") or paper.get("note_id") or "")
                if not forum_id:
                    continue
                raw_json = _json(paper)
                content_hash = _content_hash(paper)
                cdate = int(paper.get("cdate") or 0)
                briefed_date = (
                    "baseline"
                    if baseline_cutoff is not None and cdate < baseline_cutoff
                    else None
                )
                cursor = self.conn.execute(
                    "INSERT INTO papers(source, forum_id, note_id, title, abstract, "
                    "authors_json, keywords_json, venue, venueid, status, pdf, "
                    "landing_url, cdate, mdate, raw_json, content_hash, "
                    "first_seen_ms, first_seen_run, last_seen_ms, briefed_date) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source, forum_id) DO NOTHING",
                    (
                        source,
                        forum_id,
                        str(paper.get("note_id") or ""),
                        str(paper.get("title") or ""),
                        str(paper.get("abstract") or ""),
                        _json(paper.get("authors") or []),
                        _json(paper.get("keywords") or []),
                        str(paper.get("venue") or ""),
                        str(paper.get("venue_id") or ""),
                        str(paper.get("status") or "unknown"),
                        str(paper.get("pdf") or ""),
                        str(paper.get("landing_url") or ""),
                        cdate,
                        int(paper.get("mdate") or 0),
                        raw_json,
                        content_hash,
                        now_ms,
                        run_id,
                        now_ms,
                        briefed_date,
                    ),
                )
                if cursor.rowcount:
                    new_count += 1
                    continue
                # Existing paper: refresh metadata and presence, but never
                # overwrite first_seen_* or the briefing marker.
                self.conn.execute(
                    "UPDATE papers SET note_id = ?, title = ?, abstract = ?, "
                    "authors_json = ?, keywords_json = ?, venue = ?, venueid = ?, "
                    "status = ?, pdf = ?, landing_url = ?, cdate = ?, mdate = ?, "
                    "raw_json = ?, content_hash = ?, last_seen_ms = ? "
                    "WHERE source = ? AND forum_id = ?",
                    (
                        str(paper.get("note_id") or ""),
                        str(paper.get("title") or ""),
                        str(paper.get("abstract") or ""),
                        _json(paper.get("authors") or []),
                        _json(paper.get("keywords") or []),
                        str(paper.get("venue") or ""),
                        str(paper.get("venue_id") or ""),
                        str(paper.get("status") or "unknown"),
                        str(paper.get("pdf") or ""),
                        str(paper.get("landing_url") or ""),
                        cdate,
                        int(paper.get("mdate") or 0),
                        raw_json,
                        content_hash,
                        now_ms,
                        source,
                        forum_id,
                    ),
                )

            for reply in replies:
                reply_id = str(reply.get("reply_id") or reply.get("id") or "")
                forum_id = str(reply.get("forum_id") or "")
                if not reply_id or not forum_id:
                    continue
                raw_json = str(reply.get("raw_json") or _json(reply))
                content_hash = str(reply.get("content_hash") or _content_hash(reply))
                cursor = self.conn.execute(
                    "INSERT INTO reviews(source, reply_id, forum_id, invitation, "
                    "reply_type, rating, confidence, soundness, presentation, "
                    "informativeness, summary, strengths, weaknesses, questions, "
                    "decision, raw_json, content_hash, reply_cdate, reply_mdate, "
                    "first_seen_ms, first_seen_run, last_seen_ms) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?) ON CONFLICT(source, reply_id) DO NOTHING",
                    (
                        source,
                        reply_id,
                        forum_id,
                        str(reply.get("invitation") or ""),
                        str(reply.get("reply_type") or "other"),
                        str(reply.get("rating") or ""),
                        str(reply.get("confidence") or ""),
                        str(reply.get("soundness") or ""),
                        str(reply.get("presentation") or ""),
                        str(reply.get("informativeness") or ""),
                        str(reply.get("summary") or ""),
                        str(reply.get("strengths") or ""),
                        str(reply.get("weaknesses") or ""),
                        str(reply.get("questions") or ""),
                        str(reply.get("decision") or ""),
                        raw_json,
                        content_hash,
                        int(reply.get("reply_cdate") or 0),
                        int(reply.get("reply_mdate") or 0),
                        now_ms,
                        run_id,
                        now_ms,
                    ),
                )
                if cursor.rowcount:
                    new_replies += 1
                    continue
                self.conn.execute(
                    "UPDATE reviews SET forum_id = ?, invitation = ?, "
                    "reply_type = ?, rating = ?, confidence = ?, soundness = ?, "
                    "presentation = ?, informativeness = ?, summary = ?, "
                    "strengths = ?, weaknesses = ?, questions = ?, decision = ?, "
                    "raw_json = ?, content_hash = ?, reply_cdate = ?, "
                    "reply_mdate = ?, last_seen_ms = ? "
                    "WHERE source = ? AND reply_id = ?",
                    (
                        forum_id,
                        str(reply.get("invitation") or ""),
                        str(reply.get("reply_type") or "other"),
                        str(reply.get("rating") or ""),
                        str(reply.get("confidence") or ""),
                        str(reply.get("soundness") or ""),
                        str(reply.get("presentation") or ""),
                        str(reply.get("informativeness") or ""),
                        str(reply.get("summary") or ""),
                        str(reply.get("strengths") or ""),
                        str(reply.get("weaknesses") or ""),
                        str(reply.get("questions") or ""),
                        str(reply.get("decision") or ""),
                        raw_json,
                        content_hash,
                        int(reply.get("reply_cdate") or 0),
                        int(reply.get("reply_mdate") or 0),
                        now_ms,
                        source,
                        reply_id,
                    ),
                )

            for summary in summaries:
                forum_id = str(summary.get("forum_id") or "")
                if not forum_id:
                    continue
                self.conn.execute(
                    "INSERT INTO paper_review_summary(source, forum_id, "
                    "review_count, rating_count, rating_mean, rating_median, "
                    "rating_std, rating_min, rating_max, rating_range, "
                    "confidence_mean, rating_raw_values_json, "
                    "confidence_raw_values_json, decision, presentation, "
                    "strong_signal, controversial, updated_ms) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source, forum_id) DO UPDATE SET "
                    "review_count = excluded.review_count, "
                    "rating_count = excluded.rating_count, "
                    "rating_mean = excluded.rating_mean, "
                    "rating_median = excluded.rating_median, "
                    "rating_std = excluded.rating_std, "
                    "rating_min = excluded.rating_min, "
                    "rating_max = excluded.rating_max, "
                    "rating_range = excluded.rating_range, "
                    "confidence_mean = excluded.confidence_mean, "
                    "rating_raw_values_json = excluded.rating_raw_values_json, "
                    "confidence_raw_values_json = excluded.confidence_raw_values_json, "
                    "decision = excluded.decision, "
                    "presentation = excluded.presentation, "
                    "strong_signal = excluded.strong_signal, "
                    "controversial = excluded.controversial, "
                    "updated_ms = excluded.updated_ms",
                    (
                        source,
                        forum_id,
                        int(summary.get("review_count") or 0),
                        int(summary.get("rating_count") or 0),
                        summary.get("rating_mean"),
                        summary.get("rating_median"),
                        summary.get("rating_std"),
                        summary.get("rating_min"),
                        summary.get("rating_max"),
                        summary.get("rating_range"),
                        summary.get("confidence_mean"),
                        _json(summary.get("rating_raw_values") or []),
                        _json(summary.get("confidence_raw_values") or []),
                        str(summary.get("decision") or ""),
                        str(summary.get("presentation") or ""),
                        1 if summary.get("strong_signal") else 0,
                        1 if summary.get("controversial") else 0,
                        now_ms,
                    ),
                )

            for event in events:
                event_id = str(event.get("event_id") or "")
                forum_id = str(event.get("forum_id") or "")
                if not event_id or not forum_id:
                    continue
                event_types = event.get("event_types") or []
                # A newer change replaces the old pending item for the same
                # paper, so one render batch never contains the same paper
                # twice with contradictory snapshots.
                self.conn.execute(
                    "UPDATE events SET status = 'superseded', updated_ms = ? "
                    "WHERE source = ? AND forum_id = ? AND status = 'pending' "
                    "AND event_id <> ?",
                    (now_ms, source, forum_id, event_id),
                )
                self.conn.execute(
                    "INSERT INTO events(event_id, source, forum_id, "
                    "event_types_json, status, created_ms, updated_ms) "
                    "VALUES(?, ?, ?, ?, 'pending', ?, ?) "
                    "ON CONFLICT(event_id) DO UPDATE SET "
                    "status = 'pending', briefing_filename = NULL, "
                    "attempts = 0, last_error = '', "
                    "created_ms = excluded.created_ms, "
                    "updated_ms = excluded.updated_ms",
                    (
                        event_id,
                        source,
                        forum_id,
                        _json(event_types),
                        int(event.get("created_ms") or now_ms),
                        now_ms,
                    ),
                )

            scanned = sum(
                1
                for paper in papers
                if str(paper.get("forum_id") or paper.get("note_id") or "")
            )
            self.conn.execute(
                "UPDATE sync_runs SET cursor_after = ?, "
                "watermark_candidate_ms = CASE WHEN ? IS NULL THEN "
                "watermark_candidate_ms ELSE MAX(COALESCE(watermark_candidate_ms, 0), ?) "
                "END, total_expected = COALESCE(?, total_expected), "
                "fetched_count = fetched_count + ?, scanned_count = scanned_count + ?, "
                "new_count = new_count + ?, new_replies = new_replies + ?, "
                "heartbeat_ms = ?, updated_ms = ? "
                "WHERE run_id = ?",
                (
                    cursor_after,
                    candidate_watermark_ms,
                    candidate_watermark_ms,
                    total_expected,
                    int(raw_count),
                    scanned,
                    new_count,
                    new_replies,
                    now_ms,
                    now_ms,
                    run_id,
                ),
            )
        return PageCommit(new_count, new_replies)

    def finish_run(self, run_id: str, now_ms: int | None = None) -> None:
        """Mark a run complete and only now advance the source watermark."""

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._tx():
            run = self.conn.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise ConferenceStoreError(f"unknown sync run: {run_id}")
            source = run["source"]
            venue = self.conn.execute(
                "SELECT * FROM venues WHERE source = ?", (source,)
            ).fetchone()
            if venue is None:
                raise ConferenceStoreError(f"unknown venue for source: {source}")
            previous = venue["watermark_ms"]
            candidate = run["watermark_candidate_ms"]
            watermark = previous
            # A review sample reads the latest N submissions, not the oldest
            # unread ones; advancing the streaming watermark from it would
            # skip whatever lies between the sample and the real cursor.
            if run["mode"] != "review_sample" and candidate is not None:
                watermark = max(int(previous or 0), int(candidate))
            last_full = venue["last_full_sync_ms"]
            if run["mode"] in {"bootstrap", "full"}:
                last_full = now_ms
            bootstrap = venue["bootstrap_completed_ms"]
            if run["mode"] == "bootstrap":
                bootstrap = now_ms
            sample = venue["reviews_sample_completed_ms"]
            if run["mode"] == "review_sample":
                sample = now_ms
            rescan = venue["last_review_rescan_ms"]
            # A sample is also a review pass over the current venue; marking
            # it here prevents an immediate empty review_rescan on the next
            # invocation.
            if run["mode"] in {"review_rescan", "review_sample"}:
                rescan = now_ms
            self.conn.execute(
                "UPDATE sync_runs SET status = 'COMPLETE', phase = 'done', "
                "heartbeat_ms = ?, updated_ms = ? WHERE run_id = ?",
                (now_ms, now_ms, run_id),
            )
            self.conn.execute(
                "UPDATE venues SET watermark_ms = ?, last_poll_ms = ?, "
                "last_full_sync_ms = ?, bootstrap_completed_ms = ?, "
                "reviews_watch_from_ms = ?, last_review_rescan_ms = ?, "
                "reviews_sample_completed_ms = ?, "
                "last_outcome = 'SUCCESS', last_message = '' WHERE source = ?",
                (
                    watermark,
                    now_ms,
                    last_full,
                    bootstrap,
                    venue["reviews_watch_from_ms"],
                    rescan,
                    sample,
                    source,
                ),
            )

    def pause_run(
        self,
        run_id: str,
        message: str = "",
        now_ms: int | None = None,
    ) -> None:
        """Leave a run resumable after hitting the per-run page cap."""

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._tx():
            self.conn.execute(
                "UPDATE sync_runs SET phase = 'paused', message = ?, "
                "heartbeat_ms = ?, updated_ms = ? WHERE run_id = ?",
                (message, now_ms, now_ms, run_id),
            )

    def fail_run(
        self,
        run_id: str,
        outcome: str,
        message: str = "",
        now_ms: int | None = None,
    ) -> None:
        """Record a failure without advancing any watermark."""

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._tx():
            run = self.conn.execute(
                "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return
            self.conn.execute(
                "UPDATE sync_runs SET status = 'FAILED', phase = 'error', "
                "message = ?, heartbeat_ms = ?, updated_ms = ? WHERE run_id = ?",
                (message, now_ms, now_ms, run_id),
            )
            self.conn.execute(
                "UPDATE venues SET last_outcome = ?, last_message = ? WHERE source = ?",
                (outcome, message, run["source"]),
            )

    # ------------------------------------------------------------------
    # AI briefing event queue
    # ------------------------------------------------------------------
    def forum_states(self, source: str, forum_ids: Iterable[str]) -> dict[str, dict]:
        """Return paper/reviews/summary for the requested forums.

        The renderer and the event detector both need the *current* state,
        not a second copy of every abstract stored in the event row.  Missing
        forums are omitted so a stale event cannot resurrect a deleted row.
        """

        ids = list(dict.fromkeys(str(value) for value in forum_ids if str(value)))
        states: dict[str, dict] = {
            forum_id: {"paper": None, "reviews": [], "summary": None}
            for forum_id in ids
        }
        for start in range(0, len(ids), _BRIEF_CHUNK):
            chunk = ids[start : start + _BRIEF_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            paper_rows = self.conn.execute(
                f"SELECT * FROM papers WHERE source = ? "
                f"AND forum_id IN ({placeholders})",
                (source, *chunk),
            ).fetchall()
            for row in paper_rows:
                paper = self._paper_dict(row)
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError):
                    raw = {}
                paper["affiliations"] = raw.get("affiliations") or []
                paper["code_url"] = raw.get("code_url") or ""
                paper["camera_ready"] = bool(raw.get("camera_ready", False))
                states[row["forum_id"]]["paper"] = paper

            review_rows = self.conn.execute(
                f"SELECT * FROM reviews WHERE source = ? "
                f"AND forum_id IN ({placeholders}) ORDER BY reply_id",
                (source, *chunk),
            ).fetchall()
            for row in review_rows:
                states[row["forum_id"]]["reviews"].append(self._review_dict(row))

            summary_rows = self.conn.execute(
                f"SELECT * FROM paper_review_summary WHERE source = ? "
                f"AND forum_id IN ({placeholders})",
                (source, *chunk),
            ).fetchall()
            for row in summary_rows:
                states[row["forum_id"]]["summary"] = self._summary_dict(row)

        return {
            forum_id: state
            for forum_id, state in states.items()
            if state["paper"] is not None
        }

    def pending_events(self, source: str, limit: int = 10) -> list[dict]:
        """Return the oldest pending briefing events for one source."""

        rows = self.conn.execute(
            "SELECT * FROM events WHERE source = ? AND status = 'pending' "
            "ORDER BY created_ms, event_id LIMIT ?",
            (source, max(0, int(limit))),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["event_types"] = json.loads(item["event_types_json"] or "[]")
            result.append(item)
        return result

    def mark_rendered(
        self,
        event_ids: Iterable[str],
        filename: str,
        now_ms: int | None = None,
    ) -> int:
        """Mark events rendered after their briefing file is durable."""

        ids = [str(value) for value in event_ids if str(value)]
        if not ids:
            return 0
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        changed = 0
        for start in range(0, len(ids), _BRIEF_CHUNK):
            chunk = ids[start : start + _BRIEF_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            with self._tx():
                cursor = self.conn.execute(
                    f"UPDATE events SET status = 'rendered', "
                    f"briefing_filename = ?, updated_ms = ? "
                    f"WHERE event_id IN ({placeholders}) AND status = 'pending'",
                    (filename, now_ms, *chunk),
                )
                changed += cursor.rowcount
        return changed

    def record_event_failure(
        self,
        event_ids: Iterable[str],
        error: str,
        *,
        max_attempts: int,
        now_ms: int | None = None,
    ) -> int:
        """Count a model failure; quarantine the event after N attempts."""

        ids = [str(value) for value in event_ids if str(value)]
        if not ids:
            return 0
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        message = " ".join(str(error or "").split())[:500]
        changed = 0
        for start in range(0, len(ids), _BRIEF_CHUNK):
            chunk = ids[start : start + _BRIEF_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            with self._tx():
                cursor = self.conn.execute(
                    f"UPDATE events SET attempts = attempts + 1, "
                    f"last_error = ?, updated_ms = ?, "
                    f"status = CASE WHEN attempts + 1 >= ? THEN 'failed' "
                    f"ELSE 'pending' END "
                    f"WHERE event_id IN ({placeholders}) AND status = 'pending'",
                    (message, now_ms, max(1, int(max_attempts)), *chunk),
                )
                changed += cursor.rowcount
        return changed

    def failed_event_count(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE source = ? AND status = 'failed'",
            (source,),
        ).fetchone()
        return int(row["n"])

    def retrying_event_count(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE source = ? AND status = 'pending' AND attempts > 0",
            (source,),
        ).fetchone()
        return int(row["n"])

    def seed_review_events(
        self,
        source: str,
        now_ms: int,
        *,
        max_review_chars: int = 6_000,
        max_abstract_chars: int = 4_000,
        max_reviews_per_paper: int = 0,
        limit: int = 200,
    ) -> int:
        """Queue one event per paper that already has reviews/decision.

        This is the one-time backfill for a database ingested before the AI
        briefing layer existed.  ``INSERT OR IGNORE`` makes a retry harmless.
        """

        from conference_events import build_event, build_snapshot, snapshot_signature

        rows = self.conn.execute(
            "SELECT * FROM paper_review_summary WHERE source = ? "
            "AND (review_count > 0 OR decision <> '') "
            "ORDER BY updated_ms DESC, forum_id LIMIT ?",
            (source, max(1, int(limit))),
        ).fetchall()
        if not rows:
            return 0
        event_types_by_forum = {
            row["forum_id"]: (
                ["REVIEWS_CHANGED"]
                if int(row["review_count"] or 0) > 0
                else ["DECISION_CHANGED"]
            )
            for row in rows
        }
        states = self.forum_states(source, event_types_by_forum)
        inserted = 0
        with self._tx():
            for forum_id, event_types in event_types_by_forum.items():
                state = states.get(forum_id)
                if state is None:
                    continue
                snapshot = build_snapshot(
                    state["paper"],
                    state["reviews"],
                    state["summary"],
                    max_review_chars=max_review_chars,
                    max_abstract_chars=max_abstract_chars,
                    max_reviews_per_paper=max_reviews_per_paper,
                )
                event = build_event(
                    source=source,
                    forum_id=forum_id,
                    event_types=event_types,
                    signature=snapshot_signature(snapshot),
                    now_ms=now_ms,
                )
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO events(event_id, source, forum_id, "
                    "event_types_json, status, created_ms, updated_ms) "
                    "VALUES(?, ?, ?, ?, 'pending', ?, ?)",
                    (
                        event["event_id"],
                        source,
                        forum_id,
                        _json(event_types),
                        now_ms,
                        now_ms,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def mark_ai_initialized(self, source: str, now_ms: int) -> None:
        with self._tx():
            self.conn.execute(
                "UPDATE venues SET ai_initialized_ms = ? WHERE source = ?",
                (now_ms, source),
            )

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict:
        return {
            "reply_id": row["reply_id"],
            "forum_id": row["forum_id"],
            "invitation": row["invitation"],
            "reply_type": row["reply_type"],
            "rating": row["rating"],
            "confidence": row["confidence"],
            "soundness": row["soundness"],
            "presentation": row["presentation"],
            "informativeness": row["informativeness"],
            "summary": row["summary"],
            "strengths": row["strengths"],
            "weaknesses": row["weaknesses"],
            "questions": row["questions"],
            "decision": row["decision"],
            "reply_mdate": row["reply_mdate"],
        }

    @staticmethod
    def _summary_dict(row: sqlite3.Row) -> dict:
        def load(value: str) -> list:
            try:
                parsed = json.loads(value or "[]")
            except (TypeError, ValueError):
                return []
            return parsed if isinstance(parsed, list) else []

        return {
            "review_count": row["review_count"],
            "rating_count": row["rating_count"],
            "rating_mean": row["rating_mean"],
            "rating_median": row["rating_median"],
            "rating_std": row["rating_std"],
            "rating_min": row["rating_min"],
            "rating_max": row["rating_max"],
            "rating_range": row["rating_range"],
            "confidence_mean": row["confidence_mean"],
            "rating_raw_values": load(row["rating_raw_values_json"]),
            "confidence_raw_values": load(row["confidence_raw_values_json"]),
            "decision": row["decision"],
            "presentation": row["presentation"],
            "strong_signal": bool(row["strong_signal"]),
            "controversial": bool(row["controversial"]),
        }

    # ------------------------------------------------------------------
    # Briefing queries
    # ------------------------------------------------------------------
    def pending_papers(self, source: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE source = ? AND briefed_date IS NULL "
            "ORDER BY cdate, forum_id",
            (source,),
        ).fetchall()
        return [self._paper_dict(row) for row in rows]

    def mark_briefed(self, source: str, date: str, forum_ids: Iterable[str]) -> int:
        ids = [str(value) for value in forum_ids if str(value)]
        changed = 0
        for start in range(0, len(ids), _BRIEF_CHUNK):
            chunk = ids[start : start + _BRIEF_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            with self._tx():
                cursor = self.conn.execute(
                    f"UPDATE papers SET briefed_date = ? WHERE source = ? "
                    f"AND briefed_date IS NULL AND forum_id IN ({placeholders})",
                    (date, source, *chunk),
                )
                changed += cursor.rowcount
        return changed

    def status_counts(self, source: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM papers WHERE source = ? "
            "GROUP BY status ORDER BY status",
            (source,),
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def papers_count(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM papers WHERE source = ?", (source,)
        ).fetchone()
        return int(row["n"])

    def last_run(self, source: str) -> SyncRun | None:
        row = self.conn.execute(
            "SELECT * FROM sync_runs WHERE source = ? ORDER BY updated_ms DESC LIMIT 1",
            (source,),
        ).fetchone()
        return self._run_from_row(row) if row is not None else None

    @staticmethod
    def _paper_dict(row: sqlite3.Row) -> dict:
        return {
            "source": row["source"],
            "forum_id": row["forum_id"],
            "note_id": row["note_id"],
            "title": row["title"],
            "abstract": row["abstract"],
            "authors": json.loads(row["authors_json"] or "[]"),
            "keywords": json.loads(row["keywords_json"] or "[]"),
            "venue": row["venue"],
            "venue_id": row["venueid"],
            "status": row["status"],
            "pdf": row["pdf"],
            "landing_url": row["landing_url"],
            "cdate": row["cdate"],
            "mdate": row["mdate"],
            "first_seen_ms": row["first_seen_ms"],
            "briefed_date": row["briefed_date"],
        }
