"""Weekly GitHub Trending curator.

Collects the past 7 days of github_trending briefings, parses the repo names
they mention, and ranks repos by how many days they appeared on. Outputs a
JSON data file for downstream consumers.

Deterministic by design: reads local briefing files only — no network calls,
no credentials, no LLM. Deeper per-repo analysis belongs in a separate tool.

Usage:
    python3 scripts/weekly_code_trends.py
    python3 scripts/weekly_code_trends.py --force   # overwrite existing
    python3 scripts/weekly_code_trends.py --days 14  # extend lookback
"""

import argparse
import datetime
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from paths import BRIEFINGS_DIR, PUSHED_DIR

DATE = datetime.datetime.now().strftime("%Y-%m-%d")
TOP_N = 5

# Pattern: **owner/repo** — Bold markdown with forward slash
_REPO_BOLD_PATTERN = re.compile(r"\*\*([\w.-]+/[\w.-]+)\*\*")


# ── Logging ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


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


# ── Ranking ──────────────────────────────────────────────────────────────────


def rank_repos(repo_stats: dict[str, dict]) -> list[dict]:
    """Rank repos by appearance frequency, returning the top ``TOP_N``.

    Sort key: ``day_count`` descending only — a repo appearing on 5 days is
    more interesting than a one-hit wonder.

    Python's sort is stable and ``reverse=True`` preserves that stability, so
    repos tied on ``day_count`` keep their original order — which is the order
    they were first seen in, i.e. the order they first appeared across the
    collected briefings.
    """
    if not repo_stats:
        return []

    sorted_repos = sorted(
        repo_stats.values(),
        key=lambda r: r.get("day_count", 0),
        reverse=True,
    )
    return sorted_repos[:TOP_N]


# ── Orchestrator ─────────────────────────────────────────────────────────────


def run_weekly_code_trends(days: int = 7, force: bool = False) -> int:
    """Generate a JSON data file of the top GitHub trending repos for the week.

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

    # 3. Build repo stats from local data only
    repo_stats: dict[str, dict] = {}
    for repo_name in unique_repos:
        dates_seen = repo_dates[repo_name]
        repo_stats[repo_name] = {
            "full_name": repo_name,
            "url": f"https://github.com/{repo_name}",
            "day_count": len(dates_seen),
            "dates_seen": sorted(dates_seen),
        }

    # 4. Rank
    top_repos = rank_repos(repo_stats)
    log(
        f"  ranked top {len(top_repos)}: "
        + ", ".join(r["full_name"] for r in top_repos)
    )

    # 5. Save JSON
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
