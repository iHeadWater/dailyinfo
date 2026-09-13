"""Tests for weekly_code_trends.py — parsing, collection, ranking, end-to-end."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# Ensure scripts/ is on path so flat imports work
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def briefing_date(days_ago: int) -> str:
    """YYYY-MM-DD relative to today — keeps window-dependent tests time-independent."""
    return (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def sample_briefing():
    """Return content of a realistic github_trending briefing."""
    return read_fixture("github_trending_briefing_2026-07-01.md")


# ── Parsing ──────────────────────────────────────────────────────────────


class TestParseGithubBriefing:
    def test_extracts_repo_names(self, sample_briefing):
        """Should extract **owner/repo** bold markers from briefing markdown."""
        from weekly_code_trends import parse_github_briefing

        repos = parse_github_briefing("2026-07-01", sample_briefing)

        assert len(repos) == 5
        assert repos[0] == "simplex-chat/simplex-chat"
        assert repos[1] == "xbtlin/ai-berkshire"
        assert repos[4] == "google-labs-code/design.md"

    def test_all_items_have_correct_date(self, sample_briefing):
        from weekly_code_trends import parse_github_briefing

        repos = parse_github_briefing("2026-07-01", sample_briefing)
        for repo in repos:
            assert isinstance(repo, str)
            assert "/" in repo, f"Expected owner/repo format, got {repo!r}"

    def test_empty_content_returns_empty_list(self):
        from weekly_code_trends import parse_github_briefing

        repos = parse_github_briefing("2026-07-01", "")
        assert repos == []

    def test_no_bold_patterns_returns_empty_list(self):
        from weekly_code_trends import parse_github_briefing

        content = "# GitHub Trending\n\nJust some text without bold repo names.\n"
        repos = parse_github_briefing("2026-07-01", content)
        assert repos == []


# ── Collection ───────────────────────────────────────────────────────────


class TestCollectWeekCodeBriefings:
    def test_collects_and_dedup_by_date(self, tmp_path, monkeypatch):
        """Same date in both briefings/ and pushed/ — keep the first version."""
        from weekly_code_trends import collect_week_code_briefings

        data_root = tmp_path / "data"
        briefings_code = data_root / "briefings" / "code"
        pushed_code = data_root / "pushed" / "code"
        briefings_code.mkdir(parents=True)
        pushed_code.mkdir(parents=True)

        d = briefing_date(1)
        content_b = read_fixture("github_trending_briefing_2026-07-01.md")
        content_p = f"# GitHub Trending - {d}\n\n**different/repo** — other.\n"

        (briefings_code / f"github_trending_briefing_{d}.md").write_text(
            content_b, encoding="utf-8"
        )
        (pushed_code / f"github_trending_briefing_{d}.md").write_text(
            content_p, encoding="utf-8"
        )

        monkeypatch.setattr("weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings")
        monkeypatch.setattr("weekly_code_trends.PUSHED_DIR", data_root / "pushed")

        result = collect_week_code_briefings("code", days=7)
        assert len(result) == 1
        assert result[0][1] == content_b  # briefings/ version wins

    def test_skips_non_github_files(self, tmp_path, monkeypatch):
        """Only github_trending_briefing_*.md files should be collected."""
        from weekly_code_trends import collect_week_code_briefings

        data_root = tmp_path / "data"
        code_dir = data_root / "briefings" / "code"
        code_dir.mkdir(parents=True)
        (data_root / "pushed" / "code").mkdir(parents=True)

        d = briefing_date(1)
        (code_dir / f"github_trending_briefing_{d}.md").write_text(
            "# GitHub Trending\n\n**a/b** — desc.\n", encoding="utf-8"
        )
        (code_dir / f"huggingface_models_briefing_{d}.md").write_text(
            "# HF Models\n\nNot a github file.\n", encoding="utf-8"
        )

        monkeypatch.setattr("weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings")
        monkeypatch.setattr("weekly_code_trends.PUSHED_DIR", data_root / "pushed")

        result = collect_week_code_briefings("code", days=7)
        assert len(result) == 1
        assert result[0][0] == d  # only the github_trending file was collected


# ── Ranking ──────────────────────────────────────────────────────────────


class TestRankRepos:
    def test_top_5_returned_when_more_than_5(self):
        """Should return exactly 5 repos when input has more."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            f"owner{i}/repo{i}": {
                "full_name": f"owner{i}/repo{i}",
                "url": f"https://github.com/owner{i}/repo{i}",
                "day_count": i + 1,
            }
            for i in range(10)
        }

        top5 = rank_repos(repo_stats)
        assert len(top5) == 5

    def test_higher_day_count_ranks_higher(self):
        """Repo appearing on more days should rank higher."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            "a/repo_a": {
                "full_name": "a/repo_a",
                "url": "https://github.com/a/repo_a",
                "day_count": 5,
            },
            "b/repo_b": {
                "full_name": "b/repo_b",
                "url": "https://github.com/b/repo_b",
                "day_count": 1,
            },
        }

        top = rank_repos(repo_stats)
        assert top[0]["full_name"] == "a/repo_a"

    def test_ties_keep_first_seen_order(self):
        """Repos tied on day_count keep insertion order.

        That order is the order they were first seen in, i.e. the order they
        first appeared across the collected briefings. With star counts gone
        this is the sole tiebreaker, so the ranking must stay deterministic.
        """
        from weekly_code_trends import rank_repos

        repo_stats = {
            "first/repo": {
                "full_name": "first/repo",
                "url": "https://github.com/first/repo",
                "day_count": 3,
            },
            "second/repo": {
                "full_name": "second/repo",
                "url": "https://github.com/second/repo",
                "day_count": 3,
            },
            "third/repo": {
                "full_name": "third/repo",
                "url": "https://github.com/third/repo",
                "day_count": 3,
            },
        }

        top = rank_repos(repo_stats)
        assert [r["full_name"] for r in top] == [
            "first/repo",
            "second/repo",
            "third/repo",
        ]

    def test_day_count_beats_stale_extra_fields(self):
        """Ranking must ignore fields that used to come from the GitHub API.

        Guards the deterministic / no-network boundary: stale star counts on
        the input dicts must not influence the order.
        """
        from weekly_code_trends import rank_repos

        repo_stats = {
            "lowstars/repo": {
                "full_name": "lowstars/repo",
                "url": "https://github.com/lowstars/repo",
                "day_count": 4,
                "stars": 1,
            },
            "highstars/repo": {
                "full_name": "highstars/repo",
                "url": "https://github.com/highstars/repo",
                "day_count": 2,
                "stars": 999999,
            },
        }

        top = rank_repos(repo_stats)
        assert top[0]["full_name"] == "lowstars/repo"

    def test_fewer_than_5_returns_all(self):
        """When only 3 repos, return all 3."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            f"owner{i}/repo{i}": {
                "full_name": f"owner{i}/repo{i}",
                "url": f"https://github.com/owner{i}/repo{i}",
                "day_count": 1,
            }
            for i in range(3)
        }

        top = rank_repos(repo_stats)
        assert len(top) == 3

    def test_empty_input_returns_empty(self):
        from weekly_code_trends import rank_repos

        assert rank_repos({}) == []


