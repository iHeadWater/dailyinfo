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


@cli.command("weekly-report")
@click.option("--collection", default="water", show_default=True, help="Zotero collection name.")
@click.option("-d", "--date", "date_str", default=None, help="Date YYYY-MM-DD (default: today).")
@click.option(
    "--artifact",
    type=click.Choice(["none", "audio", "video", "both"]),
    default="audio",
    show_default=True,
    help="NotebookLM artifact to generate.",
)
@click.option("--open-missing-pdfs", is_flag=True, help="Open missing Zotero PDFs for cloud sync.")
@click.option("--title", default=None, help="Override WeChat article title.")
@click.option("--thumb", default=None, help="WeChat cover image media_id.")
@click.option("--dry-run", is_flag=True, help="Save HTML locally, do not push to WeChat.")
@click.option("--skip-zotero", is_flag=True, help="Skip zotero-brief step, reuse existing briefing.md.")
@click.option("--skip-polish", is_flag=True, help="Skip DeepSeek polish step, reuse existing polished MD.")
@click.option("--skip-figures", is_flag=True, help="Skip figure extraction/generation step.")
def weekly_report(collection, date_str, artifact, open_missing_pdfs, title, thumb, dry_run, skip_zotero, skip_polish, skip_figures):
    """One-shot: Zotero -> NotebookLM -> DeepSeek polish -> WeChat draft.

    Chains three steps automatically:
    1. zotero-brief  (Zotero + NotebookLM -> briefing.md)
    2. polish_wechat (DeepSeek + SKILL.md -> wechat_article_polished.md)
    3. push_wechat   (MD -> HTML -> WeChat draft box)
    """
    import datetime as _dt

    sys.path.insert(0, str(SCRIPTS_DIR))
    from zotero_notebooklm import make_paths, parse_date, resolve_zotero_collection

    target_date = parse_date(date_str)
    collection_info = resolve_zotero_collection(collection) if collection else None
    collection_name = collection_info["name"] if collection_info else None
    paths = make_paths(target_date, collection_name=collection_name)

    # ── Step 1: Zotero -> NotebookLM ────────────────────────────────────────
    if not skip_zotero:
        click.echo(f"\n[Step 1/3] Running zotero-brief for {target_date} / collection={collection} ...")
        rc = _run_zotero_brief(
            date_str=date_str,
            force=False,
            artifact=artifact,
            manual_only=False,
            limit=50,
            collection=collection,
            open_missing_pdfs=open_missing_pdfs,
            pdf_wait_seconds=20,
            notebooklm_home=None,
            notebook_title=None,
        )
        if rc != 0:
            click.echo("zotero-brief failed; aborting.", err=True)
            sys.exit(rc)
    else:
        click.echo("[Step 1/3] Skipped (--skip-zotero).")

    briefing_path = paths.briefing
    if not briefing_path.exists():
        click.echo(f"briefing.md not found: {briefing_path}", err=True)
        sys.exit(1)

    # ── Step 2: DeepSeek polish ──────────────────────────────────────────────
    polished_path = paths.output_dir / "wechat_article_polished.md"
    if not skip_polish:
        click.echo(f"\n[Step 2/3] Polishing {briefing_path.name} with DeepSeek ...")
        from wechat.polish import load_skill_prompt, polish

        skill_prompt = load_skill_prompt()
        polished_text = polish(briefing_path.read_text(encoding="utf-8"), skill_prompt)
        polished_path.write_text(polished_text, encoding="utf-8")
        click.echo(f"  Saved: {polished_path}")
    else:
        click.echo("[Step 2/3] Skipped (--skip-polish).")
        if not polished_path.exists():
            click.echo(f"Polished MD not found: {polished_path}", err=True)
            sys.exit(1)

    # ── Step 2.5: Extract / generate figures ────────────────────────────────
    figures_json = paths.output_dir / "figures" / "figures.json"
    if not skip_figures:
        click.echo(f"\n[Step 2.5/3] Extracting figures ...")
        fig_cmd = [_python(), str(SCRIPTS_DIR / "wechat" / "figure.py"), str(polished_path),
                   "--output-dir", str(paths.output_dir)]
        result = subprocess.run(fig_cmd, cwd=PROJECT_ROOT)
        if result.returncode != 0:
            click.echo("Warning: figure extraction failed, continuing without figures.", err=True)
    else:
        click.echo("[Step 2.5/3] Skipped (--skip-figures).")

    # ── Step 3: Push to WeChat ───────────────────────────────────────────────
    click.echo(f"\n[Step 3/3] Pushing to WeChat draft box ...")
    push_cmd = [_python(), str(SCRIPTS_DIR / "wechat" / "push.py"), str(polished_path)]
    if title:
        push_cmd += ["--title", title]
    if thumb:
        push_cmd += ["--thumb", thumb]
    if dry_run:
        push_cmd.append("--dry-run")
    if not skip_figures and figures_json.exists():
        push_cmd += ["--figures", str(figures_json)]
    result = subprocess.run(push_cmd, cwd=PROJECT_ROOT)
    sys.exit(result.returncode)


@cli.command("download-pdf")
@click.argument("input_ref")
@click.option(
    "-o",
    "--output",
    default=None,
    help="Output PDF path. Default: ~/.myagentdata/dailyinfo/papers/<slug>.pdf",
)
@click.option(
    "--publisher",
    "publisher_filter",
    default=None,
    type=click.Choice(["elsevier", "springer", "wiley", "taylor-francis", "agu"]),
    help="Force a specific publisher workflow (skips auto-detection).",
)
def download_pdf(input_ref, output, publisher_filter):
    """Resolve a DOI/PII/URL and download the PDF via institutional access.

    INPUT_REF: a DOI (10.xxx/...), PII (S00221694...), or article URL.

    The actual browser download is performed by the ``download-pdf``
    Claude Code skill, which uses Playwright MCP tools. This CLI command
    resolves the input, detects the publisher, and prints the download
    instructions. When run inside Claude Code, invoke ``/download-pdf``
    instead.

    \b
    Examples:
        dailyinfo download-pdf 10.1016/j.jhydrol.2024.132471
        dailyinfo download-pdf S0022169424018675
        dailyinfo download-pdf "https://www.sciencedirect.com/..."
    """
    from download_pdf import Publisher, classify_input, detect_publisher, output_path_for

    result = classify_input(input_ref)
    pub_enum = detect_publisher(result["url"]) if result["url"] else Publisher.UNKNOWN
    publisher_name = pub_enum.name.lower() if pub_enum != Publisher.UNKNOWN else None

    # Compute default output path
    default_path = output if output else str(output_path_for(input_ref))

    if result["type"] == "unknown":
        click.echo(f"Error: Cannot classify input: {input_ref!r}", err=True)
        click.echo("Expected: DOI (10.xxx/...), PII, or article URL.")
        sys.exit(2)

    click.echo(f"Input Type:  {result['type']}")
    click.echo(f"Normalized:  {result['normalized']}")
    click.echo(f"Article URL: {result['url']}")
    if publisher_filter:
        click.echo(f"Publisher:   {publisher_filter} (forced)")
    elif publisher_name:
        click.echo(f"Publisher:   {publisher_name}")
    elif result["type"] == "doi":
        click.echo("Publisher:   (resolves after DOI redirect)")

    click.echo(f"Output:      {default_path}")
    click.echo("")
    click.echo("To download this PDF, use the Claude Code skill:")
    click.echo(f"  /download-pdf {input_ref}")
    if output:
        click.echo(f"  (custom output: {output})")


if __name__ == "__main__":
    cli()
