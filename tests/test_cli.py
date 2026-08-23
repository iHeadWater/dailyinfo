"""Tests for the Click-based ``dailyinfo`` CLI."""

from __future__ import annotations

import tomllib
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class _FakeCompleted:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def cli_mod(monkeypatch):
    """Import ``cli`` and stub out ``subprocess.run`` to avoid real commands."""
    import cli as cli_module

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    cli_module.__test_calls__ = calls  # type: ignore[attr-defined]
    return cli_module


def _write_valid_env(path):
    path.write_text(
        "DEEPSEEK_API_KEY=sk-real-test\n"
        "DISCORD_BOT_TOKEN=test-bot-token\n"
        "DISCORD_CHANNEL_PAPERS=1\n"
        "DISCORD_CHANNEL_AI_NEWS=2\n"
        "DISCORD_CHANNEL_CODE=3\n"
        "DISCORD_CHANNEL_RESOURCE=4\n"
        "DISCORD_CHANNEL_CONFERENCE=6\n"
        "DISCORD_CHANNEL_PAPERS_DEV=101\n"
        "DISCORD_CHANNEL_AI_NEWS_DEV=102\n"
        "DISCORD_CHANNEL_CODE_DEV=103\n"
        "DISCORD_CHANNEL_RESOURCE_DEV=104\n"
        "DISCORD_CHANNEL_CONFERENCE_DEV=106\n"
        "DISCORD_CHANNEL_ARXIV=5\n"
        "DISCORD_CHANNEL_ARXIV_DEV=105\n",
        encoding="utf-8",
    )


def test_install_fails_when_env_missing(cli_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "ENV_FILE", tmp_path / ".env")

    result = CliRunner().invoke(cli_mod.cli, ["install"])
    assert result.exit_code == 1
    assert "ERROR: .env not found" in result.output


def test_install_fails_on_placeholder_key(cli_mod, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=your_api_key_here\nDISCORD_BOT_TOKEN=real\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "ENV_FILE", env)

    result = CliRunner().invoke(cli_mod.cli, ["install"])
    assert result.exit_code == 1
    assert "DEEPSEEK_API_KEY" in result.output


