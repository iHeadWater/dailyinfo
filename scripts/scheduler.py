#!/usr/bin/env python3
"""Container-native scheduler wrapper for DailyInfo.

supercronic owns the clock: it reads ``deploy/crontab.full`` (or
``crontab.collect``) and starts ``run-job`` at the configured times.  This
module only answers the questions around that clock:

* what does a job run, and what happened when it ran (state)
* which job scheduled for today never succeeded (catch-up)
* is any job overdue (healthcheck)

Keeping time decisions out of Python is deliberate.  A container that is
paused, a host that suspends, or a clock jump all become "cron missed a
minute", which the periodic ``catch-up`` line repairs.  There is no
in-process timer to drift.

State lives in ``$DAILYINFO_DATA_ROOT/state/scheduler_state.json`` and is
written atomically.  A single ``flock`` file serialises every job, so a long
run cannot overlap the next one or a manual ``collector`` invocation.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import IO, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Flat imports (``import paths``) resolve when the module is loaded through
# ``python -m scripts.scheduler`` only if ``scripts/`` is on sys.path.  This
# mirrors scripts/cli.py.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import paths  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_CRONTAB_DIR = Path("/etc/dailyinfo")

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_JOBS_MODE = "full"
JOB_MODES = ("full", "collect")

STATE_VERSION = 1
MAX_ATTEMPTS_PER_DAY = 3
# Three attempts per day means two delays: 5 and 15 minutes.  A third value
# here would be unreachable and would make the docs promise a 30-minute wait
# that never happens.
RETRY_BACKOFF_SECONDS = (5 * 60, 15 * 60)
JOB_TIMEOUT_SECONDS = 2 * 60 * 60
EXIT_LOCKED = 75
EXIT_STATE_UNWRITABLE = 74

_RUN_JOB_PREFIX = ["-m", "scripts.scheduler", "run-job"]
_CATCH_UP_PREFIX = ["-m", "scripts.scheduler", "catch-up"]
_EVERY_MINUTES = re.compile(r"\*/\d+")


class SchedulerConfigError(RuntimeError):
    """Environment or schedule configuration cannot be served safely."""


class CrontabError(ValueError):
    """The checked-in crontab cannot be parsed or is internally inconsistent."""


class StateCorruptError(RuntimeError):
    """The state file could not be read and has been quarantined."""


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    """One named job.  The schedule lives in crontab, not here."""

    name: str
    argv: tuple[str, ...]
    push_only: bool = False


JOB_SPECS: dict[str, JobSpec] = {
    spec.name: spec
    for spec in (
        JobSpec("arxiv", ("scripts.cli", "run", "-p", "3")),
        JobSpec("resource", ("scripts.cli", "run", "-p", "5")),
        JobSpec("code", ("scripts.cli", "run", "-p", "4")),
        JobSpec("papers", ("scripts.cli", "run", "-p", "1")),
        JobSpec("ai_news", ("scripts.cli", "run", "-p", "2")),
        JobSpec(
            "push_early",
            ("scripts.cli", "push", "--categories", "ai_news,code,resource"),
            push_only=True,
        ),
        JobSpec(
            "push_papers",
            ("scripts.cli", "push", "--categories", "papers"),
            push_only=True,
        ),
        JobSpec(
            "push_arxiv",
            ("scripts.cli", "push", "--categories", "arxiv"),
            push_only=True,
        ),
    )
}


# ---------------------------------------------------------------------------
# Crontab: the schedule source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CronJob:
    name: str
    hour: int
    minute: int

    def scheduled_at(self, day: date, tz: ZoneInfo) -> datetime:
        return datetime.combine(day, dt_time(self.hour, self.minute), tzinfo=tz)


@dataclass(frozen=True)
class Crontab:
    path: Path
    jobs: tuple[CronJob, ...]


def _is_python(token: str) -> bool:
    return token.rsplit("/", 1)[-1] in ("python", "python3")


def _parse_int_field(value: str, low: int, high: int, *, lineno: int) -> int:
    if not value.isdigit():
        raise CrontabError(f"line {lineno}: expected an integer, got {value!r}")
    parsed = int(value)
    if not low <= parsed <= high:
        raise CrontabError(f"line {lineno}: {parsed} outside {low}..{high}")
    return parsed


def parse_crontab(path: Path) -> Crontab:
    """Parse the subset of crontab syntax used by ``deploy/crontab.*``.

    Only ``M H * * *`` job lines and a ``*/N * * * *`` catch-up line are
    accepted.  Rejecting everything else keeps the file honest: a typo fails
    at container start, not silently at 03:00.
    """
    if not path.is_file():
        raise CrontabError(f"crontab not found: {path}")

    jobs: list[CronJob] = []
    seen: set[str] = set()
    catch_up_seen = False

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(maxsplit=5)
        if len(parts) != 6:
            raise CrontabError(f"line {lineno}: expected 5 time fields and a command")
        minute, hour, dom, mon, dow, command = parts
        if (dom, mon, dow) != ("*", "*", "*"):
            raise CrontabError(f"line {lineno}: only '* * *' day fields are supported")

        tokens = command.split()
        if (
            len(tokens) == 4
            and _is_python(tokens[0])
            and tokens[1:4] == _CATCH_UP_PREFIX
        ):
            if not _EVERY_MINUTES.fullmatch(minute) or hour != "*":
                raise CrontabError(
                    f"line {lineno}: catch-up must run every N minutes (got {minute} {hour})"
                )
            catch_up_seen = True
            continue

        if not (
            len(tokens) == 5
            and _is_python(tokens[0])
            and tokens[1:4] == _RUN_JOB_PREFIX
        ):
            raise CrontabError(
                f"line {lineno}: unsupported command; use "
                "'python -m scripts.scheduler run-job <name>'"
            )

        name = tokens[4]
        if name not in JOB_SPECS:
            raise CrontabError(f"line {lineno}: unknown job {name!r}")
        if name in seen:
            raise CrontabError(f"line {lineno}: duplicate job {name!r}")
        seen.add(name)
        jobs.append(
            CronJob(
                name=name,
                hour=_parse_int_field(hour, 0, 23, lineno=lineno),
                minute=_parse_int_field(minute, 0, 59, lineno=lineno),
            )
        )

    if not jobs:
        raise CrontabError(f"{path}: no run-job lines")
    if not catch_up_seen:
        raise CrontabError(f"{path}: missing the periodic catch-up line")

    jobs.sort(key=lambda job: (job.hour, job.minute))
    return Crontab(path=path, jobs=tuple(jobs))


def jobs_mode() -> str:
    mode = os.environ.get("DAILYINFO_SCHEDULER_JOBS", DEFAULT_JOBS_MODE)
    if mode not in JOB_MODES:
        raise SchedulerConfigError(
            f"DAILYINFO_SCHEDULER_JOBS must be one of {JOB_MODES}, got {mode!r}"
        )
    return mode


def crontab_path(mode: str | None = None) -> Path:
    explicit = os.environ.get("DAILYINFO_SCHEDULER_CRONTAB", "")
    if explicit:
        return Path(explicit)
    selected = mode or jobs_mode()
    # The image installs the crontabs under /etc.  Docker healthchecks and
    # `docker compose exec` do NOT inherit exports made by the entrypoint, so
    # this lookup must work without DAILYINFO_SCHEDULER_CRONTAB being set.
    installed = INSTALLED_CRONTAB_DIR / f"crontab.{selected}"
    if installed.is_file():
        return installed
    return PROJECT_ROOT / "deploy" / f"crontab.{selected}"


def timezone() -> ZoneInfo:
    name = os.environ.get("DAILYINFO_SCHEDULER_TZ", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SchedulerConfigError(
            f"timezone {name!r} is unavailable; install tzdata in the image"
        ) from exc


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class JobState:
    success_date: str = ""
    last_attempt_date: str = ""
    last_attempt_at: str = ""
    attempts_today: int = 0
    next_attempt_at: str = ""
    last_exit_code: int | None = None
    last_duration_s: float = 0.0


@dataclass
class SchedulerState:
    started_at: str = ""
    # Updated on every container start (not on every catch-up sweep).  A
    # restart must get a grace window to replay the day, but the window must
    # not slide forward every 15 minutes or a persistent failure never turns
    # the healthcheck red.
    last_started_at: str = ""
    jobs: dict[str, JobState] = field(default_factory=dict)

    def job(self, name: str) -> JobState:
        return self.jobs.setdefault(name, JobState())


def state_path() -> Path:
    return paths.STATE_DIR / "scheduler_state.json"


def lock_path() -> Path:
    return paths.STATE_DIR / "scheduler.lock"


def _state_to_dict(state: SchedulerState) -> dict:
    return {
        "version": STATE_VERSION,
        "started_at": state.started_at,
        "last_started_at": state.last_started_at,
        "jobs": {
            name: {
                "success_date": job.success_date,
                "last_attempt_date": job.last_attempt_date,
                "last_attempt_at": job.last_attempt_at,
                "attempts_today": job.attempts_today,
                "next_attempt_at": job.next_attempt_at,
                "last_exit_code": job.last_exit_code,
                "last_duration_s": job.last_duration_s,
            }
            for name, job in sorted(state.jobs.items())
        },
    }


def _job_state_from_dict(raw: object) -> JobState:
    if not isinstance(raw, dict):
        raise ValueError("job state is not an object")
    exit_code = raw.get("last_exit_code")
    return JobState(
        success_date=str(raw.get("success_date", "") or ""),
        last_attempt_date=str(raw.get("last_attempt_date", "") or ""),
        last_attempt_at=str(raw.get("last_attempt_at", "") or ""),
        attempts_today=int(raw.get("attempts_today", 0) or 0),
        next_attempt_at=str(raw.get("next_attempt_at", "") or ""),
        last_exit_code=exit_code if isinstance(exit_code, int) else None,
        last_duration_s=float(raw.get("last_duration_s", 0.0) or 0.0),
    )


def _quarantine_state(path: Path) -> None:
    suffix = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{suffix}")
    try:
        os.replace(path, target)
    except OSError:
        pass


def load_state() -> SchedulerState:
    """Read state; quarantine an unreadable file and raise."""
    path = state_path()
    if not path.exists():
        return SchedulerState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state root is not an object")
        version = raw.get("version")
        if version is not None and version != STATE_VERSION:
            raise ValueError(f"unsupported state version {version!r}")
        jobs_raw = raw.get("jobs", {})
        if not isinstance(jobs_raw, dict):
            raise ValueError("jobs is not an object")
        return SchedulerState(
            started_at=str(raw.get("started_at", "") or ""),
            last_started_at=str(raw.get("last_started_at", "") or ""),
            jobs={
                name: _job_state_from_dict(value) for name, value in jobs_raw.items()
            },
        )
    except (OSError, ValueError, TypeError) as exc:
        _quarantine_state(path)
        raise StateCorruptError(f"state file {path} is unreadable: {exc}") from exc


def save_state(state: SchedulerState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_state_to_dict(state), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Atomic replacement still holds where directory fsync is unsupported.
            pass
    except OSError as exc:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise SchedulerConfigError(f"could not write {path}: {exc}") from exc


def _load_state_for_run() -> SchedulerState:
    try:
        return load_state()
    except StateCorruptError as exc:
        log(f"WARNING: {exc}; starting from an empty state")
        return SchedulerState()


def mark_started(now: datetime | None = None, *, restart: bool = False) -> None:
    """Record first use and, on ``restart=True``, this container start.

    ``catch_up`` calls this without ``restart`` to fill a fresh data root.
    The entrypoint calls the ``mark-started`` CLI subcommand with ``restart``
    on every boot so a replay after a restart gets a bounded grace window.
    """
    tz = timezone()
    now = now or datetime.now(tz)
    lock = try_acquire_lock()
    if lock is None:
        return
    try:
        state = _load_state_for_run()
        changed = False
        if not state.started_at:
            state.started_at = now.isoformat()
            changed = True
        if restart or not state.last_started_at:
            state.last_started_at = now.isoformat()
            changed = True
        if changed:
            save_state(state)
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def try_acquire_lock() -> IO[str] | None:
    try:
        lock_path().parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path(), "a+", encoding="utf-8")
    except OSError as exc:
        raise SchedulerConfigError(
            f"cannot open scheduler lock {lock_path()}: {exc}; the data root "
            "must be writable by the container user (see deploy/bootstrap.sh)"
        ) from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def release_lock(handle: IO[str]) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# Running jobs
# ---------------------------------------------------------------------------


def log(message: str, *, now: datetime | None = None) -> None:
    if now is None:
        try:
            now = datetime.now(timezone())
        except SchedulerConfigError:
            # A broken tzdata/timezone must still produce a log line; the
            # error path in main() would otherwise raise a second time.
            now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{stamp}] {message}", flush=True)


def _child_env(spec: JobSpec) -> dict[str, str]:
    """Push jobs keep Discord credentials; collection children never see them."""
    env = dict(os.environ)
    if not spec.push_only:
        for key in list(env):
            if key.startswith("DISCORD_"):
                del env[key]
    return env


def _run_with_timeout(
    command: list[str], timeout: int, *, env: dict[str, str] | None = None
) -> int:
    process = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # subprocess.run would kill only the CLI wrapper.  The pipeline is a
        # grandchild, so kill the whole session; otherwise it keeps writing
        # after the lock is released and catch-up starts a second writer.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (AttributeError, OSError):
            # Windows has no killpg/SIGKILL; the process group is the fallback.
            process.kill()
        process.wait()
        raise


def _default_runner(spec: JobSpec, timeout: int) -> int:
    command = [sys.executable, "-m", *spec.argv]
    log(f"job {spec.name}: start {' '.join(command)}")
    returncode = _run_with_timeout(command, timeout, env=_child_env(spec))
    log(f"job {spec.name}: exit {returncode}")
    return returncode


JobRunner = Callable[[JobSpec, int], int]


def _execute(spec: JobSpec, runner: JobRunner | None) -> int:
    try:
        if runner is not None:
            return runner(spec, JOB_TIMEOUT_SECONDS)
        return _default_runner(spec, JOB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        log(f"job {spec.name}: timed out after {JOB_TIMEOUT_SECONDS}s")
        return 124
    except OSError as exc:
        log(f"job {spec.name}: could not start: {exc}")
        return 127


def _parse_iso(value: str, tz: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Hand-edited or legacy state without an offset must not crash an
        # aware comparison later.
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _record_attempt(
    state: SchedulerState,
    name: str,
    now: datetime,
    exit_code: int,
    duration: float,
) -> JobState:
    job = state.job(name)
    today = now.date().isoformat()
    job.last_attempt_date = today
    job.last_attempt_at = now.isoformat()
    job.last_exit_code = exit_code
    job.last_duration_s = round(duration, 3)
    job.attempts_today += 1

    if exit_code == 0:
        job.success_date = today
        job.attempts_today = 0
        job.next_attempt_at = ""
    elif job.attempts_today < MAX_ATTEMPTS_PER_DAY:
        delay = RETRY_BACKOFF_SECONDS[
            min(job.attempts_today - 1, len(RETRY_BACKOFF_SECONDS) - 1)
        ]
        job.next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
    else:
        job.next_attempt_at = ""
    return job


def run_job(
    name: str,
    *,
    now: datetime | None = None,
    runner: JobRunner | None = None,
) -> int:
    """Run one named job under the global lock and record the outcome.

    Returns the job's exit code.  A busy lock is a skip, not a failure: the
    periodic catch-up sweep will run the job once the current one finishes.
    """
    spec = JOB_SPECS.get(name)
    if spec is None:
        raise SchedulerConfigError(f"unknown job {name!r}")

    tz = timezone()
    now = now or datetime.now(tz)
    lock = try_acquire_lock()
    if lock is None:
        log(f"job {name}: skipped, another job holds the lock", now=now)
        return 0

    try:
        state = _load_state_for_run()
        if not state.started_at:
            state.started_at = now.isoformat()
        if not state.last_started_at:
            state.last_started_at = now.isoformat()

        today = now.date().isoformat()
        job = state.job(name)
        if job.success_date == today:
            log(f"job {name}: already succeeded today", now=now)
            save_state(state)
            return 0
        if job.last_attempt_date != today:
            job.attempts_today = 0
            job.next_attempt_at = ""

        # Refuse to execute when the outcome cannot be recorded.  Otherwise an
        # unwritable /data makes every 15-minute sweep run the job again,
        # bypassing the retry cap and the backoff.
        try:
            save_state(state)
        except SchedulerConfigError as exc:
            log(f"job {name}: refusing to run, state is not writable: {exc}", now=now)
            return EXIT_STATE_UNWRITABLE

        started = time.monotonic()
        exit_code = _execute(spec, runner)
        duration = time.monotonic() - started
        _record_attempt(state, name, now, exit_code, duration)
        save_state(state)
        log(f"job {name}: recorded exit {exit_code} in {duration:.1f}s", now=now)
        return exit_code
    finally:
        release_lock(lock)


def _job_due(
    job: CronJob,
    state: SchedulerState,
    now: datetime,
    tz: ZoneInfo,
) -> bool:
    scheduled = job.scheduled_at(now.date(), tz)
    if scheduled > now:
        return False

    today = now.date().isoformat()
    job_state = state.jobs.get(job.name)
    if job_state is None:
        return True
    if job_state.success_date == today:
        return False
    if (
        job_state.last_attempt_date == today
        and job_state.attempts_today >= MAX_ATTEMPTS_PER_DAY
    ):
        return False
    next_attempt = (
        _parse_iso(job_state.next_attempt_at, tz) if job_state.next_attempt_at else None
    )
    if next_attempt is not None and now < next_attempt:
        return False
    return True


def catch_up(
    *,
    now: datetime | None = None,
    runner: JobRunner | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Run every job that is due today and has not succeeded yet.

    Invoked once at container start and every 15 minutes by cron.  It is a
    no-op when nothing is due, which is the common case.

    ``now`` freezes the sweep for due selection.  Each job is recorded with
    ``clock()``, so a sweep that starts at 03:00 and reaches push_papers at
    03:20 records 03:20, not 03:00.  When a test passes ``now`` without a
    clock, the clock defaults to that same instant.
    """
    tz = timezone()
    if now is not None:
        clock = clock or (lambda: now)
    else:
        clock = clock or (lambda: datetime.now(tz))
    sweep_now = now or clock()
    crontab = parse_crontab(crontab_path())

    # Fill a fresh data root.  Restart bookkeeping is done by the entrypoint's
    # ``mark-started`` call, which sets last_started_at every boot.
    mark_started(sweep_now)

    for job in crontab.jobs:
        try:
            state = _load_state_for_run()
        except SchedulerConfigError as exc:
            log(f"catch-up: cannot read state: {exc}", now=sweep_now)
            return 1

        if not _job_due(job, state, sweep_now, tz):
            continue

        lock = try_acquire_lock()
        if lock is None:
            log(
                "catch-up: another job is running; the next sweep will retry",
                now=sweep_now,
            )
            return 0
        release_lock(lock)

        run_job(job.name, now=clock(), runner=runner)

    return 0


