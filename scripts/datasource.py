"""DataSource abstraction layer — each source type handles its own fetching."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import datetime
import html as html_lib
import re
import requests
import time
from typing import Optional

NOW = datetime.datetime.now()


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


def build_feed_url_map(db) -> tuple[dict, dict]:
    """Build url→feed_id maps from FreshRSS DB for fast lookup."""
    full_map, base_map = {}, {}
    for fid, url in db.execute("SELECT id, url FROM feed").fetchall():
        clean = html_lib.unescape(url)
        full_map[clean] = fid
        base = clean.split("?")[0]
        if base not in base_map:
            base_map[base] = fid
    return full_map, base_map


def resolve_feed_id(url: str, full_map: dict, base_map: dict) -> Optional[int]:
    if not url:
        return None
    clean = html_lib.unescape(url)
    return full_map.get(clean) or base_map.get(clean.split("?")[0])


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
        self.max_batches: int = config.get("max_batches", 10)

    def fetch(self) -> list[Item]:
        if not self._db:
            return []
        fid = resolve_feed_id(
            self.config.get("url", ""), self._full_map, self._base_map
        )
        if not fid:
            return []

        if self.use_content:
            rows = self._db.execute(
                "SELECT title, content, link, date FROM entry "
                "WHERE id_feed=? AND date>? ORDER BY date DESC LIMIT 3",
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
            return items

        rows = self._db.execute(
            "SELECT title, link, date FROM entry WHERE id_feed=? AND date>? ORDER BY date DESC",
            [fid, self._cutoff_ts],
        ).fetchall()
        entries = list(rows)
        if self.max_articles and len(entries) > self.max_articles:
            entries = entries[: self.max_articles]
        return [
            Item(
                title=row["title"] or "",
                date=datetime.datetime.fromtimestamp(row["date"]).strftime("%Y-%m-%d"),
                url=row["link"] or "",
            )
            for row in entries
        ]

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
    def fetch(self) -> list[Item]:
        method = self.config.get("method", "GET").upper()
        params = self.config.get("params", {})
        if method == "POST":
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
        return self._parse_dlut_api(data)

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

    def _parse_dlut_api(self, api_data) -> list[Item]:
        max_items = self.config.get("max_items", 10)
        extract = self.config.get("extract", {})
        field_map = extract.get("fields", {})
        list_url = self.config.get("list_url", self.config.get("url", ""))
        cutoff = self._cutoff_dt
        items = []

        data_list: list = []
        if isinstance(api_data, dict):
            if "object" in api_data and isinstance(api_data["object"], dict):
                data_list = api_data["object"].get("list", [])
            elif "list" in api_data:
                data_list = api_data["list"]
        elif isinstance(api_data, list):
            data_list = api_data

        for row in data_list:
            if not isinstance(row, dict):
                continue
            title = row.get(field_map.get("title", "title"), "").strip()
            date_val = row.get(field_map.get("date", "date"), "")
            if not title:
                continue
            dt = None
            if isinstance(date_val, str) and date_val:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        dt = datetime.datetime.strptime(date_val[:19], fmt)
                        break
                    except ValueError:
                        pass
            if dt and dt < cutoff:
                continue
            items.append(
                Item(
                    title=title[:100],
                    date=(
                        dt.strftime("%Y-%m-%d")
                        if dt
                        else (date_val[:10] if date_val else "unknown")
                    ),
                    url=list_url,
                )
            )
            if len(items) >= max_items:
                break
        return items

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
