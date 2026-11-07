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

SCHEMA_VERSION = 1
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
    last_outcome: str
    last_message: str


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
            self.conn.executescript("""
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
                CREATE INDEX IF NOT EXISTS sync_runs_source_status
                    ON sync_runs(source, status, updated_ms);
                """)
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
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
            if found < SCHEMA_VERSION:
                raise ConferenceSchemaError(
                    f"conference database schema v{found} needs a migration "
                    f"to v{SCHEMA_VERSION}; none is implemented"
                )

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
                    "last_full_sync_ms = NULL, last_poll_ms = NULL WHERE source = ?",
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
            last_outcome=row["last_outcome"],
            last_message=row["last_message"],
        )

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
        now_ms: int | None = None,
    ) -> int:
        """Upsert one page and its cursor atomically; return newly inserted count."""

        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        papers = list(papers)
        new_count = 0
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
                "new_count = new_count + ?, heartbeat_ms = ?, updated_ms = ? "
                "WHERE run_id = ?",
                (
                    cursor_after,
                    candidate_watermark_ms,
                    candidate_watermark_ms,
                    total_expected,
                    int(raw_count),
                    scanned,
                    new_count,
                    now_ms,
                    now_ms,
                    run_id,
                ),
            )
        return new_count

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
            if candidate is not None:
                watermark = max(int(previous or 0), int(candidate))
            last_full = venue["last_full_sync_ms"]
            if run["mode"] in {"bootstrap", "full"}:
                last_full = now_ms
            bootstrap = venue["bootstrap_completed_ms"]
            if run["mode"] == "bootstrap":
                bootstrap = now_ms
            self.conn.execute(
                "UPDATE sync_runs SET status = 'COMPLETE', phase = 'done', "
                "heartbeat_ms = ?, updated_ms = ? WHERE run_id = ?",
                (now_ms, now_ms, run_id),
            )
            self.conn.execute(
                "UPDATE venues SET watermark_ms = ?, last_poll_ms = ?, "
                "last_full_sync_ms = ?, bootstrap_completed_ms = ?, "
                "last_outcome = 'SUCCESS', last_message = '' WHERE source = ?",
                (watermark, now_ms, last_full, bootstrap, source),
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
