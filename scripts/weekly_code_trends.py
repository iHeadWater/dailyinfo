"""Weekly GitHub Trending curator.

Collects the past 7 days of github_trending briefings, parses repo names,
queries GitHub API for accurate star counts, ranks by frequency + stars,
and outputs a JSON data file for the weekly-code-trends skill to consume.

Usage:
    python3 scripts/weekly_code_trends.py
    python3 scripts/weekly_code_trends.py --force   # overwrite existing
    python3 scripts/weekly_code_trends.py --days 14  # extend lookback
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from paths import BRIEFINGS_DIR, PUSHED_DIR

GITHUB_API_URL = "https://api.github.com/repos"
_BACKOFF_SECONDS = [2, 5, 10]

DATE = datetime.datetime.now().strftime("%Y-%m-%d")

# Pattern: **owner/repo** — Bold markdown with forward slash
_REPO_BOLD_PATTERN = re.compile(r"\*\*([\w.-]+/[\w.-]+)\*\*")


# ── Logging ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── GitHub token loading ─────────────────────────────────────────────────────


def _load_github_token() -> str:
    """Load GITHUB_TOKEN from env var or .env file."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return token
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ── Collection ───────────────────────────────────────────────────────────────


def collect_week_code_briefings(
    category: str = "code", days: int = 7
) -> list[tuple[str, str]]:
    """Return (date, content) tuples for github_trending briefings in the lookback window.

    Only collects files matching ``github_trending_briefing_*.md``.
    """
    cutoff = datetime.datetime.combine(
        datetime.date.today() - datetime.timedelta(days=days),
        datetime.time.min,
    )
    collected: list[tuple[str, str]] = []

    for base_dir in (BRIEFINGS_DIR, PUSHED_DIR):
        cat_dir = base_dir / category
        if not cat_dir.exists():
            continue
        for fpath in cat_dir.glob("github_trending_briefing_*.md"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fpath.name)
            if not m:
                continue
            try:
                file_date = datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if file_date < cutoff:
                continue
            text = fpath.read_text(encoding="utf-8")
            collected.append((m.group(1), text))

    # Deduplicate: same date can appear in both briefings/ and pushed/.
    # Keep the first version found (briefings/ is scanned first).
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for date, content in collected:
        if date not in seen:
            seen.add(date)
            deduped.append((date, content))

    deduped.sort(key=lambda x: x[0])
    return deduped


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_github_briefing(date: str, content: str) -> list[str]:
    """Extract ``owner/repo`` names from a github_trending briefing markdown file.

    Parses ``**owner/repo**`` bold markdown patterns. Returns a list of
    ``owner/repo`` strings.
    """
    repos: list[str] = []
    for match in _REPO_BOLD_PATTERN.finditer(content):
        repos.append(match.group(1))
    return repos


# ── GitHub API ───────────────────────────────────────────────────────────────


