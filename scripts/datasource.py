"""DataSource abstraction layer — each source type handles its own fetching."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import datetime
import html as html_lib
import json
import os
import pathlib
import re
import requests
import time
from typing import Optional

NOW = datetime.datetime.now()


def _resolve_state_dir() -> pathlib.Path:
    override = os.environ.get("DAILYINFO_DATA_ROOT", "")
    root = pathlib.Path(override).expanduser() if override else pathlib.Path.home() / ".myagentdata" / "dailyinfo"
    return root / "state"


_STATE_DIR = _resolve_state_dir()


# ---------------------------------------------------------------------------
# Item — standardized output from any DataSource
# ---------------------------------------------------------------------------
@dataclass
class Item:
    title: str
    date: str  # YYYY-MM-DD
    url: str = ""
    content: str = ""  # populated by deep-content sources (e.g. SmolAI)
    extra: dict = field(
        default_factory=dict
    )  # source-specific fields (stars, likes, …)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def strip_html(text: str) -> str:
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(
        r"</?(?:p|div|h[1-6]|li|tr|td|th|blockquote)[^>]*>", "\n", text, flags=re.I
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _normalise_url(url: str) -> str:
    """Normalise a feed URL for tolerant matching."""
    u = html_lib.unescape(url).strip()
    u = re.sub(r"^https?://", "", u)          # strip scheme
    u = u.rstrip("/")                          # strip trailing slash
    return u


def build_feed_url_map(db) -> tuple[dict, dict]:
    """Build url→feed_id maps from FreshRSS DB for fast lookup."""
    full_map, base_map = {}, {}
    for fid, url in db.execute("SELECT id, url FROM feed").fetchall():
        clean = html_lib.unescape(url)
        full_map[clean] = fid
        base = clean.split("?")[0]
        if base not in base_map:
            base_map[base] = fid
        # normalised key for tolerant matching (no scheme, no trailing slash)
        nkey = _normalise_url(base)
        if nkey not in base_map:
            base_map[nkey] = fid
    return full_map, base_map


def resolve_feed_id(url: str, full_map: dict, base_map: dict) -> Optional[int]:
    if not url:
        return None
    clean = html_lib.unescape(url)
    # exact match
    fid = full_map.get(clean)
    if fid:
        return fid
    # base URL (strip query params)
    base = clean.split("?")[0]
    fid = base_map.get(base)
    if fid:
        return fid
    # tolerant match (ignore scheme + trailing slash)
    fid = base_map.get(_normalise_url(base))
    if fid:
        return fid
    return None


# ---------------------------------------------------------------------------
# Date parsers (DLUT site variants)
# ---------------------------------------------------------------------------
def _parse_date_dlut_news(date_html: str) -> Optional[datetime.datetime]:
    day_m = re.search(r"<span>(\d+)</span>", date_html)
    ym_m = re.search(r"(\d{4}-\d{2})", date_html)
    if day_m and ym_m:
        try:
            return datetime.datetime.strptime(
                f"{ym_m.group(1)}-{day_m.group(1).zfill(2)}", "%Y-%m-%d"
            )
        except ValueError:
            pass
    return None


def _parse_date_standard(date_str: str) -> Optional[datetime.datetime]:
    date_str = re.sub(r"<[^>]+>", "", date_str).strip()
    try:
        return datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        pass
    try:
        if re.match(r"^\d{1,2}-\d{1,2}$", date_str):
            dt = datetime.datetime.strptime(f"{NOW.year}-{date_str}", "%Y-%m-%d")
            return dt.replace(year=NOW.year - 1) if dt > NOW else dt
    except ValueError:
        pass
    return None


def _parse_date_dlut_future(date_html: str) -> Optional[datetime.datetime]:
    cleaned = re.sub(r"<[^>]+>", " ", date_html).strip()
    m = re.search(r"(\d{1,2})\s+(\d{4})[.\-](\d{2})", cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(2)), int(m.group(3)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"(\d{4})[.\-](\d{2})\s+(\d{1,2})", cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _parse_date_dlut_scidep(date_html: str) -> Optional[datetime.datetime]:
    cleaned = re.sub(r"<[^>]+>", " ", date_html).strip()
    m = re.search(r"(\d{1,2})\s+(\d{4})-(\d{2})", cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(2)), int(m.group(3)), int(m.group(1)))
        except ValueError:
            pass
    return None


def _parse_date_dlut_recruitment(date_val) -> Optional[datetime.datetime]:
    if isinstance(date_val, str) and " " in date_val and len(date_val) > 10:
        try:
            return datetime.datetime.strptime(date_val[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    cleaned = re.sub(r"<[^>]+>", " ", str(date_val)).strip()
    md_m = re.search(r"(\d{2})-(\d{2})", cleaned)
    yyyy_m = re.search(r"(\d{4})", cleaned)
    if md_m and yyyy_m:
        try:
            return datetime.datetime.strptime(
                f"{yyyy_m.group(1)}-{md_m.group(1)}-{md_m.group(2)}", "%Y-%m-%d"
            )
        except ValueError:
            pass
    if md_m:
        try:
            dt = datetime.datetime.strptime(
                f"{NOW.year}-{md_m.group(1)}-{md_m.group(2)}", "%Y-%m-%d"
            )
            return dt.replace(year=NOW.year - 1) if dt > NOW else dt
        except ValueError:
            pass
    return None


_DATE_PARSERS = {
    "dlut_news": _parse_date_dlut_news,
    "standard": _parse_date_standard,
    "dlut_future": _parse_date_dlut_future,
    "dlut_scidep": _parse_date_dlut_scidep,
    "dlut_recruitment": _parse_date_dlut_recruitment,
}


# ---------------------------------------------------------------------------
# DataSource base class
# ---------------------------------------------------------------------------
class DataSource(ABC):
    def __init__(self, config: dict, defaults: dict):
        self.config = config
        self.defaults = defaults
        self.name: str = config["name"]
        self.display_name: str = config.get("display_name", self.name)
        self.category: str = config.get("category", "general")
        self.lookback_hours: int = config.get("lookback_hours") or defaults.get(
            "lookback_hours", 24
        )
        self._cutoff_dt = NOW - datetime.timedelta(hours=self.lookback_hours)
        self._cutoff_ts = int(time.time()) - self.lookback_hours * 3600

    @abstractmethod
    def fetch(self) -> list[Item]:
        """Fetch and return items filtered to the lookback window."""
        ...

    def format_items(self, items: list[Item]) -> str:
        """Default formatting: numbered [date] title list."""
        return "\n".join(
            f"{i+1}. [{item.date}] {item.title}" for i, item in enumerate(items)
        )

    def get_batches(self, items: list[Item]) -> list[list[Item]]:
        """Split items into batches; override in subclasses for finer control."""
        batch_size = self.config.get("max_articles_per_batch")
        if not batch_size:
            return [items]
        return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]

    def commit_cursor(self) -> None:
        """Persist cursor after a successful briefing save. No-op for most sources."""

    @staticmethod
    def create(config: dict, defaults: dict, **ctx) -> "DataSource":
        """Factory: instantiate the correct subclass for config['type']."""
        t = config.get("type") or config.get("source_type", "scrape")
        if t == "rss":
            return RSSDataSource(
                config,
                defaults,
                db=ctx.get("db"),
                full_map=ctx.get("full_map", {}),
                base_map=ctx.get("base_map", {}),
            )
        if t == "api":
            return APIDataSource(config, defaults)
        return ScrapeDataSource(config, defaults)


# ---------------------------------------------------------------------------
# RSSDataSource — reads from FreshRSS SQLite
# ---------------------------------------------------------------------------
class RSSDataSource(DataSource):
    def __init__(self, config, defaults, db=None, full_map=None, base_map=None):
        super().__init__(config, defaults)
        self._db = db
        self._full_map = full_map or {}
        self._base_map = base_map or {}
        self.use_content: bool = config.get("use_content", False)
        self.max_articles: Optional[int] = config.get("max_articles")
        self.max_articles_per_batch: Optional[int] = config.get(
            "max_articles_per_batch"
        )
        self.max_batches: int = config.get("max_batches", 1000)
        self._seen: dict[str, str] = self._load_seen()  # link -> first-seen date
        self._total_before_filter: int = 0  # for logging

    # --- seen-links dedup ---
    def _seen_path(self) -> pathlib.Path:
        return _STATE_DIR / f"{self.name}_seen.json"

    def _load_seen(self) -> dict[str, str]:
        p = self._seen_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_seen(self) -> None:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._seen_path().write_text(
            json.dumps(self._seen, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _filter_seen(self, items: list[Item]) -> list[Item]:
        self._total_before_filter = len(items)
        return [it for it in items if it.url not in self._seen]

    def commit_seen(self, items: list[Item]) -> None:
        """Record item URLs as processed. Called after briefing is saved."""
        today = datetime.date.today().isoformat()
        for it in items:
            if it.url and it.url not in self._seen:
                self._seen[it.url] = today
        self._save_seen()

    def cleanup_seen(self, max_age_days: int = 30) -> None:
        """Remove seen records older than max_age_days."""
        cutoff = (datetime.date.today() - datetime.timedelta(days=max_age_days)).isoformat()
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        self._save_seen()

    def fetch(self) -> list[Item]:
        if not self._db:
            return []
        url = self.config.get("url", "")
        fid = resolve_feed_id(url, self._full_map, self._base_map)
        if not fid:
            available = list(self._full_map.values())[:5]
            sample_urls = [k for k, v in self._full_map.items() if v in available]
            print(
                f"  [WARN] resolve_feed_id: no match for {self.name!r} "
                f"(url={url!r}). DB has {len(self._full_map)} feeds. "
                f"Sample DB urls: {sample_urls[:5]}",
                flush=True,
            )
            return []

        if self.use_content:
            rows = self._db.execute(
                "SELECT title, content, link, date FROM entry "
                "WHERE id_feed=? AND lastSeen>? ORDER BY date DESC LIMIT 3",
                [fid, self._cutoff_ts],
            ).fetchall()
            items = []
            for row in rows:
                plain = strip_html(row["content"] or "")
                if len(plain) > 12000:
                    trunc = plain.rfind(" ", 0, 12000)
                    plain = (
                        plain[: max(trunc, 10000)] + "\n\n[... content truncated ...]"
                    )
                if len(plain) < 100:
                    continue
                items.append(
                    Item(
                        title=row["title"] or "",
                        date=datetime.datetime.fromtimestamp(row["date"]).strftime(
                            "%Y-%m-%d"
                        ),
                        url=row["link"] or "",
                        content=plain,
                    )
                )
            return self._filter_seen(items)

        rows = self._db.execute(
            "SELECT title, link, date FROM entry WHERE id_feed=? AND lastSeen>? ORDER BY date DESC",
            [fid, self._cutoff_ts],
        ).fetchall()
        entries = list(rows)
        if self.max_articles and len(entries) > self.max_articles:
            entries = entries[: self.max_articles]
        items = [
            Item(
                title=row["title"] or "",
                date=datetime.datetime.fromtimestamp(row["date"]).strftime("%Y-%m-%d"),
                url=row["link"] or "",
            )
            for row in entries
        ]
        return self._filter_seen(items)

    def get_batches(self, items: list[Item]) -> list[list[Item]]:
        """Split items into AI-processing batches respecting max_articles_per_batch."""
        if not self.max_articles_per_batch:
            return [items]
        batches = [
            items[i : i + self.max_articles_per_batch]
            for i in range(0, len(items), self.max_articles_per_batch)
        ]
        return batches[: self.max_batches]

    def format_items(self, items: list[Item]) -> str:
        return "\n".join(f"{i+1}. {item.title}" for i, item in enumerate(items))


# ---------------------------------------------------------------------------
# ScrapeDataSource — HTML scraping (GitHub Trending + DLUT sites)
# ---------------------------------------------------------------------------
class ScrapeDataSource(DataSource):
    def fetch(self) -> list[Item]:
        if self.name == "github_trending":
            return self._fetch_github()
        resp = requests.get(
            self.config["url"],
            timeout=20,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
        )
        resp.encoding = resp.apparent_encoding or "utf-8"
        if self.name == "skxjz":
            return self._parse_skxjz(resp.text)
        if self.name == "chinawater":
            return self._parse_chinawater(resp.text)
        return self._parse_dlut_html(resp.text)

    # --- GitHub Trending ---
    def _fetch_github(self) -> list[Item]:
        resp = requests.get(
            "https://github.com/trending?since=daily",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            timeout=30,
        )
        resp.raise_for_status()
        articles = re.findall(
            r'<article\s+class="Box-row[^"]*">([\s\S]*?)</article>', resp.text
        )
        if not articles:
            articles = re.findall(r"<article[^>]*>([\s\S]*?)</article>", resp.text)
        items = []
        for art in articles[:25]:
            name_m = re.search(
                r'<h2[^>]*>\s*<a[^>]+href="(/[^"]+)"[^>]*>([\s\S]*?)</a>', art
            )
            if not name_m:
                continue
            repo_path = name_m.group(1).strip().lstrip("/")
            repo_name = (
                re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", name_m.group(2))).strip()
                or repo_path
            )
            desc_m = re.search(r"<p[^>]*>([\s\S]*?)</p>", art)
            description = (
                strip_html(desc_m.group(1)).strip() if desc_m else "No description"
            )
            lang_m = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]+)', art)
            stars_m = re.search(r"([\d,]+)\s*stars\s*today", art)
            total_m = re.findall(r'href="/[^"]+/stargazers"[^>]*>\s*([\d,]+)', art)
            items.append(
                Item(
                    title=description,
                    date=NOW.strftime("%Y-%m-%d"),
                    url=f"https://github.com/{repo_path}",
                    extra={
                        "full_name": repo_path,
                        "name": repo_name,
                        "language": lang_m.group(1).strip() if lang_m else "",
                        "stars": total_m[0].replace(",", "") if total_m else "0",
                        "stars_today": (
                            stars_m.group(1).replace(",", "") if stars_m else "0"
                        ),
                    },
                )
            )
        return items

    def _parse_skxjz(self, page_html: str) -> list[Item]:
        """水科学进展 (skxjz.nhri.cn) — current-issue article list.

        DOI issue numbers don't map to calendar months, so use today's date
        for all items; the _already_pushed_within guard prevents re-delivery.
        """
        max_items = self.config.get("max_items", 30)
        base_url = "http://skxjz.nhri.cn"
        today = NOW.strftime("%Y-%m-%d")
        rgx = re.compile(
            r'class="article-list-title[^"]*"[^>]*>[\s\S]*?'
            r"<a\s+href=['\"](?P<path>/article/doi/[^'\"]+)['\"][^>]*>\s*(?P<title>[\s\S]*?)\s*</a>",
            re.I,
        )
        items = []
        for m in rgx.finditer(page_html):
            title = re.sub(r"<[^>]+>", "", m.group("title")).strip()
            if not title:
                continue
            items.append(Item(title=title, date=today, url=base_url + m.group("path")))
            if len(items) >= max_items:
                break
        return items

    def _parse_chinawater(self, page_html: str) -> list[Item]:
        """中国水利 (chinawater.com.cn) — news list with date embedded in URL."""
        max_items = self.config.get("max_items", 20)
        base_url = self.config.get("base_url", "http://www.chinawater.com.cn")
        rgx = re.compile(
            r'<a[^>]+href=["\']([^"\']*t(\d{8})[^"\']*\.html)["\'][^>]*>'
            r"\s*([^<]{3,150})\s*</a>",
            re.I,
        )
        items: list[Item] = []
        seen: set[str] = set()
        for m in rgx.finditer(page_html):
            href, date_str, raw_title = m.group(1), m.group(2), m.group(3).strip()
            title = html_lib.unescape(raw_title).strip()
            if not title or title in seen:
                continue
            seen.add(title)
            try:
                dt = datetime.datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                dt = None
            if dt and dt < self._cutoff_dt:
                continue
            url = href if href.startswith("http") else f"{base_url}/{href.lstrip('./')}"
            items.append(Item(
                title=title,
                date=dt.strftime("%Y-%m-%d") if dt else NOW.strftime("%Y-%m-%d"),
                url=url,
            ))
            if len(items) >= max_items:
                break
        return items

    def format_items(self, items: list[Item]) -> str:
        if self.name == "github_trending":
            lines = []
            for i, item in enumerate(items):
                e = item.extra
                lang = f' [{e["language"]}]' if e.get("language") else ""
                lines.append(
                    f'{i+1}. **{e.get("full_name", item.title)}**{lang} '
                    f'(total {e.get("stars",0)}, +{e.get("stars_today",0)} today) - {item.title}\n'
                    f"   {item.url}"
                )
            return "\n".join(lines)
        return super().format_items(items)

    # --- DLUT HTML parsing ---
    def _parse_dlut_html(self, page_html: str) -> list[Item]:
        name = self.name
        base_url = self.config.get("base_url", "")
        max_items = self.config.get("max_items", 10)
        date_format = self.config.get("date_format", "standard")
        date_parser = _DATE_PARSERS.get(date_format, _parse_date_standard)
        cutoff = self._cutoff_dt
        items = []

        if date_format == "dlut_news":
            rgx = re.compile(
                r'<li[^>]*class=["\'][^"\']*bg-mask[^"\']*["\'][^>]*>'
                r"[\s\S]*?<time[^>]*>([\s\S]*?)</time>"
                r'[\s\S]*?<h4>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
                r"[\s\S]*?</li>",
                re.I,
            )
            for m in rgx.finditer(page_html):
                time_html, href, title_raw = m.group(1), m.group(2), m.group(3)
                dt = date_parser(time_html)
                if dt and dt < cutoff:
                    continue
                url = href if href.startswith("http") else base_url + href.lstrip("./")
                items.append(
                    Item(
                        title=title_raw.strip()[:100],
                        date=dt.strftime("%Y-%m-%d") if dt else "unknown",
                        url=url,
                    )
                )
                if len(items) >= max_items:
                    break

        elif name == "dlut_sche":
            rgx = re.compile(
                r'<li[^>]*style[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
                r'\s*<span[^>]*class=["\'][^"\']*date[^"\']*["\'][^>]*>([^<]+)</span>',
                re.I,
            )
            for m in rgx.finditer(page_html):
                href, title, date_raw = (
                    m.group(1),
                    m.group(2).strip(),
                    m.group(3).strip(),
                )
                dt = date_parser(date_raw)
                if dt and dt < cutoff:
                    continue
                url = href if href.startswith("http") else base_url + href.lstrip("./")
                items.append(
                    Item(
                        title=title,
                        date=dt.strftime("%Y-%m-%d") if dt else date_raw,
                        url=url,
                    )
                )
                if len(items) >= max_items:
                    break

        elif name == "dlut_futureschool":
            rgx = re.compile(
                r"<li[^>]*>"
                r'[\s\S]*?<div[^>]*class=["\'][^"\']*time[^"\']*["\'][^>]*>([\s\S]*?)</div>'
                r'[\s\S]*?<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*name[^"\']*["\'][^>]*>'
                r"\s*([^<]+?)\s*</a>[\s\S]*?</li>",
                re.I,
            )
            for m in rgx.finditer(page_html):
                date_html_inner, href, title = (
                    m.group(1),
                    m.group(2),
                    m.group(3).strip(),
                )
                dt = date_parser(date_html_inner)
                if dt and dt < cutoff:
                    continue
                url = href if href.startswith("http") else base_url + href.lstrip("./")
                items.append(
                    Item(
                        title=title,
                        date=dt.strftime("%Y-%m-%d") if dt else "unknown",
                        url=url,
                    )
                )
                if len(items) >= max_items:
                    break

        elif name == "dlut_scidep":
            rgx = re.compile(
                r'<li[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
                r'[\s\S]*?<div[^>]*class=["\'][^"\']*tz-ul-date[^"\']*["\'][^>]*>([\s\S]*?)</div>'
                r'[\s\S]*?<div[^>]*class=["\'][^"\']*tz-ul-tt[^"\']*["\'][^>]*>([^<]+)</div>'
                r"[\s\S]*?</a>\s*</li>",
                re.I,
            )
            for m in rgx.finditer(page_html):
                href, date_html_inner, title = (
                    m.group(1),
                    m.group(2),
                    m.group(3).strip(),
                )
                dt = date_parser(date_html_inner)
                if dt and dt < cutoff:
                    continue
                url = href if href.startswith("http") else base_url + href.lstrip("./")
                items.append(
                    Item(
                        title=title,
                        date=dt.strftime("%Y-%m-%d") if dt else "unknown",
                        url=url,
                    )
                )
                if len(items) >= max_items:
                    break

        return items


# ---------------------------------------------------------------------------
# APIDataSource — REST API sources (HuggingFace + DLUT recruitment)
# ---------------------------------------------------------------------------
class APIDataSource(DataSource):
    def __init__(self, config: dict, defaults: dict):
        super().__init__(config, defaults)
        self._pending_cursor: Optional[dict] = None

    def _load_cursor(self) -> Optional[dict]:
        cursor_file = _STATE_DIR / f"{self.name}_cursor.json"
        if cursor_file.exists():
            try:
                return json.loads(cursor_file.read_text())
            except Exception:
                return None
        return None

    def commit_cursor(self) -> None:
        if self._pending_cursor is None:
            return
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        cursor_file = _STATE_DIR / f"{self.name}_cursor.json"
        cursor_file.write_text(json.dumps(self._pending_cursor))
        self._pending_cursor = None

    def fetch(self) -> list[Item]:
        method = self.config.get("method", "GET").upper()
        params = self.config.get("params", {})
        if method == "POST":
            if self.config.get("paginate"):
                return self._fetch_dlut_paginated(params)
            resp = requests.post(self.config["url"], data=params, timeout=20)
        else:
            params_str = {k: str(v) for k, v in params.items()}
            headers = dict(self.config.get("headers", {}))
            headers["User-Agent"] = "DailyInfo-Bot/1.0"
            resp = requests.get(
                self.config["url"], params=params_str, headers=headers, timeout=30
            )
        resp.raise_for_status()
        data = resp.json()

        if self.name.startswith("huggingface_"):
            return self._parse_huggingface(data)
        if self.config.get("parser") == "crossref":
            return self._parse_crossref(data)
        return self._parse_dlut_api(data)

    def _fetch_dlut_paginated(self, base_params: dict) -> list[Item]:
        """Paginate DLUT POST API.

        Uses cursor-based fetching when a saved cursor exists (no time window,
        no duplicates). Falls back to lookback_hours on first run or reset.
        """
        cursor = self._load_cursor()
        max_items = self.config.get("max_items", 100)
        all_items: list[Item] = []
        page = 1
        max_pages = 20

        while page <= max_pages:
            params = {**base_params, "pageNo": page}
            resp = requests.post(self.config["url"], data=params, timeout=20)
            resp.raise_for_status()
            api_data = resp.json()

            obj = api_data.get("object", {}) if isinstance(api_data, dict) else {}
            data_list = obj.get("list", []) if isinstance(obj, dict) else []
            last_page = obj.get("lastPage", True)

            new_items, should_stop = self._parse_dlut_api_rows(data_list, cursor=cursor)
            all_items.extend(new_items)

            if should_stop or last_page or (max_items and len(all_items) >= max_items):
                break
            page += 1

        result = all_items[:max_items] if max_items else all_items

        # Stage the cursor — only written to disk when commit_cursor() is called
        # (i.e. after run_pipelines confirms the briefing was saved successfully).
        if result:
            newest = result[0]  # API returns newest first
            self._pending_cursor = {
                "last_id": newest.extra.get("item_id", ""),
                "last_time": newest.extra.get("item_time", newest.date),
            }

        return result

    def _parse_crossref(self, api_data: dict) -> list[Item]:
        """Crossref REST API (/works) — returns English titles with publication dates."""
        rows = api_data.get("message", {}).get("items", [])
        max_items = self.config.get("max_items", 20)
        items: list[Item] = []
        for row in rows:
            titles = row.get("title") or []
            title = titles[0] if titles else ""
            if not title:
                continue
            pub = (
                row.get("published")
                or row.get("published-print")
                or row.get("published-online")
                or {}
            )
            parts = (pub.get("date-parts") or [[]])[0]
            try:
                if len(parts) >= 3:
                    dt: Optional[datetime.datetime] = datetime.datetime(parts[0], parts[1], parts[2])
                elif len(parts) >= 2:
                    dt = datetime.datetime(parts[0], parts[1], 1)
                else:
                    dt = None
            except (ValueError, TypeError):
                dt = None
            if dt and dt < self._cutoff_dt:
                continue
            items.append(Item(
                title=title,
                date=dt.strftime("%Y-%m-%d") if dt else NOW.strftime("%Y-%m-%d"),
                url=row.get("URL", ""),
                extra={"doi": row.get("DOI", "")},
            ))
            if len(items) >= max_items:
                break

        chinese_title_url = self.config.get("chinese_title_url", "")
        if chinese_title_url and items:
            self._enrich_chinese_titles(items, chinese_title_url)

        return items

    _CHINESE_TITLE_RGX = re.compile(
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        re.I,
    )

    def _enrich_chinese_titles(self, items: list[Item], url_template: str) -> None:
        """Fetch Chinese titles from per-article pages and replace English titles in-place."""
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"}
        for item in items:
            doi = item.extra.get("doi", "")
            if not doi:
                continue
            url = url_template.format(doi=doi)
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.ok:
                    m = self._CHINESE_TITLE_RGX.search(resp.text)
                    if m:
                        item.title = html_lib.unescape(m.group(1)).strip()
            except Exception:
                pass

    def _parse_huggingface(self, data) -> list[Item]:
        extract = self.config.get("extract", {})
        field_map = extract.get("fields", {})
        max_items = self.config.get("max_items", 25)
        if not isinstance(data, list):
            data = []
        items = []
        for row in data[:max_items]:
            extracted = {out_k: row.get(src_k) for out_k, src_k in field_map.items()}
            name = extracted.get("name", "")
            items.append(
                Item(
                    title=name,
                    date=NOW.strftime("%Y-%m-%d"),
                    url=f"https://huggingface.co/{name}",
                    extra=extracted,
                )
            )
        return items

    def _parse_dlut_api_rows(
        self, data_list: list, cursor: Optional[dict] = None
    ) -> tuple[list[Item], bool]:
        """Process a list of rows from a DLUT API page.

        Returns (new_items, should_stop).

        Cursor mode (cursor provided): stop when the previously-seen item is
        reached (matched by ID, or by timestamp as fallback). No time window.

        Lookback mode (no cursor): stop when an item older than the lookback
        cutoff is encountered (original behaviour).
        """
        extract = self.config.get("extract", {})
        field_map = extract.get("fields", {})
        list_url = self.config.get("list_url", self.config.get("url", ""))
        items: list[Item] = []
        should_stop = False

        cursor_id = (cursor or {}).get("last_id", "")
        cursor_time_str = (cursor or {}).get("last_time", "")
        cursor_dt: Optional[datetime.datetime] = None
        if cursor_time_str:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    cursor_dt = datetime.datetime.strptime(cursor_time_str[:19], fmt)
                    break
                except ValueError:
                    pass

        for row in data_list:
            if not isinstance(row, dict):
                continue
            title = row.get(field_map.get("title", "title"), "").strip()
            date_val = row.get(field_map.get("date", "date"), "")
            item_id = row.get("id", "")
            if not title:
                continue
            dt: Optional[datetime.datetime] = None
            if isinstance(date_val, str) and date_val:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        dt = datetime.datetime.strptime(date_val[:19], fmt)
                        break
                    except ValueError:
                        pass

            if cursor:
                # Cursor mode: stop at the previously-seen item
                if cursor_id and item_id == cursor_id:
                    should_stop = True
                    break
                if cursor_dt and dt and dt < cursor_dt:
                    # Fallback: timestamp strictly before cursor → already seen
                    should_stop = True
                    break
            else:
                # Lookback mode: skip items older than the time cutoff
                if dt and dt < self._cutoff_dt:
                    should_stop = True
                    continue

            items.append(
                Item(
                    title=title[:100],
                    date=(dt.strftime("%Y-%m-%d") if dt else (date_val[:10] if date_val else "unknown")),
                    url=list_url,
                    extra={"item_id": item_id, "item_time": date_val},
                )
            )

        return items, should_stop

    def _parse_dlut_api(self, api_data) -> list[Item]:
        max_items = self.config.get("max_items", 10)
        data_list: list = []
        if isinstance(api_data, dict):
            if "object" in api_data and isinstance(api_data["object"], dict):
                data_list = api_data["object"].get("list", [])
            elif "list" in api_data:
                data_list = api_data["list"]
        elif isinstance(api_data, list):
            data_list = api_data

        items, _ = self._parse_dlut_api_rows(data_list)
        return items[:max_items] if max_items else items

    def format_items(self, items: list[Item]) -> str:
        if self.name == "huggingface_models":
            return "\n".join(
                f'{i+1}. **{item.extra.get("name","")}**'
                f'{" ("+item.extra["task"]+")" if item.extra.get("task") else ""}'
                f' - likes {item.extra.get("likes",0)}, downloads {item.extra.get("downloads",0)}'
                for i, item in enumerate(items)
            )
        if self.name == "huggingface_datasets":
            return "\n".join(
                f'{i+1}. **{item.extra.get("name","")}**'
                f' - likes {item.extra.get("likes",0)}, downloads {item.extra.get("downloads",0)}'
                for i, item in enumerate(items)
            )
        if self.name == "huggingface_spaces":
            return "\n".join(
                f'{i+1}. **{item.extra.get("name","")}**'
                f'{" ["+item.extra["sdk"]+"]" if item.extra.get("sdk") else ""}'
                f' - likes {item.extra.get("likes",0)}'
                for i, item in enumerate(items)
            )
        return super().format_items(items)
