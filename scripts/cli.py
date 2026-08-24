#!/usr/bin/env python3
"""dailyinfo CLI — daily briefing pipeline manager.

Usage:
    dailyinfo install    # Validate env and create workspace directories
    dailyinfo start      # Start FreshRSS container
    dailyinfo stop       # Stop services
    dailyinfo restart    # Restart FreshRSS container
    dailyinfo run        # Run all pipelines
    dailyinfo run -p 2   # Run a specific pipeline
    dailyinfo push       # Push today's briefings to Discord
    dailyinfo status     # Show briefing / pushed file counts
    dailyinfo logs       # Tail execution log
"""

import os
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Ensure flat imports like ``from paths import ...`` resolve when this module
# is loaded via the ``scripts.cli:cli`` console-script entry point, where
# ``sys.path`` does not include ``scripts/`` by default. Direct invocations
# (``python scripts/cli.py``) already have it in ``sys.path[0]``.
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import click  # noqa: E402

from paths import (  # noqa: E402
    BRIEFINGS_DIR,
    CURRENT_ENV,
    FRESHRSS_DATA,
    PUSHED_DIR,
    STATE_DIR,
    WORKSPACE_ROOT,
)

from clean_cache import clean_stale_cache  # noqa: E402

SCRIPTS_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPTS_DIR.parent
DATE = datetime.now().strftime("%Y-%m-%d")
ENV_FILE = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / "logs"


def _env_banner() -> str:
    """Return a short env tag for display (e.g. '[env:dev]')."""
    return f"[env:{CURRENT_ENV}]"


CATEGORIES = ["papers", "ai_news", "code", "resource", "arxiv", "conference"]


def _python() -> str:
    return sys.executable


def _read_env_keys(keys: list[str]) -> dict[str, str]:
    """Parse selected keys from .env as plain text (no configparser).

    Matches the parsing style used by run_pipelines.py.
    """
    result = {k: "" for k in keys}
    if not ENV_FILE.exists():
        return result
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in result:
                result[key] = val.strip().strip('"').strip("'")
    return result


def _ensure_workspace() -> None:
    """Create ~/.myagentdata/dailyinfo/{freshrss/data,briefings/*,pushed/*}."""
    FRESHRSS_DATA.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        BRIEFINGS_DIR.joinpath(category).mkdir(parents=True, exist_ok=True)
        PUSHED_DIR.joinpath(category).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("dailyinfo")
except Exception:
    __version__ = "0.0.0"


@click.group()
@click.version_option(version=__version__)
def cli():
    """dailyinfo — daily briefing pipeline manager."""
    pass