def query_github_api(repo_full_names: list[str]) -> dict[str, dict]:
    """Query GitHub API for accurate repo metadata.

    Calls ``GET /repos/{owner}/{repo}`` for each repo. Uses GITHUB_TOKEN
    for higher rate limits if available.

    Args:
        repo_full_names: List of ``owner/repo`` strings.

    Returns:
        Dict keyed by full_name with fields: full_name, description, stars,
        language, url, updated_at. Failed repos are omitted.
    """
    token = _load_github_token()
    headers = {
        "User-Agent": "dailyinfo/1.0",
        "Accept": "application/vnd.github.v3+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[str, dict] = {}
    for full_name in repo_full_names:
        url = f"{GITHUB_API_URL}/{full_name}"
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                result[full_name] = {
                    "full_name": data.get("full_name", full_name),
                    "description": (data.get("description") or ""),
                    "stars": data.get("stargazers_count", 0),
                    "language": (data.get("language") or ""),
                    "url": data.get("html_url", f"https://github.com/{full_name}"),
                    "updated_at": data.get("updated_at", ""),
                }
                break
            except Exception as exc:
                log(f"  [github_api] {full_name} attempt {attempt + 1}/3: {exc}")
                if attempt < 2:
                    time.sleep(_BACKOFF_SECONDS[attempt])

    return result


# ── Ranking ──────────────────────────────────────────────────────────────────


def rank_repos(repo_stats: dict[str, dict]) -> list[dict]:
    """Rank repos by frequency + star count, returning top 5.

    Sort key: (day_count DESC, stars DESC). Day count is the primary signal
    (a repo appearing on 5 days is more interesting than a one-hit wonder).
    Stars act as tiebreaker.
    """
    if not repo_stats:
        return []

    sorted_repos = sorted(
        repo_stats.values(),
        key=lambda r: (r.get("day_count", 0), r.get("stars", 0)),
        reverse=True,
    )
    return sorted_repos[:5]


# ── Orchestrator ─────────────────────────────────────────────────────────────


def run_weekly_code_trends(days: int = 7, force: bool = False) -> int:
    """Generate a JSON data file of top-5 GitHub trending repos for the week.

    Args:
        days: Lookback window in days.
        force: Overwrite existing data file for today.

    Returns:
        0 on success, 1 on failure.
    """
    out_dir = BRIEFINGS_DIR / "code_weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"data_{DATE}.json"

    if out_path.exists() and not force:
        log(f"  code_weekly data already exists for {DATE}, skip (use --force)")
        return 0

    # 1. Collect
    log(f"  collecting last {days} days of github_trending briefings...")
    dated_briefings = collect_week_code_briefings("code", days)
    if not dated_briefings:
        log("  no github_trending briefings found in the past week, abort")
        return 1
    log(
        f"  found {len(dated_briefings)} briefings: "
        f"{dated_briefings[0][0]} ~ {dated_briefings[-1][0]}"
    )

    # 2. Parse — build per-repo frequency stats
    repo_dates: dict[str, list[str]] = defaultdict(list)
    for date, content in dated_briefings:
        repos = parse_github_briefing(date, content)
        for repo in repos:
            if date not in repo_dates[repo]:
                repo_dates[repo].append(date)

    unique_repos = list(repo_dates.keys())
    log(f"  parsed {len(unique_repos)} unique repos across {len(dated_briefings)} days")

    if not unique_repos:
        log("  no repos parsed, abort")
        return 1

    # 3. Query GitHub API
    log(f"  querying GitHub API for {len(unique_repos)} repos...")
    api_results = query_github_api(unique_repos)
    log(f"  got API results for {len(api_results)} repos")

    # 4. Build repo_stats with combined data
    repo_stats: dict[str, dict] = {}
    for repo_name in unique_repos:
        api_data = api_results.get(repo_name, {})
        dates_seen = repo_dates[repo_name]
        repo_stats[repo_name] = {
            "full_name": repo_name,
            "description": api_data.get("description", ""),
            "stars": api_data.get("stars", 0),
            "language": api_data.get("language", ""),
            "url": api_data.get("url", f"https://github.com/{repo_name}"),
            "updated_at": api_data.get("updated_at", ""),
            "day_count": len(dates_seen),
            "dates_seen": sorted(dates_seen),
        }

    # 5. Rank
    top_repos = rank_repos(repo_stats)
    log(
        f"  ranked top {len(top_repos)}: "
        + ", ".join(r["full_name"] for r in top_repos)
    )

    # 6. Save JSON
    output_data = {
        "date": DATE,
        "top_repos": top_repos,
        "all_repos_count": len(unique_repos),
        "days_scanned": days,
        "briefings_found": len(dated_briefings),
    }
    out_path.write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"  saved -> {out_path}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate weekly GitHub Trending top-5 JSON data file"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default: 7)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing data file for today",
    )
    args = parser.parse_args()

    log("=== GitHub Trending Weekly ===")
    code = run_weekly_code_trends(days=args.days, force=args.force)
    sys.exit(code)


if __name__ == "__main__":
    main()