def healthcheck(*, now: datetime | None = None) -> int:
    """Exit 1 when a job that should have finished today has not succeeded."""
    tz = timezone()
    now = now or datetime.now(tz)
    crontab = parse_crontab(crontab_path())

    try:
        state = load_state()
    except StateCorruptError as exc:
        print(f"unhealthy: {exc}", flush=True)
        return 1

    if not state.started_at:
        # Nothing has ever run from this data root; there is no history to
        # judge.  The entrypoint runs catch-up before supercronic, so a live
        # scheduler always has a state file by the time health matters.
        return 0

    started_at = _parse_iso(state.started_at, tz)
    last_started_at = _parse_iso(state.last_started_at, tz) or started_at
    failures: list[str] = []

    for job in crontab.jobs:
        occurrence = job.scheduled_at(now.date(), tz)
        if occurrence > now:
            occurrence -= timedelta(days=1)
        is_today = occurrence.date() == now.date()

        # A container that started today is not responsible for yesterday's
        # run.  Today's occurrences always count: catch-up replays them, so a
        # failed replay must turn the check red instead of being excused.
        if not is_today and started_at is not None and occurrence < started_at:
            continue

        deadline = occurrence + timedelta(seconds=JOB_TIMEOUT_SECONDS)
        if is_today and last_started_at is not None:
            # Give a fresh boot time to replay the day, but anchor the window
            # to the boot, not to each sweep.
            deadline = max(
                deadline, last_started_at + timedelta(seconds=JOB_TIMEOUT_SECONDS)
            )
        if now <= deadline:
            continue

        job_state = state.jobs.get(job.name)
        if (
            job_state is not None
            and job_state.success_date == occurrence.date().isoformat()
        ):
            continue

        failures.append(
            f"{job.name}: scheduled {occurrence.isoformat()}, "
            f"last attempt {job_state.last_attempt_at if job_state else 'never'}"
        )

    if failures:
        print("unhealthy: overdue jobs without a success today", flush=True)
        for failure in failures:
            print(f"  {failure}", flush=True)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Environment verification and manual execution