@cli.command()
def install():
    """Validate environment and create workspace directories.

    Scheduling is delegated to any external cron (system crontab, systemd
    timer, agent runtime such as myopenclaw's hermes cron, etc.).
    This command does NOT write to the host crontab.
    """
    click.echo(f"==> DailyInfo Environment Setup {_env_banner()}")

    click.echo("[1/3] Checking .env configuration...")
    if not ENV_FILE.exists():
        click.echo(f"  ERROR: .env not found at {ENV_FILE}")
        click.echo("  Run: cp .env.example .env and fill in your keys")
        sys.exit(1)

    # Determine which channel keys to validate based on current environment.
    from paths import env_suffix

    suffix = env_suffix()
    required = ["DEEPSEEK_API_KEY", "DISCORD_BOT_TOKEN"]
    channel_keys = [
        f"DISCORD_CHANNEL_PAPERS{suffix}",
        f"DISCORD_CHANNEL_AI_NEWS{suffix}",
        f"DISCORD_CHANNEL_CODE{suffix}",
        f"DISCORD_CHANNEL_RESOURCE{suffix}",
        f"DISCORD_CHANNEL_ARXIV{suffix}",
        f"DISCORD_CHANNEL_CONFERENCE{suffix}",
    ]
    env = _read_env_keys(required + channel_keys)

    missing = [k for k in required if not env[k] or "your_" in env[k]]
    if missing:
        click.echo(f"  ERROR: empty or placeholder values for: {', '.join(missing)}")
        sys.exit(1)

    unset_channels = [k for k in channel_keys if not env[k]]
    if unset_channels:
        click.echo(
            f"  WARN: no channel id for {', '.join(unset_channels)} "
            f"— those categories will be skipped at push time"
        )
    click.echo("  .env validated")

    click.echo("[2/3] Creating workspace directories...")
    _ensure_workspace()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    click.echo(f"  Created: {WORKSPACE_ROOT}")
    click.echo(f"  Created: {FRESHRSS_DATA}")

    click.echo("[3/3] Installing Python dependencies...")
    try:
        result = subprocess.run(
            ["uv", "sync", "--python", "python3"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.echo("  Dependencies installed via uv")
        else:
            raise FileNotFoundError
    except FileNotFoundError:
        result = subprocess.run(
            [_python(), "-m", "pip", "install", "-e", "."],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(f"  ERROR: pip install failed: {result.stderr}")
            sys.exit(1)
        click.echo("  Dependencies installed via pip")

    click.echo("")
    click.echo("Setup complete. Next steps:")
    click.echo("  1. dailyinfo start         # start FreshRSS (http://localhost:8081)")
    click.echo("  2. dailyinfo run           # generate briefings")
    click.echo("  3. dailyinfo push          # push today's briefings to Discord")
    click.echo("")
    click.echo("Scheduling is expected to be driven by an external cron")
    click.echo(
        "(system crontab, systemd timer, hermes cron, ...) calling these commands."
    )


@cli.command()
def start():
    """Start Docker services (FreshRSS)."""
    click.echo("==> Starting Docker services...")
    compose_file = PROJECT_ROOT / "docker-compose.yml"
    if not compose_file.exists():
        click.echo("  ERROR: docker-compose.yml not found")
        sys.exit(1)

    result = subprocess.run(
        ["docker", "compose", "up", "-d", "freshrss"],
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        click.echo("  ERROR: Failed to start services")
        sys.exit(1)

    click.echo("  FreshRSS started")
    click.echo("  URL: http://localhost:8081")


@cli.command()
def stop():
    """Stop Docker services."""
    click.echo("==> Stopping Docker services...")
    result = subprocess.run(["docker", "compose", "down"], cwd=PROJECT_ROOT)
    if result.returncode != 0:
        click.echo("  ERROR: Failed to stop services")
        sys.exit(1)
    click.echo("  Services stopped")


@cli.command()
def restart():
    """Restart Docker services."""
    click.echo("==> Restarting Docker services...")
    result = subprocess.run(
        ["docker", "compose", "restart", "freshrss"], cwd=PROJECT_ROOT
    )
    if result.returncode != 0:
        click.echo("  ERROR: Failed to restart services")
        sys.exit(1)
    click.echo("  FreshRSS restarted")


@cli.command()
@click.option(
    "--pipeline",
    "-p",
    type=click.Choice(["1", "2", "3", "4", "5", "6", "all"]),
    default="all",
    help="Pipeline to run: 1=papers, 2=ai_news, 3=arxiv, 4=code, 5=resource, 6=conference.",
)
@click.option(
    "-f",
    "--force",
    multiple=True,
    metavar="SOURCE",
    help="Force regenerate today's briefing. Pass 'all' to refresh everything "
    "or a source name (e.g. 'arxiv_cs_ai'). Repeatable.",
)
@click.option(
    "--source",
    multiple=True,
    metavar="SOURCE",
    help="Only run a named configured source. Repeatable.",
)
def run(pipeline, force, source):
    """Scrape sources, generate AI summaries, save briefing files.

    By default, sources whose today's briefing already exists are skipped;
    pass --force to bypass the skip check for specific sources or all.
    """
    script = SCRIPTS_DIR / "run_pipelines.py"
    cmd = [_python(), str(script)]
    if pipeline != "all":
        cmd += ["--pipeline", pipeline]
    for src in force:
        cmd += ["--force", src]
    for src in source:
        cmd += ["--source", src]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
@click.option(
    "-d",
    "--date",
    "date_str",
    default=None,
    help="Date to push in YYYY-MM-DD format. Defaults to today; use this to backfill.",
)
@click.option(
    "-c",
    "--categories",
    default=None,
    help=(
        "Comma-separated list of categories to push "
        "(e.g. 'papers,ai_news,code,resource' or 'weekly'). "
        "Defaults to all daily categories."
    ),
)
def push(date_str, categories):
    """Push briefings for the given date (default: today) to Discord channels.

    Use --categories to restrict which channels are pushed.
    Morning cron omits 'weekly'; noon cron passes 'weekly' only.
    """
    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            click.echo(f"Error: --date must be YYYY-MM-DD (got {date_str!r})", err=True)
            sys.exit(2)

    script = SCRIPTS_DIR / "push_to_discord.py"
    cmd = [_python(), str(script)]
    if date_str:
        cmd += ["--date", date_str]
    if categories:
        cmd += ["--categories", categories]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
@click.option("--days", default=7, show_default=True, help="Lookback window in days.")
@click.option("--force", is_flag=True, help="Overwrite existing recap for today.")
def weekly(days, force):
    """Generate a weekly AI news recap from the past N days of briefings."""
    script = SCRIPTS_DIR / "weekly_summary.py"
    cmd = [_python(), str(script), "--days", str(days)]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def status():
    """Show today's briefing and pushed file counts."""
    total_pending = 0

    click.echo(f"Briefings for {DATE} {_env_banner()}:")
    click.echo(f"  Workspace: {WORKSPACE_ROOT}")
    for cat in CATEGORIES:
        path = BRIEFINGS_DIR / cat
        if path.is_dir():
            files = [f for f in sorted(path.iterdir()) if DATE in f.name]
            total_pending += len(files)
            click.echo(f"  {cat:15s}: {len(files):3d} files")
        else:
            click.echo(f"  {cat:15s}: directory missing")

    click.echo("")
    click.echo("Already pushed today:")
    for cat in CATEGORIES:
        path = PUSHED_DIR / cat
        if path.is_dir():
            files = [f for f in sorted(path.iterdir()) if DATE in f.name]
            if files:
                click.echo(f"  {cat:15s}: {len(files):3d} files")

    click.echo(f"\nTotal pending: {total_pending} files")
    state_path = STATE_DIR / "openreview.sqlite3"
    if state_path.exists():
        try:
            from conference import ConferenceState

            summaries = ConferenceState(state_path).source_summary()
            if summaries:
                click.echo("\nOpenReview venues:")
                for item in summaries:
                    click.echo(
                        f"  {item['source']:24s}: {item.get('last_outcome') or '-':13s} "
                        f"tracked={item.get('tracked', 0)} pending={item.get('pending', 0)}"
                    )
                    if item.get("run_status") in {"RUNNING", "INTERRUPTED"}:
                        total = item.get("run_total") or "?"
                        click.echo(
                            f"    run {item.get('run_status')} "
                            f"phase={item.get('run_phase') or '-'} "
                            f"fetched={item.get('run_fetched', 0)}/{total} "
                            f"candidates={item.get('run_candidates', 0)} "
                            f"evaluated={item.get('run_evaluated', 0)} "
                            f"relevant={item.get('run_relevant', 0)}"
                        )
        except Exception as exc:
            click.echo(f"  OpenReview state unavailable: {exc}")


@cli.command()
def logs():
    """Tail the pipeline execution log."""
    log_file = LOGS_DIR / "dailyinfo.log"
    if not log_file.exists():
        click.echo(f"Log file not found: {log_file}")
        sys.exit(1)
    result = subprocess.run(["tail", "-n", "100", str(log_file)])
    sys.exit(result.returncode)


def _source_url(source_name: str) -> str:
    """Resolve a configured source URL from config/sources.json."""
    sources_path = PROJECT_ROOT / "config" / "sources.json"
    with open(sources_path, encoding="utf-8") as f:
        cfg = json.load(f)
    for source in cfg.get("sources", []):
        if source.get("name") == source_name:
            return source.get("url", "")
    return ""


@cli.command("cache-clear")
@click.option(
    "--source",
    default="arxiv_cs_ai",
    show_default=True,
    help="Configured source name whose FreshRSS cache should be cleared.",
)
@click.option(
    "--url", default="", help="Explicit feed URL to clear instead of --source."
)
@click.option(
    "--all-stale", is_flag=True, help="Clear every stale SimplePie cache entry."
)
@click.option(
    "--max-age",
    default=24,
    show_default=True,
    help="Stale threshold in hours for --all-stale.",
)
@click.option(
    "--dry-run", is_flag=True, help="Show what would be deleted without deleting."
)
@click.option(
    "--refresh", is_flag=True, help="Run FreshRSS actualize_script.php after clearing."
)
@click.option(
    "--container",
    default="dailyinfo_freshrss",
    show_default=True,
    help="FreshRSS container name for --refresh.",
)
def cache_clear(source, url, all_stale, max_age, dry_run, refresh, container):
    """Clear FreshRSS SimplePie cache files for stuck feeds (see issue #57)."""
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from freshrss_cache import (
        clear_stale_caches,
        delete_cache_files,
        find_cache_files_for_url,
        find_stale_caches,
        refresh_freshrss,
    )
    from paths import FRESHRSS_DATA

    cache_dir = FRESHRSS_DATA / "cache"
    if not cache_dir.exists():
        click.echo(f"Cache directory not found: {cache_dir}")
        sys.exit(1)

    if all_stale:
        targets = find_stale_caches(cache_dir, max_age_hours=max_age)
        description = f"stale cache file(s) older than {max_age}h"
    else:
        feed_url = url or _source_url(source)
        if not feed_url:
            click.echo(f"Source not found or has no URL: {source}")
            sys.exit(1)
        targets = find_cache_files_for_url(cache_dir, feed_url)
        description = f"cache file(s) for {source if not url else feed_url}"

    if not targets:
        click.echo(f"No {description} found.")
        return

    click.echo(f"Found {len(targets)} {description}:")
    for f in targets:
        click.echo(f"  {f.name}")

    if dry_run:
        click.echo("Dry run — nothing deleted.")
        return

    count = (
        clear_stale_caches(cache_dir, max_age_hours=max_age)
        if all_stale
        else delete_cache_files(targets)
    )
    click.echo(f"Deleted {count} cache file(s).")

    if refresh:
        result = refresh_freshrss(container=container)
        if result.returncode != 0:
            click.echo("FreshRSS refresh failed.")
            sys.exit(result.returncode)
        click.echo("FreshRSS refresh triggered.")
    else:
        click.echo(
            "Run with --refresh or execute: "
            f"docker exec {container} php /var/www/FreshRSS/app/actualize_script.php"
        )


@cli.command("clean-cache")
@click.option(
    "--max-age",
    default=24,
    show_default=True,
    type=int,
    help="Maximum cache file age in hours. Files older than this are deleted.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be deleted without actually deleting anything.",
)
def clean_cache(max_age, dry_run):
    """Delete stale FreshRSS SimplePie cache files.

    Occasionally SimplePie cache entries get stuck (always "uses cache"
    instead of doing a real HTTP request), causing feeds to silently stop
    receiving new articles.  This command clears cache files older than
    --max-age hours.

    The default threshold of 24 hours is far longer than FreshRSS's normal
    refresh cycle (15 min), so healthy caches are never affected.
    """
    if max_age < 1:
        click.echo("Error: --max-age must be a positive integer", err=True)
        sys.exit(2)

    cache_dir = FRESHRSS_DATA / "cache"
    if not cache_dir.is_dir():
        click.echo(f"Cache directory not found: {cache_dir}")
        sys.exit(1)

    deleted, errors = clean_stale_cache(
        cache_dir, max_age_hours=max_age, dry_run=dry_run
    )

    if dry_run:
        if deleted == 0:
            click.echo("No stale cache files would be deleted.")
        else:
            click.echo(f"Would delete {deleted} stale cache file(s) (dry run).")
    else:
        if deleted == 0:
            click.echo("No stale cache files found.")
        else:
            msg = f"Cleaned {deleted} stale cache file(s)"
            if errors > 0:
                msg += f" ({errors} error(s))"
            msg += "."
            click.echo(msg)

    if errors > 0 and not dry_run:
        sys.exit(1)


if __name__ == "__main__":
    cli()