def test_install_succeeds_with_full_env(cli_mod, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _write_valid_env(env)
    monkeypatch.setattr(cli_mod, "ENV_FILE", env)

    result = CliRunner().invoke(cli_mod.cli, ["install"])
    assert result.exit_code == 0, result.output
    assert "Setup complete" in result.output
    assert "WARN" not in result.output


def test_install_warns_about_unset_channels(cli_mod, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=sk-real-test\nDISCORD_BOT_TOKEN=tok\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_mod, "ENV_FILE", env)

    result = CliRunner().invoke(cli_mod.cli, ["install"])
    assert result.exit_code == 0, result.output
    assert "WARN: no channel id" in result.output
    assert "DISCORD_CHANNEL_PAPERS" in result.output


def test_install_creates_workspace_dirs(cli_mod, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    _write_valid_env(env)
    monkeypatch.setattr(cli_mod, "ENV_FILE", env)

    result = CliRunner().invoke(cli_mod.cli, ["install"])
    assert result.exit_code == 0

    from paths import BRIEFINGS_DIR, FRESHRSS_DATA, PUSHED_DIR

    assert FRESHRSS_DATA.exists()
    for cat in ("papers", "ai_news", "code", "resource", "arxiv", "conference"):
        assert (BRIEFINGS_DIR / cat).is_dir()
        assert (PUSHED_DIR / cat).is_dir()


def test_status_reports_file_counts(cli_mod):
    from paths import BRIEFINGS_DIR, PUSHED_DIR

    today = datetime.now().strftime("%Y-%m-%d")

    (BRIEFINGS_DIR / "papers").mkdir(parents=True, exist_ok=True)
    (BRIEFINGS_DIR / "papers" / f"one_{today}.md").write_text("x", encoding="utf-8")
    (BRIEFINGS_DIR / "papers" / f"two_{today}.md").write_text("x", encoding="utf-8")
    (BRIEFINGS_DIR / "ai_news").mkdir(parents=True, exist_ok=True)

    (PUSHED_DIR / "ai_news").mkdir(parents=True, exist_ok=True)
    (PUSHED_DIR / "ai_news" / f"done_{today}.md").write_text("x", encoding="utf-8")

    result = CliRunner().invoke(cli_mod.cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "papers" in result.output
    assert "2 files" in result.output
    assert "ai_news" in result.output
    assert "Total pending: 2" in result.output


def test_status_reports_openreview_checkpoint_progress(cli_mod):
    from paths import STATE_DIR
    from conference import ConferenceState

    ConferenceState(STATE_DIR / "openreview.sqlite3").start_run(
        "openreview_iclr_2026",
        "ICLR.cc/2026/Conference",
        "full",
        "config-v1",
        "ICLR.cc/2026/Conference/-/Submission",
        None,
    )

    result = CliRunner().invoke(cli_mod.cli, ["status"])

    assert result.exit_code == 0, result.output
    assert "run RUNNING" in result.output
    assert "phase=DISCOVERY" in result.output


def test_run_forwards_pipeline_arg(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run", "-p", "2"])
    assert result.exit_code == 0, result.output

    calls = cli_mod.__test_calls__
    assert calls, "expected at least one subprocess call"
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    assert pipeline_calls[0][-2:] == ["--pipeline", "2"]


def test_run_forwards_conference_source_filter(cli_mod):
    result = CliRunner().invoke(
        cli_mod.cli,
        ["run", "-p", "6", "--source", "openreview_iclr_2026"],
    )
    assert result.exit_code == 0, result.output
    pipeline_calls = [
        c
        for c in cli_mod.__test_calls__
        if any("run_pipelines.py" in part for part in c)
    ]
    assert pipeline_calls[-1][-4:] == [
        "--pipeline",
        "6",
        "--source",
        "openreview_iclr_2026",
    ]


def test_run_without_pipeline_runs_all(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    assert calls
    # When --pipeline is not passed, CLI omits the flag so the child runs all.
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    assert all("--pipeline" not in cmd for cmd in pipeline_calls)


def test_run_forwards_force_all(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run", "--force", "all"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    assert pipeline_calls[-1][-2:] == ["--force", "all"]


def test_run_forwards_multiple_force_sources(cli_mod):
    result = CliRunner().invoke(
        cli_mod.cli, ["run", "-f", "arxiv_cs_ai", "-f", "nature"]
    )
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    last = pipeline_calls[-1]
    assert last.count("--force") == 2
    assert "arxiv_cs_ai" in last
    assert "nature" in last


def test_run_combines_pipeline_and_force(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run", "-p", "1", "-f", "arxiv_cs_ai"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    last = pipeline_calls[-1]
    assert "--pipeline" in last and "1" in last
    assert last[-2:] == ["--force", "arxiv_cs_ai"]


def test_push_invokes_push_script(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["push"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    push_calls = [c for c in calls if any("push_to_discord.py" in part for part in c)]
    assert push_calls
    assert all("--date" not in cmd for cmd in push_calls)


def test_push_forwards_date_argument(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["push", "--date", "2026-04-22"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    push_calls = [c for c in calls if any("push_to_discord.py" in part for part in c)]
    assert push_calls
    assert push_calls[-1][-2:] == ["--date", "2026-04-22"]


def test_push_rejects_invalid_date(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["push", "--date", "yesterday"])
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in result.output


def test_start_fails_when_compose_missing(cli_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "PROJECT_ROOT", tmp_path)

    result = CliRunner().invoke(cli_mod.cli, ["start"])
    assert result.exit_code == 1
    assert "docker-compose.yml not found" in result.output


def test_version_flag_prints_project_version(cli_mod):
    project_version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    result = CliRunner().invoke(cli_mod.cli, ["--version"])
    assert result.exit_code == 0
    assert project_version in result.output


def test_run_forwards_pipeline_flag(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run", "-p", "3"])
    assert result.exit_code == 0, result.output

    calls = cli_mod.__test_calls__
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    assert "--pipeline" in pipeline_calls[0]
    assert pipeline_calls[0][pipeline_calls[0].index("--pipeline") + 1] == "3"


def test_run_default_runs_all_pipelines(cli_mod):
    result = CliRunner().invoke(cli_mod.cli, ["run"])
    assert result.exit_code == 0

    calls = cli_mod.__test_calls__
    pipeline_calls = [c for c in calls if any("run_pipelines.py" in part for part in c)]
    assert pipeline_calls
    assert "--pipeline" not in pipeline_calls[0]


class TestCleanCacheCommand:
    """Tests for the ``dailyinfo clean-cache`` CLI command."""

    def test_clean_cache_defaults(self, cli_mod, monkeypatch):
        """Default invocation uses 24h threshold and deletes for real."""
        cache_dir = cli_mod.FRESHRSS_DATA / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        results = {}

        def fake_clean(cache_dir, max_age_hours=24, dry_run=False):
            results.update(
                cache_dir=str(cache_dir),
                max_age_hours=max_age_hours,
                dry_run=dry_run,
            )
            return (3, 0)

        monkeypatch.setattr(cli_mod, "clean_stale_cache", fake_clean)

        result = CliRunner().invoke(cli_mod.cli, ["clean-cache"])
        assert result.exit_code == 0, result.output
        assert results["max_age_hours"] == 24
        assert results["dry_run"] is False
        assert "Cleaned 3 stale cache file(s)" in result.output

    def test_clean_cache_dry_run(self, cli_mod, monkeypatch):
        """--dry-run reports what would be deleted without deleting."""
        cache_dir = cli_mod.FRESHRSS_DATA / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        results = {}

        def fake_clean(cache_dir, max_age_hours=24, dry_run=False):
            results.update(dry_run=dry_run)
            return (5, 0)

        monkeypatch.setattr(cli_mod, "clean_stale_cache", fake_clean)

        result = CliRunner().invoke(cli_mod.cli, ["clean-cache", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert results["dry_run"] is True
        assert "Would delete 5 stale cache file(s)" in result.output

    def test_clean_cache_custom_max_age(self, cli_mod, monkeypatch):
        """--max-age overrides the default threshold."""
        cache_dir = cli_mod.FRESHRSS_DATA / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        results = {}

        def fake_clean(cache_dir, max_age_hours=24, dry_run=False):
            results.update(max_age_hours=max_age_hours)
            return (0, 0)

        monkeypatch.setattr(cli_mod, "clean_stale_cache", fake_clean)

        result = CliRunner().invoke(cli_mod.cli, ["clean-cache", "--max-age", "12"])
        assert result.exit_code == 0, result.output
        assert results["max_age_hours"] == 12
        assert "No stale cache files found" in result.output

    def test_clean_cache_handles_errors(self, cli_mod, monkeypatch):
        """Errors during deletion are reported."""
        cache_dir = cli_mod.FRESHRSS_DATA / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        def fake_clean(cache_dir, max_age_hours=24, dry_run=False):
            return (2, 1)

        monkeypatch.setattr(cli_mod, "clean_stale_cache", fake_clean)

        result = CliRunner().invoke(cli_mod.cli, ["clean-cache"])
        assert result.exit_code == 1, result.output
        assert "Cleaned 2 stale cache file(s)" in result.output
        assert "1 error(s)" in result.output


def test_cache_clear_defaults_to_arxiv_source(cli_mod):
    from paths import FRESHRSS_DATA

    cache_dir = FRESHRSS_DATA / "cache"
    cache_dir.mkdir(parents=True)
    arxiv_spc = cache_dir / "arxiv.spc"
    arxiv_html = cache_dir / "arxiv.html"
    nature_spc = cache_dir / "nature.spc"
    arxiv_spc.write_text("https://rss.arxiv.org/rss/cs.AI", encoding="utf-8")
    arxiv_html.write_text("cached html", encoding="utf-8")
    nature_spc.write_text("https://www.nature.com/nature.rss", encoding="utf-8")

    result = CliRunner().invoke(cli_mod.cli, ["cache-clear"])

    assert result.exit_code == 0, result.output
    assert "Deleted 2 cache file(s)." in result.output
    assert not arxiv_spc.exists()
    assert not arxiv_html.exists()
    assert nature_spc.exists()


def test_cache_clear_dry_run_keeps_files(cli_mod):
    from paths import FRESHRSS_DATA

    cache_dir = FRESHRSS_DATA / "cache"
    cache_dir.mkdir(parents=True)
    arxiv_spc = cache_dir / "arxiv.spc"
    arxiv_spc.write_text("https://rss.arxiv.org/rss/cs.AI", encoding="utf-8")

    result = CliRunner().invoke(cli_mod.cli, ["cache-clear", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert arxiv_spc.exists()


def test_cache_clear_refresh_runs_actualize_script(cli_mod, monkeypatch):
    from paths import FRESHRSS_DATA
    import freshrss_cache

    cache_dir = FRESHRSS_DATA / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "arxiv.spc").write_text("https://rss.arxiv.org/rss/cs.AI", encoding="utf-8")

    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(freshrss_cache.subprocess, "run", fake_run)

    result = CliRunner().invoke(cli_mod.cli, ["cache-clear", "--refresh"])

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "exec",
            "dailyinfo_freshrss",
            "php",
            "/var/www/FreshRSS/app/actualize_script.php",
        ]
    ]