# ---------------------------------------------------------------------------


def verify_env(
    *,
    now: datetime | None = None,
    local_offset: timedelta | None = None,
) -> None:
    """Fail fast when the container clock, mode or credentials are wrong."""
    tz = timezone()
    now = now or datetime.now(tz)

    model_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not model_key or "your_" in model_key:
        raise SchedulerConfigError(
            "DEEPSEEK_API_KEY is missing (set it in the project .env)"
        )

    if local_offset is None:
        local_offset = datetime.now().astimezone().utcoffset()
    expected_offset = now.astimezone(tz).utcoffset()
    if local_offset != expected_offset:
        raise SchedulerConfigError(
            f"container local offset {local_offset} does not match {tz.key} "
            f"({expected_offset}); supercronic would run jobs at the wrong wall time"
        )

    # The lock and state live under STATE_DIR.  Fail here with an actionable
    # message instead of a PermissionError in the first job.
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        probe = paths.STATE_DIR / f".write-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise SchedulerConfigError(
            f"data root {paths.WORKSPACE_ROOT} is not writable: {exc}; "
            "run deploy/bootstrap.sh or fix ownership"
        ) from exc

    mode = jobs_mode()
    path = crontab_path(mode)
    parsed = parse_crontab(path)

    expected_names = {
        name for name, spec in JOB_SPECS.items() if mode == "full" or not spec.push_only
    }
    actual_names = {job.name for job in parsed.jobs}
    if actual_names != expected_names:
        raise SchedulerConfigError(
            f"{path} schedules {sorted(actual_names)}, expected {sorted(expected_names)}"
        )

    if mode == "full":
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token or "your_" in token:
            raise SchedulerConfigError(
                "full mode requires DISCORD_BOT_TOKEN (set it in the project .env), "
                "or use DAILYINFO_SCHEDULER_JOBS=collect"
            )
        # A missing channel makes push_to_discord skip that category and still
        # exit 0, which would mark the push job successful with nothing sent.
        missing = [
            category
            for category in ("papers", "ai_news", "code", "resource")
            if not paths.get_channel_id(category)
        ]
        if not paths.get_channel_id("arxiv") and not paths.get_channel_id("ai_news"):
            missing.append("arxiv")
        if missing:
            raise SchedulerConfigError(
                "full mode is missing Discord channel ids: " + ", ".join(missing)
            )

    log(f"verify-env: tz={tz.key} mode={mode} crontab={path}", now=now)
    for job in parsed.jobs:
        log(
            f"  {job.name:<12} daily {job.hour:02d}:{job.minute:02d}",
            now=now,
        )


