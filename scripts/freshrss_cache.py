"""FreshRSS SimplePie cache utilities.

Provides helpers to detect and clear stale cache files that can cause feeds
to stop updating silently (see issue #57).
"""

import datetime
import json
import subprocess
import time
from pathlib import Path
from typing import Iterable, List
from urllib.parse import urlparse


def _cache_files(cache_dir: Path) -> Iterable[Path]:
    """Yield FreshRSS SimplePie cache files recursively."""
    if not cache_dir.exists():
        return []
    return cache_dir.rglob("*")


def _candidate_tokens(url: str) -> list[str]:
    """Return stable URL fragments likely to appear in SimplePie cache files."""
    parsed = urlparse(url)
    tokens = [url]
    if parsed.netloc:
        tokens.append(parsed.netloc)
    if parsed.netloc and parsed.path:
        tokens.append(f"{parsed.netloc}{parsed.path}")
    return [token for token in tokens if token]


def _file_contains_any(path: Path, tokens: list[str]) -> bool:
    """Read a small cache file as bytes and search URL tokens defensively."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(token.encode("utf-8") in data for token in tokens)


def find_stale_caches(cache_dir: Path, max_age_hours: int = 24) -> List[Path]:
    """Return stale .spc cache files not modified within max_age_hours."""
    if not cache_dir.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    return [
        f
        for f in cache_dir.rglob("*.spc")
        if f.is_file() and f.stat().st_mtime < cutoff
    ]


def find_cache_files_for_url(cache_dir: Path, url: str) -> List[Path]:
    """Return cache files whose contents identify the requested feed URL."""
    tokens = _candidate_tokens(url)
    matched: set[Path] = set()
    for path in _cache_files(cache_dir):
        if not path.is_file() or path.suffix not in {".spc", ".html"}:
            continue
        if _file_contains_any(path, tokens):
            matched.add(path)
            if path.suffix == ".spc":
                matched.add(path.with_suffix(".html"))
            elif path.suffix == ".html":
                matched.add(path.with_suffix(".spc"))
    return sorted(p for p in matched if p.exists())


def delete_cache_files(files: Iterable[Path]) -> int:
    """Delete cache files and return the number actually removed."""
    count = 0
    for path in sorted(set(files)):
        try:
            path.unlink(missing_ok=True)
            count += 1
        except OSError:
            continue
    return count


def clear_stale_caches(cache_dir: Path, max_age_hours: int = 24) -> int:
    """Delete stale .spc cache files and their paired .html files."""
    targets: set[Path] = set()
    for spc in find_stale_caches(cache_dir, max_age_hours):
        targets.add(spc)
        targets.add(spc.with_suffix(".html"))
    return delete_cache_files(p for p in targets if p.exists())


def zero_state_path(state_dir: Path, source_name: str) -> Path:
    """Return the persistent zero-result state path for a source."""
    return state_dir / f"{source_name}_zero_state.json"


def record_zero_result(
    state_dir: Path, source_name: str, date: str | None = None
) -> int:
    """Record one zero-result day and return the consecutive zero-day count."""
    today = date or datetime.date.today().isoformat()
    path = zero_state_path(state_dir, source_name)
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    last_date = previous.get("last_zero_date", "")
    count = int(previous.get("consecutive_zero_days", 0) or 0)
    yesterday = (
        datetime.date.fromisoformat(today) - datetime.timedelta(days=1)
    ).isoformat()

    if last_date == today:
        new_count = max(count, 1)
    elif last_date == yesterday:
        new_count = count + 1
    else:
        new_count = 1

    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"last_zero_date": today, "consecutive_zero_days": new_count},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return new_count


def reset_zero_result(state_dir: Path, source_name: str) -> None:
    """Clear zero-result state when a source successfully returns items."""
    try:
        zero_state_path(state_dir, source_name).unlink(missing_ok=True)
    except OSError:
        pass


def refresh_freshrss(container: str = "dailyinfo_freshrss") -> subprocess.CompletedProcess:
    """Trigger FreshRSS feed refresh inside the container."""
    return subprocess.run(
        [
            "docker",
            "exec",
            container,
            "php",
            "/var/www/FreshRSS/app/actualize_script.php",
        ],
        text=True,
    )
