#!/usr/bin/env python3
"""dailyinfo CLI — daily briefing pipeline manager.

Usage:
    dailyinfo install      # Initialize environment (one-time setup)
    dailyinfo start         # Start Docker services
    dailyinfo stop         # Stop Docker services
    dailyinfo restart     # Restart Docker services
    dailyinfo run         # Run all pipelines
    dailyinfo run -p 2   # Run specific pipeline
    dailyinfo push        # Push briefings to Discord
    dailyinfo status      # Show briefing file counts
    dailyinfo logs        # Tail execution log
"""

import configparser
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPTS_DIR.parent
DATE = datetime.now().strftime('%Y-%m-%d')

# New workspace path (moved from ~/.openclaw/)
WORKSPACE_ROOT = Path.home() / '.dailyinfo' / 'workspace'
BRIEFINGS_DIR = WORKSPACE_ROOT / 'briefings'
PUSHED_DIR = WORKSPACE_ROOT / 'pushed'
FRESHRSS_DATA = Path.home() / '.freshrss' / 'data'

# Legacy path (for migration)
LEGACY_BRIEFINGS = Path.home() / '.openclaw' / 'workspace' / 'briefings'
LEGACY_PUSHED = Path.home() / '.openclaw' / 'workspace' / 'pushed'

# Config files
ENV_FILE = PROJECT_ROOT / '.env'
CONFIG_DIR = PROJECT_ROOT / 'config'
SOURCES_JSON = CONFIG_DIR / 'sources.json'
LOGS_DIR = PROJECT_ROOT / 'logs'


def _python():
    return sys.executable


def _ensure_workspace():
    """Ensure workspace directories exist."""
    for d in ['papers', 'ai_news', 'code', 'resource']:
        BRIEFINGS_DIR.joinpath(d).mkdir(parents=True, exist_ok=True)
        PUSHED_DIR.joinpath(d).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------
@click.group()
@click.version_option(version='0.2.0')
def cli():
    """dailyinfo — daily briefing pipeline manager."""
    pass