# ── End-to-End ──────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_full_pipeline_writes_json_output(self, tmp_path, monkeypatch):
        """Complete pipeline: collect → parse → rank → write JSON."""
        from weekly_code_trends import run_weekly_code_trends

        data_root = tmp_path / "data"
        briefings_code = data_root / "briefings" / "code"
        code_weekly_dir = briefings_code.parent / "code_weekly"
        briefings_code.mkdir(parents=True)
        code_weekly_dir.mkdir(parents=True)

        # Write 3 days of github trending briefings with some repo overlap
        # (dates relative to today so tests stay within the lookback window)
        d1, d2, d3 = briefing_date(3), briefing_date(2), briefing_date(1)
        content_day1 = f"""# GitHub Trending - {d1}
**shared/repo** — A shared repo appearing multiple days.
**unique1/repo1** — Day 1 unique repo.
**unique2/repo2** — Day 1 also unique.
"""

        content_day2 = f"""# GitHub Trending - {d2}
**shared/repo** — Same shared repo, day 2.
**unique3/repo3** — Day 2 unique.
"""

        content_day3 = f"""# GitHub Trending - {d3}
**shared/repo** — Same shared repo, day 3.
**unique4/repo4** — Day 3 unique.
**unique5/repo5** — Another day 3 repo.
"""

        (briefings_code / f"github_trending_briefing_{d1}.md").write_text(
            content_day1, encoding="utf-8"
        )
        (briefings_code / f"github_trending_briefing_{d2}.md").write_text(
            content_day2, encoding="utf-8"
        )
        (briefings_code / f"github_trending_briefing_{d3}.md").write_text(
            content_day3, encoding="utf-8"
        )

        monkeypatch.setattr("weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings")
        monkeypatch.setattr("weekly_code_trends.PUSHED_DIR", data_root / "pushed")

        # Run
        code = run_weekly_code_trends(days=7, force=True)
        assert code == 0

        # Verify JSON output
        json_files = list(code_weekly_dir.glob("data_*.json"))
        assert len(json_files) == 1

        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert "top_repos" in data
        assert len(data["top_repos"]) <= 5
        # shared/repo should be in top 5 (appeared 3× across 3 days)
        top_names = [r["full_name"] for r in data["top_repos"]]
        assert "shared/repo" in top_names

        # Every field must be derivable from local files alone — no network data
        for repo in data["top_repos"]:
            assert set(repo) == {"full_name", "url", "day_count", "dates_seen"}

    def test_no_briefings_returns_error(self, tmp_path, monkeypatch):
        """When no briefings exist, return code 1."""
        from weekly_code_trends import run_weekly_code_trends

        data_root = tmp_path / "data"
        (data_root / "briefings" / "code").mkdir(parents=True)
        (data_root / "pushed" / "code").mkdir(parents=True)

        monkeypatch.setattr("weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings")
        monkeypatch.setattr("weekly_code_trends.PUSHED_DIR", data_root / "pushed")

        code = run_weekly_code_trends(days=7, force=True)
        assert code == 1


# ── CLI ──────────────────────────────────────────────────────────────────


class TestCLI:
    def test_main_help(self):
        """Verify argparse help works without error."""
        import subprocess

        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "weekly_code_trends.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--days" in result.stdout
        assert "--force" in result.stdout