def exec_command(argv: list[str]) -> int:
    """Run a manual command under the same lock as the scheduled jobs."""
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise SchedulerConfigError("exec requires a command")

    lock = try_acquire_lock()
    if lock is None:
        log("exec: a scheduled job is running; refusing to run concurrently")
        return EXIT_LOCKED
    try:
        try:
            return subprocess.run(argv, check=False).returncode
        except OSError as exc:
            log(f"exec: cannot start {argv[0]!r}: {exc}")
            return 2
    finally:
        release_lock(lock)


def print_status(*, now: datetime | None = None) -> int:
    tz = timezone()
    now = now or datetime.now(tz)
    crontab = parse_crontab(crontab_path())
    try:
        state = load_state()
    except StateCorruptError as exc:
        print(f"state: {exc}", flush=True)
        return 1

    print(f"mode={jobs_mode()} tz={tz.key} now={now.isoformat()}", flush=True)
    print(f"started_at={state.started_at or '(never)'}", flush=True)
    for job in crontab.jobs:
        job_state = state.jobs.get(job.name)
        if job_state is None:
            print(f"  {job.name:<12} never run", flush=True)
            continue
        print(
            f"  {job.name:<12} last={job_state.last_attempt_at or '-'} "
            f"exit={job_state.last_exit_code} "
            f"success={job_state.success_date or '-'} "
            f"attempts_today={job_state.attempts_today}",
            flush=True,
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.scheduler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-job")
    run_parser.add_argument("name")
    subparsers.add_parser("catch-up")
    subparsers.add_parser("healthcheck")
    subparsers.add_parser("verify-env")
    subparsers.add_parser("mark-started")
    subparsers.add_parser("status")
    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("argv", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    try:
        if args.command == "run-job":
            return run_job(args.name)
        if args.command == "catch-up":
            return catch_up()
        if args.command == "healthcheck":
            return healthcheck()
        if args.command == "verify-env":
            verify_env()
            return 0
        if args.command == "mark-started":
            mark_started(restart=True)
            return 0
        if args.command == "status":
            return print_status()
        if args.command == "exec":
            return exec_command(args.argv)
    except (SchedulerConfigError, CrontabError) as exc:
        log(f"ERROR: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
