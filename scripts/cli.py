#!/usr/bin/env python3
"""dailyinfo CLI — run pipelines, push to Discord, check status."""

import click
import subprocess
import sys
import os
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
DATE = datetime.now().strftime('%Y-%m-%d')
BRIEFINGS_DIR = os.path.expanduser('~/.openclaw/workspace/briefings')


def _python():
    return sys.executable


@click.group()
def cli():
    """dailyinfo — daily briefing pipeline manager."""
    pass


@cli.command()
@click.option('--pipeline', '-p', type=click.Choice(['1', '2', '3', 'all']), default='all',
              help='Pipeline to run: 1=RSS papers/news, 2=code trending, 3=university news. Default: all')
def run(pipeline):
    """Scrape sources, generate AI summaries, save briefing files."""
    script = os.path.join(SCRIPTS_DIR, 'run_pipelines.py')
    cmd = [_python(), script]
    if pipeline != 'all':
        cmd += ['--pipeline', pipeline]
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def push():
    """Push today's briefings to Discord channels."""
    script = os.path.join(SCRIPTS_DIR, 'push_to_discord.py')
    result = subprocess.run([_python(), script], cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command()
def status():
    """Show today's briefing file counts per channel."""
    categories = ['papers', 'ai_news', 'code', 'resource']
    total = 0
    click.echo(f'Briefings for {DATE}:')
    for cat in categories:
        path = os.path.join(BRIEFINGS_DIR, cat)
        if os.path.exists(path):
            files = [f for f in sorted(os.listdir(path)) if DATE in f]
            total += len(files)
            click.echo(f'  {cat:15s}: {len(files):3d} files')
        else:
            click.echo(f'  {cat:15s}: directory missing')

    pushed_dir = os.path.expanduser('~/.openclaw/workspace/pushed')
    click.echo()
    click.echo('Already pushed today:')
    for cat in categories:
        path = os.path.join(pushed_dir, cat)
        if os.path.exists(path):
            files = [f for f in sorted(os.listdir(path)) if DATE in f]
            if files:
                click.echo(f'  {cat:15s}: {len(files):3d} files')
    click.echo(f'\nTotal pending: {total} files')


@cli.command()
def logs():
    """Tail the cron execution log."""
    log_file = os.path.join(PROJECT_ROOT, 'logs', 'dailyinfo.log')
    if not os.path.exists(log_file):
        click.echo(f'Log file not found: {log_file}')
        sys.exit(1)
    result = subprocess.run(['tail', '-n', '100', log_file])
    sys.exit(result.returncode)


@cli.command()
def bot():
    """Start the Discord bot (listens for @mentions, replies with deep analysis)."""
    script = os.path.join(SCRIPTS_DIR, 'discord_bot.py')
    result = subprocess.run([_python(), script], cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


if __name__ == '__main__':
    cli()
