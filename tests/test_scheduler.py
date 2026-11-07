"""Tests for the container-native scheduler wrapper.

The schedule itself lives in ``deploy/crontab.*`` and is executed by
supercronic; ``scheduler.py`` only records outcomes, recovers missed runs and
reports health.  Clocks and job runners are injected so every decision is
testable without waiting for wall time.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scheduler

TZ = ZoneInfo("Asia/Shanghai")
REPO_ROOT = Path(__file__).resolve().parent.parent


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class FakeRunner:
    """Records job names and returns scripted exit codes."""

    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[str] = []
        self.codes = dict(codes or {})

    def __call__(self, spec: scheduler.JobSpec, timeout: int) -> int:
        self.calls.append(spec.name)
        return self.codes.get(spec.name, 0)


@pytest.fixture
def crontab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "crontab.full"
    path.write_text(
        "\n".join(
            [
                "*/15 * * * * python -m scripts.scheduler catch-up",
                "0 3 * * * python -m scripts.scheduler run-job arxiv",
                "30 3 * * * python -m scripts.scheduler run-job resource",
                "45 3 * * * python -m scripts.scheduler run-job code",
                "0 4 * * * python -m scripts.scheduler run-job papers",
                "30 4 * * * python -m scripts.scheduler run-job ai_news",
                "30 5 * * * python -m scripts.scheduler run-job push_early",
                "0 6 * * * python -m scripts.scheduler run-job push_papers",
                "0 7 * * * python -m scripts.scheduler run-job push_arxiv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILYINFO_SCHEDULER_CRONTAB", str(path))
    monkeypatch.setenv("DAILYINFO_SCHEDULER_JOBS", "full")
    return path


def read_state() -> dict:
    path = scheduler.state_path()
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schedule source
# ---------------------------------------------------------------------------


def test_checked_in_full_crontab_matches_launchd_schedule() -> None:
    parsed = scheduler.parse_crontab(REPO_ROOT / "deploy" / "crontab.full")

    assert {job.name: (job.hour, job.minute) for job in parsed.jobs} == {
        "arxiv": (3, 0),
        "resource": (3, 30),
        "code": (3, 45),
        "papers": (4, 0),
        "ai_news": (4, 30),
        "push_early": (5, 30),
        "push_papers": (6, 0),
        "push_arxiv": (7, 0),
    }


def test_collect_crontab_is_full_minus_push_jobs() -> None:
    full = scheduler.parse_crontab(REPO_ROOT / "deploy" / "crontab.full")
    collect = scheduler.parse_crontab(REPO_ROOT / "deploy" / "crontab.collect")

    full_non_push = [
        job for job in full.jobs if not scheduler.JOB_SPECS[job.name].push_only
    ]
    assert [job.name for job in collect.jobs] == [job.name for job in full_non_push]


def test_unknown_job_name_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "crontab"
    path.write_text(
        "0 3 * * * python -m scripts.scheduler run-job not_a_job\n",
        encoding="utf-8",
    )

    with pytest.raises(scheduler.CrontabError):
        scheduler.parse_crontab(path)


def test_crontab_path_prefers_the_installed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Docker healthchecks and `compose exec` do not inherit the entrypoint's
    # exports, so the image path must resolve without the env var.
    installed = tmp_path / "etc"
    installed.mkdir()
    (installed / "crontab.collect").write_text("", encoding="utf-8")
    monkeypatch.setattr(scheduler, "INSTALLED_CRONTAB_DIR", installed)
    monkeypatch.delenv("DAILYINFO_SCHEDULER_CRONTAB", raising=False)
    monkeypatch.setenv("DAILYINFO_SCHEDULER_JOBS", "collect")

    assert scheduler.crontab_path() == installed / "crontab.collect"


def test_crontab_path_falls_back_to_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler, "INSTALLED_CRONTAB_DIR", tmp_path / "missing")
    monkeypatch.delenv("DAILYINFO_SCHEDULER_CRONTAB", raising=False)
    monkeypatch.setenv("DAILYINFO_SCHEDULER_JOBS", "collect")

    assert scheduler.crontab_path() == REPO_ROOT / "deploy" / "crontab.collect"


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------


def test_catch_up_runs_only_jobs_due_today_in_order(crontab: Path) -> None:
    runner = FakeRunner()

    scheduler.catch_up(now=at(4, 31), runner=runner)

    assert runner.calls == ["arxiv", "resource", "code", "papers", "ai_news"]
    state = read_state()
    assert state["jobs"]["ai_news"]["success_date"] == "2026-11-07"


def test_catch_up_runs_nothing_before_first_job(crontab: Path) -> None:
    runner = FakeRunner()

    scheduler.catch_up(now=at(2, 59), runner=runner)

    assert runner.calls == []


def test_catch_up_skips_jobs_that_already_succeeded(crontab: Path) -> None:
    runner = FakeRunner()
    scheduler.catch_up(now=at(3, 5), runner=runner)
    assert runner.calls == ["arxiv"]

    second = FakeRunner()
    scheduler.catch_up(now=at(4, 31), runner=second)

    assert second.calls == ["resource", "code", "papers", "ai_news"]


def test_catch_up_skips_jobs_beyond_retry_limit(crontab: Path) -> None:
    runner = FakeRunner({"arxiv": 1})

    scheduler.catch_up(now=at(3, 5), runner=runner)
    scheduler.catch_up(now=at(3, 10), runner=runner)
    scheduler.catch_up(now=at(3, 30), runner=runner)
    scheduler.catch_up(now=at(4, 0), runner=runner)

    assert runner.calls.count("arxiv") == scheduler.MAX_ATTEMPTS_PER_DAY


def test_failed_job_backs_off_before_retry(crontab: Path) -> None:
    runner = FakeRunner({"arxiv": 1})

    scheduler.catch_up(now=at(3, 1), runner=runner)
    state = read_state()["jobs"]["arxiv"]
    assert state["attempts_today"] == 1
    assert state["next_attempt_at"] == at(3, 6).isoformat()

    scheduler.catch_up(now=at(3, 5), runner=runner)
    assert runner.calls.count("arxiv") == 1

    scheduler.catch_up(now=at(3, 6), runner=runner)
    assert runner.calls.count("arxiv") == 2


def test_success_clears_retry_state(crontab: Path) -> None:
    runner = FakeRunner({"arxiv": 1})
    scheduler.catch_up(now=at(3, 1), runner=runner)

    runner.codes["arxiv"] = 0
    scheduler.catch_up(now=at(3, 6), runner=runner)

    state = read_state()["jobs"]["arxiv"]
    assert state["success_date"] == "2026-11-07"
    assert state["attempts_today"] == 0
    assert state["next_attempt_at"] == ""


def test_run_job_records_the_scheduled_date_and_exit_code(crontab: Path) -> None:
    runner = FakeRunner({"arxiv": 2})

    exit_code = scheduler.run_job("arxiv", now=at(3, 0), runner=runner)

    assert exit_code == 2
    state = read_state()["jobs"]["arxiv"]
    assert state["success_date"] == ""
    assert state["last_attempt_date"] == "2026-11-07"
    assert state["last_exit_code"] == 2


def test_run_job_lock_busy_skips_without_running(crontab: Path) -> None:
    holder = scheduler.try_acquire_lock()
    assert holder is not None
    try:
        runner = FakeRunner()
        exit_code = scheduler.run_job("arxiv", now=at(3, 0), runner=runner)
    finally:
        scheduler.release_lock(holder)

    assert exit_code == 0
    assert runner.calls == []


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_healthcheck_is_green_before_the_deadline(crontab: Path) -> None:
    scheduler.mark_started(now=at(2, 0))

    assert scheduler.healthcheck(now=at(4, 59)) == 0


def test_healthcheck_turns_red_for_an_overdue_job(crontab: Path) -> None:
    scheduler.mark_started(now=at(2, 0))

    assert scheduler.healthcheck(now=at(5, 1)) == 1


def test_healthcheck_stays_red_until_yesterdays_job_succeeds(crontab: Path) -> None:
    # 00:30 the next day: yesterday's 03:00 arxiv job is still the most recent
    # occurrence, and it never succeeded.
    scheduler.mark_started(now=at(2, 0))

    assert scheduler.healthcheck(now=at(0, 30, day=8)) == 1


def test_healthcheck_ignores_runs_before_the_scheduler_started(crontab: Path) -> None:
    # A container started at 05:01 has no obligation to deliver yesterday's
    # 05:30 push; only occurrences at or after started_at count.
    scheduler.mark_started(now=at(5, 1))

    assert scheduler.healthcheck(now=at(5, 2)) == 0


def test_healthcheck_accepts_a_successful_run(crontab: Path) -> None:
    runner = FakeRunner()
    scheduler.catch_up(now=at(3, 1), runner=runner)
    scheduler.catch_up(now=at(4, 31), runner=runner)

    assert scheduler.healthcheck(now=at(5, 1)) == 0


def test_corrupt_state_is_quarantined_and_health_is_red(crontab: Path) -> None:
    scheduler.state_path().parent.mkdir(parents=True, exist_ok=True)
    scheduler.state_path().write_text("{not json", encoding="utf-8")

    assert scheduler.healthcheck(now=at(9, 0)) == 1
    assert scheduler.state_path().exists() is False
    assert list(scheduler.state_path().parent.glob("scheduler_state.json.corrupt-*"))


# ---------------------------------------------------------------------------
# Environment and manual execution
# ---------------------------------------------------------------------------


def test_verify_env_rejects_missing_discord_token_in_full_mode(
    crontab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    with pytest.raises(scheduler.SchedulerConfigError):
        scheduler.verify_env(now=at(1, 0), local_offset=timedelta(hours=8))


def test_verify_env_accepts_collect_mode_without_discord_token(
    crontab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILYINFO_SCHEDULER_JOBS", "collect")
    monkeypatch.setenv(
        "DAILYINFO_SCHEDULER_CRONTAB", str(REPO_ROOT / "deploy" / "crontab.collect")
    )
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    scheduler.verify_env(now=at(1, 0), local_offset=timedelta(hours=8))


def test_verify_env_rejects_a_timezone_mismatch(
    crontab: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DAILYINFO_SCHEDULER_TZ", "UTC")

    with pytest.raises(scheduler.SchedulerConfigError):
        scheduler.verify_env(now=at(1, 0), local_offset=timedelta(hours=8))


def test_exec_propagates_the_child_exit_code(crontab: Path) -> None:
    exit_code = scheduler.exec_command(
        ["--", sys.executable, "-c", "raise SystemExit(3)"]
    )

    assert exit_code == 3


def test_exec_refuses_to_run_while_a_job_holds_the_lock(crontab: Path) -> None:
    holder = scheduler.try_acquire_lock()
    assert holder is not None
    try:
        exit_code = scheduler.exec_command(
            ["--", sys.executable, "-c", "raise SystemExit(0)"]
        )
    finally:
        scheduler.release_lock(holder)

    assert exit_code == scheduler.EXIT_LOCKED