@cli.command()
def install():
    """Initialize environment (one-time setup).

    - Validates .env configuration
    - Creates workspace directories
    - Installs Python dependencies (via uv)
    - Sets up crontab for scheduled runs
    """
    click.echo("==> DailyInfo Environment Setup")
    click.echo("")

    # 1. Check .env
    click.echo("[1/5] Checking .env configuration...")
    if not ENV_FILE.exists():
        click.echo(f"  ERROR: .env not found at {ENV_FILE}")
        click.echo("  Run: cp .env.example .env and edit it with your API keys")
        sys.exit(1)

    # Validate required keys
    config = configparser.ConfigParser()
    config.read(ENV_FILE)

    missing = []
    if not config.has_option('DEFAULT', 'OPENROUTER_API_KEY'):
        missing.append('OPENROUTER_API_KEY')
    if not config.has_option('DEFAULT', 'DISCORD_BOT_TOKEN'):
        missing.append('DISCORD_BOT_TOKEN')

    if missing:
        click.echo(f"  ERROR: Missing required keys in .env: {', '.join(missing)}")
        sys.exit(1)

    # Check API keys are not placeholders
    api_key = config.get('DEFAULT', 'OPENROUTER_API_KEY', fallback='')
    discord_token = config.get('DEFAULT', 'DISCORD_BOT_TOKEN', fallback='')

    if 'your_' in api_key or not api_key:
        click.echo("  ERROR: OPENROUTER_API_KEY is empty or placeholder")
        sys.exit(1)
    if 'your_' in discord_token or not discord_token:
        click.echo("  ERROR: DISCORD_BOT_TOKEN is empty or placeholder")
        sys.exit(1)

    click.echo("  .env validated")

    # 2. Create directories
    click.echo("[2/5] Creating workspace directories...")
    _ensure_workspace()
    FRESHRSS_DATA.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    click.echo(f"  Created: {WORKSPACE_ROOT}")
    click.echo(f"  Created: {FRESHRSS_DATA}")

    # 3. Install Python dependencies
    click.echo("[3/5] Installing Python dependencies...")
    try:
        # Try uv first
        result = subprocess.run(
            ['uv', 'sync', '--python', 'python3'],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            click.echo("  Dependencies installed via uv")
        else:
            raise FileNotFoundError
    except FileNotFoundError:
        # Fallback to pip
        result = subprocess.run(
            [_python(), '-m', 'pip', 'install', '-e', '.'],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(f"  ERROR: pip install failed: {result.stderr}")
            sys.exit(1)
        click.echo("  Dependencies installed via pip")

    # 4. Setup crontab
    click.echo("[4/5] Setting up crontab...")
    try:
        result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
    except Exception:
        existing = ""

    # Remove old dailyinfo entries
    lines = [l for l in existing.split('\n') if 'dailyinfo' not in l.lower()]
    lines.append("")
    lines.append("# DailyInfo - auto-generated by dailyinfo install")
    lines.append(f"{PROJECT_ROOT}")

    # Add new cron entries (pipelines at 06:00/06:15/06:30, push at 07:00)
    lines.append(f"0 6 * * * cd {PROJECT_ROOT} && {_python()} scripts/run_pipelines.py --pipeline 1 >> {LOGS_DIR}/pipeline1.log 2>&1")
    lines.append(f"15 6 * * * cd {PROJECT_ROOT} && {_python()} scripts/run_pipelines.py --pipeline 2 >> {LOGS_DIR}/pipeline2.log 2>&1")
    lines.append(f"30 6 * * * cd {PROJECT_ROOT} && {_python()} scripts/run_pipelines.py --pipeline 3 >> {LOGS_DIR}/pipeline3.log 2>&1")
    lines.append(f"0 7 * * * cd {PROJECT_ROOT} && {_python()} scripts/push_to_discord.py >> {LOGS_DIR}/discord_push.log 2>&1")

    crontab_file = Path('/tmp/dailyinfo_crontab')
    crontab_file.write_text('\n'.join(lines))
    subprocess.run(['crontab', str(crontab_file)], check=True)
    crontab_file.unlink()
    click.echo("  Crontab installed")

    # 5. Done
    click.echo("[5/5] Setup complete!")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Visit http://localhost:8081 to configure FreshRSS")
    click.echo("  2. Run: dailyinfo start  # Start FreshRSS")
    click.echo("  3. Run: dailyinfo run   # Generate briefings")


@cli.command()
def start():
    """Start Docker services (FreshRSS only).

    Use this to start FreshRSS for RSS feed collection.
    """
    click.echo("==> Starting Docker services...")

    # Check docker-compose.yml exists
    compose_file = PROJECT_ROOT / 'docker-compose.yml'
    if not compose_file.exists():
        click.echo(f"  ERROR: docker-compose.yml not found")
        sys.exit(1)

    result = subprocess.run(
        ['docker', 'compose', 'up', '-d', 'freshrss'],
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

    result = subprocess.run(
        ['docker', 'compose', 'down'],
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        click.echo("  ERROR: Failed to stop services")
        sys.exit(1)

    click.echo("  Services stopped")


@cli.command()
def restart():
    """Restart Docker services."""
    click.echo("==> Restarting Docker services...")

    result = subprocess.run(
        ['docker', 'compose', 'restart', 'freshrss'],
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        click.echo("  ERROR: Failed to restart services")
        sys.exit(1)

    click.echo("  FreshRSS restarted")


@cli.command()
@click.option('--pipeline', '-p', type=click.Choice(['1', '2', '3', 'all']), default='all',
              help='Pipeline to run: 1=RSS papers/news, 2=code trending, 3=university news. Default: all')
def run(pipeline):
    """Scrape sources, generate AI summaries, save briefing files."""
    script = SCRIPTS_DIR / 'run_pipelines.py'
    cmd = [_python(), str(script)]
    if pipeline != 'all':
        cmd += ['--pipeline', pipeline]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def push():
    """Push today's briefings to Discord channels."""
    script = SCRIPTS_DIR / 'push_to_discord.py'
    result = subprocess.run([_python(), str(script)], cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def status():
    """Show today's briefing file counts."""
    categories = ['papers', 'ai_news', 'code', 'resource']
    total = 0

    click.echo(f"Briefings for {DATE}:")
    for cat in categories:
        path = BRIEFINGS_DIR / cat
        if path.is_dir():
            files = [f for f in sorted(path.iterdir()) if DATE in f.name]
            total += len(files)
            click.echo(f"  {cat:15s}: {len(files):3d} files")
        else:
            click.echo(f"  {cat:15s}: directory missing")

    click.echo("")
    click.echo("Already pushed today:")
    for cat in categories:
        path = PUSHED_DIR / cat
        if path.is_dir():
            files = [f for f in sorted(path.iterdir()) if DATE in f.name]
            if files:
                click.echo(f"  {cat:15s}: {len(files):3d} files")

    click.echo(f"\nTotal pending: {total} files")


@cli.command()
def logs():
    """Tail the pipeline execution log."""
    log_file = LOGS_DIR / 'dailyinfo.log'
    if not log_file.exists():
        click.echo(f"Log file not found: {log_file}")
        sys.exit(1)

    result = subprocess.run(['tail', '-n', '100', str(log_file)])
    sys.exit(result.returncode)


if __name__ == '__main__':
    cli()