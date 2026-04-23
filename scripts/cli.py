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

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click

from paths import BRIEFINGS_DIR, FRESHRSS_DATA, PUSHED_DIR, WORKSPACE_ROOT

SCRIPTS_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPTS_DIR.parent
DATE = datetime.now().strftime("%Y-%m-%d")
ENV_FILE = PROJECT_ROOT / ".env"
LOGS_DIR = PROJECT_ROOT / "logs"

CATEGORIES = ["papers", "ai_news", "code", "resource"]


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
@click.group()
@click.version_option(version="0.3.0")
def cli():
    """dailyinfo — daily briefing pipeline manager."""
    pass


@cli.command()
def install():
    """Validate environment and create workspace directories.

    Scheduling is delegated to an external cron (e.g. myopenclaw hermes cron).
    This command does NOT write to the host crontab.
    """
    click.echo("==> DailyInfo Environment Setup")

    click.echo("[1/3] Checking .env configuration...")
    if not ENV_FILE.exists():
        click.echo(f"  ERROR: .env not found at {ENV_FILE}")
        click.echo("  Run: cp .env.example .env and fill in your keys")
        sys.exit(1)

    required = ["OPENROUTER_API_KEY", "DISCORD_BOT_TOKEN"]
    channel_keys = [
        "DISCORD_CHANNEL_PAPERS",
        "DISCORD_CHANNEL_AI_NEWS",
        "DISCORD_CHANNEL_CODE",
        "DISCORD_CHANNEL_RESOURCE",
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
    click.echo("(e.g. myopenclaw hermes cron) calling these commands.")


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
    type=click.Choice(["1", "2", "3", "all"]),
    default="all",
    help="Pipeline to run: 1=RSS papers/news, 2=code trending, 3=university news.",
)
def run(pipeline):
    """Scrape sources, generate AI summaries, save briefing files."""
    script = SCRIPTS_DIR / "run_pipelines.py"
    cmd = [_python(), str(script)]
    if pipeline != "all":
        cmd += ["--pipeline", pipeline]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def push():
    """Push today's briefings to Discord channels."""
    script = SCRIPTS_DIR / "push_to_discord.py"
    result = subprocess.run([_python(), str(script)], cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def status():
    """Show today's briefing and pushed file counts."""
    total_pending = 0

    click.echo(f"Briefings for {DATE}:")
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
