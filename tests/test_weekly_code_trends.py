"""Tests for weekly_code_trends.py — parsing, ranking, GitHub API, end-to-end."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

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


@pytest.fixture
def fake_github_api_response():
    """Return a minimal GitHub API response dict matching GET /repos/{owner}/{repo}."""
    return {
        "full_name": "simplex-chat/simplex-chat",
        "description": "Private messaging network without user identifiers",
        "stargazers_count": 12345,
        "language": "Haskell",
        "html_url": "https://github.com/simplex-chat/simplex-chat",
        "updated_at": "2026-07-01T12:00:00Z",
        "topics": ["privacy", "chat", "p2p"],
    }


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

        monkeypatch.setattr(
            "weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings"
        )
        monkeypatch.setattr(
            "weekly_code_trends.PUSHED_DIR", data_root / "pushed"
        )

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

        monkeypatch.setattr(
            "weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings"
        )
        monkeypatch.setattr(
            "weekly_code_trends.PUSHED_DIR", data_root / "pushed"
        )

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
                "description": f"Test repo {i}",
                "stars": 1000 + i * 100,
                "language": "Python",
                "url": f"https://github.com/owner{i}/repo{i}",
                "day_count": i + 1,
            }
            for i in range(10)
        }

        top5 = rank_repos(repo_stats)
        assert len(top5) == 5

    def test_higher_day_count_ranks_higher(self):
        """Repo appearing on more days should rank higher (same stars)."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            "a/repo_a": {
                "full_name": "a/repo_a",
                "description": "Frequent",
                "stars": 1000,
                "language": "Python",
                "url": "https://github.com/a/repo_a",
                "day_count": 5,
            },
            "b/repo_b": {
                "full_name": "b/repo_b",
                "description": "Rare",
                "stars": 1000,
                "language": "Python",
                "url": "https://github.com/b/repo_b",
                "day_count": 1,
            },
        }

        top = rank_repos(repo_stats)
        assert top[0]["full_name"] == "a/repo_a"

    def test_higher_stars_tiebreaker(self):
        """Same day_count — higher stars ranks higher."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            "a/repo_a": {
                "full_name": "a/repo_a",
                "description": "Less stars",
                "stars": 500,
                "language": "Python",
                "url": "https://github.com/a/repo_a",
                "day_count": 3,
            },
            "b/repo_b": {
                "full_name": "b/repo_b",
                "description": "More stars",
                "stars": 50000,
                "language": "Python",
                "url": "https://github.com/b/repo_b",
                "day_count": 3,
            },
        }

        top = rank_repos(repo_stats)
        assert top[0]["full_name"] == "b/repo_b"

    def test_fewer_than_5_returns_all(self):
        """When only 3 repos, return all 3."""
        from weekly_code_trends import rank_repos

        repo_stats = {
            f"owner{i}/repo{i}": {
                "full_name": f"owner{i}/repo{i}",
                "description": f"Test repo {i}",
                "stars": 1000 + i,
                "language": "Python",
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


# ── GitHub API ───────────────────────────────────────────────────────────


class TestGitHubAPI:
    def test_query_single_repo(self, fake_github_api_response):
        """Verify GitHub API call format and response parsing."""
        from weekly_code_trends import query_github_api

        with mock.patch("weekly_code_trends.requests.get") as mock_get:
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = fake_github_api_response
            mock_get.return_value = mock_resp

            result = query_github_api(["simplex-chat/simplex-chat"])

            assert len(result) == 1
            assert result["simplex-chat/simplex-chat"]["stars"] == 12345
            assert result["simplex-chat/simplex-chat"]["language"] == "Haskell"
            assert (
                result["simplex-chat/simplex-chat"]["description"]
                == "Private messaging network without user identifiers"
            )

    def test_api_error_returns_empty_dict(self):
        """On HTTP error, return empty dict for the failed repo."""
        from weekly_code_trends import query_github_api

        with mock.patch("weekly_code_trends.requests.get") as mock_get:
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 404
            mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
            mock_get.return_value = mock_resp

            result = query_github_api(["nonexistent/repo"])

            assert result == {}

    def test_batch_query(self):
        """Multiple repos queried in sequence."""
        from weekly_code_trends import query_github_api

        with mock.patch("weekly_code_trends.requests.get") as mock_get:

            def make_resp(full_name, stars):
                r = mock.MagicMock()
                r.status_code = 200
                r.json.return_value = {
                    "full_name": full_name,
                    "stargazers_count": stars,
                    "language": "Python",
                    "description": "desc",
                    "html_url": f"https://github.com/{full_name}",
                    "updated_at": "2026-07-01T12:00:00Z",
                }
                return r

            mock_get.side_effect = [
                make_resp("a/repo1", 1000),
                make_resp("b/repo2", 2000),
                make_resp("c/repo3", 3000),
            ]

            result = query_github_api(["a/repo1", "b/repo2", "c/repo3"])

            assert len(result) == 3
            assert result["a/repo1"]["stars"] == 1000
            assert result["c/repo3"]["stars"] == 3000

    def test_rate_limit_handling(self):
        """On 403 rate limit, raise a meaningful error."""
        from weekly_code_trends import query_github_api

        with mock.patch("weekly_code_trends.requests.get") as mock_get:
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 403
            mock_resp.json.return_value = {"message": "API rate limit exceeded"}
            mock_resp.raise_for_status.side_effect = Exception("403 rate limited")
            mock_get.return_value = mock_resp

            result = query_github_api(["a/repo1"])

            # Should not crash — returns empty dict for failed repo
            assert result == {}


# ── End-to-End ──────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_full_pipeline_writes_json_output(self, tmp_path, monkeypatch):
        """Complete pipeline: collect → parse → query API → rank → write JSON."""
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

        monkeypatch.setattr(
            "weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings"
        )
        monkeypatch.setattr(
            "weekly_code_trends.PUSHED_DIR", data_root / "pushed"
        )

        # Stub GitHub API
        def _fake_query_github(repo_names):
            result = {}
            for rn in repo_names[:10]:
                result[rn] = {
                    "full_name": rn,
                    "description": f"Description for {rn}",
                    "stars": 5000,
                    "language": "Python",
                    "url": f"https://github.com/{rn}",
                    "updated_at": "2026-07-01T12:00:00Z",
                }
            return result

        monkeypatch.setattr(
            "weekly_code_trends.query_github_api", _fake_query_github
        )

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

    def test_no_briefings_returns_error(self, tmp_path, monkeypatch):
        """When no briefings exist, return code 1."""
        from weekly_code_trends import run_weekly_code_trends

        data_root = tmp_path / "data"
        (data_root / "briefings" / "code").mkdir(parents=True)
        (data_root / "pushed" / "code").mkdir(parents=True)

        monkeypatch.setattr(
            "weekly_code_trends.BRIEFINGS_DIR", data_root / "briefings"
        )
        monkeypatch.setattr(
            "weekly_code_trends.PUSHED_DIR", data_root / "pushed"
        )

        code = run_weekly_code_trends(days=7, force=True)
        assert code == 1


# ── CLI ──────────────────────────────────────────────────────────────────


class TestCLI:
    def test_main_help(self):
        """Verify argparse help works without error."""
        import subprocess
        import os

        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "weekly_code_trends.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "--days" in result.stdout
        assert "--force" in result.stdout
