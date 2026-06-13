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

import click

from paths import BRIEFINGS_DIR, CURRENT_ENV, FRESHRSS_DATA, PUSHED_DIR, WORKSPACE_ROOT

SCRIPTS_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPTS_DIR.parent
DATE = datetime.now().strftime("%Y-%m-%d")
ENV_FILE = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / "logs"


def _env_banner() -> str:
    """Return a short env tag for display (e.g. '[env:dev]')."""
    return f"[env:{CURRENT_ENV}]"


CATEGORIES = ["papers", "ai_news", "code", "resource", "arxiv"]


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


def _run_zotero_brief(**kwargs) -> int:
    """Lazy import so normal CLI use does not require NotebookLM extras."""
    from zotero_notebooklm import run_zotero_brief

    return run_zotero_brief(**kwargs)


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
    type=click.Choice(["1", "2", "3", "4", "5", "all"]),
    default="all",
    help="Pipeline to run: 1=papers, 2=ai_news, 3=arxiv, 4=code, 5=resource.",
)
@click.option(
    "-f",
    "--force",
    multiple=True,
    metavar="SOURCE",
    help="Force regenerate today's briefing. Pass 'all' to refresh everything "
    "or a source name (e.g. 'arxiv_cs_ai'). Repeatable.",
)
def run(pipeline, force):
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
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command("zotero-brief")
@click.option(
    "-d",
    "--date",
    "date_str",
    default=None,
    help="Zotero dateAdded day to process in YYYY-MM-DD format. Defaults to today.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing local Zotero briefing.")
@click.option(
    "--artifact",
    type=click.Choice(["none", "audio", "video", "both"]),
    default="none",
    show_default=True,
    help="Optional NotebookLM artifact to generate after the markdown briefing.",
)
@click.option(
    "--manual-only",
    is_flag=True,
    help="Only prepare PDFs, source_index.md, and manual NotebookLM steps.",
)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="Maximum number of Zotero papers to include.",
)
@click.option(
    "--collection",
    default=None,
    help="Zotero collection name or key to restrict the run, e.g. water.",
)
@click.option(
    "--open-missing-pdfs",
    is_flag=True,
    help="Open inaccessible Zotero PDF attachments once, then retry copying.",
)
@click.option(
    "--pdf-wait-seconds",
    default=20,
    show_default=True,
    type=int,
    help="Seconds to wait after opening a Zotero PDF attachment.",
)
@click.option(
    "--notebooklm-home",
    default=None,
    help="NotebookLM profile directory. Also available as NOTEBOOKLM_HOME.",
)
@click.option(
    "--notebook-title",
    default=None,
    help="NotebookLM notebook title. Defaults to the target date.",
)
def zotero_brief(
    date_str,
    force,
    artifact,
    manual_only,
    limit,
    collection,
    open_missing_pdfs,
    pdf_wait_seconds,
    notebooklm_home,
    notebook_title,
):
    """Build a Zotero -> NotebookLM paper briefing package.

    This workflow does not call the OpenRouter summarizer used by
    ``dailyinfo run``. NotebookLM reads the uploaded PDFs and index.
    """
    if date_str:
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            click.echo(f"Error: --date must be YYYY-MM-DD (got {date_str!r})", err=True)
            sys.exit(2)
    if limit < 1:
        click.echo("Error: --limit must be a positive integer", err=True)
        sys.exit(2)
    if pdf_wait_seconds < 0:
        click.echo("Error: --pdf-wait-seconds must be zero or positive", err=True)
        sys.exit(2)

    result = _run_zotero_brief(
        date_str=date_str,
        force=force,
        artifact=artifact,
        manual_only=manual_only,
        limit=limit,
        collection=collection,
        open_missing_pdfs=open_missing_pdfs,
        pdf_wait_seconds=pdf_wait_seconds,
        notebooklm_home=notebooklm_home,
        notebook_title=notebook_title,
    )
    sys.exit(result)


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
        "Defaults to all five categories."
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


@cli.command()
def logs():
    """Tail the pipeline execution log."""
    log_file = LOGS_DIR / "dailyinfo.log"
    if not log_file.exists():
        click.echo(f"Log file not found: {log_file}")
        sys.exit(1)
    result = subprocess.run(["tail", "-n", "100", str(log_file)])
    sys.exit(result.returncode)


if __name__ == "__main__":
    cli()
