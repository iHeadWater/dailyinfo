#!/usr/bin/env python3
"""DailyInfo Pipeline Runner — generates daily briefing files.

Reads RSS feeds from FreshRSS, scrapes GitHub/HuggingFace trending,
scrapes DUT university news, then calls OpenRouter AI for summaries.
Output files are saved to ~/.openclaw/workspace/briefings/{category}/.

Usage:
    python3 scripts/run_pipelines.py              # run all 3 pipelines
    python3 scripts/run_pipelines.py --pipeline 1  # RSS papers + AI news only
    python3 scripts/run_pipelines.py --pipeline 2  # code trending only
    python3 scripts/run_pipelines.py --pipeline 3  # university news only
"""

import sqlite3, json, os, re, sys, requests, datetime, time, argparse
import html as html_lib

# ---------------------------------------------------------------------------
# Paths — resolved relative to this script's parent (the project root)
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')
SOURCES_JSON = os.path.join(CONFIG_DIR, 'sources.json')
WORKSPACE = os.path.expanduser('~/.openclaw/workspace')
BRIEFINGS_DIR = os.path.join(WORKSPACE, 'briefings')

DATE = datetime.datetime.now().strftime('%Y-%m-%d')
NOW = datetime.datetime.now()

# Load FreshRSS database path dynamically from config
def _get_freshrss_db():
    try:
        with open(SOURCES_JSON) as f:
            cfg = json.load(f)
        user = cfg.get('defaults', {}).get('freshrss_user', 'owen')
        return os.path.expanduser(f'~/.freshrss/data/users/{user}/db.sqlite')
    except Exception:
        return os.path.expanduser('~/.freshrss/data/users/owen/db.sqlite')

FRESHRSS_DB = _get_freshrss_db()

def _build_feed_url_map(db):
    """Build url→feed_id mapping from FreshRSS DB. Matches by full URL or base URL (no query string)."""
    full_map, base_map = {}, {}
    for fid, url in db.execute('SELECT id, url FROM feed').fetchall():
        url_clean = html_lib.unescape(url)
        full_map[url_clean] = fid
        base = url_clean.split('?')[0]
        if base not in base_map:
            base_map[base] = fid
    return full_map, base_map

def _resolve_feed_id(url, full_map, base_map):
    if not url:
        return None
    url_clean = html_lib.unescape(url)
    if url_clean in full_map:
        return full_map[url_clean]
    return base_map.get(url_clean.split('?')[0])

# ---------------------------------------------------------------------------
# Load API key
# ---------------------------------------------------------------------------
def load_api_key():
    """Load OpenRouter API key from .env file."""
    env_path = os.path.join(PROJECT_ROOT, '.env')
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values
            vals = dotenv_values(env_path)
            key = vals.get('OPENROUTER_API_KEY', '')
            if key and not key.startswith('your_'):
                return key
        except ImportError:
            # Fallback: parse .env manually
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('OPENROUTER_API_KEY=') and 'your_' not in line:
                        return line.split('=', 1)[1].strip()
    # Fallback: environment variable
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if key:
        return key
    log('ERROR: No OPENROUTER_API_KEY found in .env or environment')
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)

def call_ai(prompt, model='anthropic/claude-haiku-4.5', max_tokens=1200):
    resp = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {API_KEY}', 'Content-Type': 'application/json'},
        json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens},
        timeout=120
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']

def save(directory, filename, content):
    path = os.path.join(BRIEFINGS_DIR, directory)
    os.makedirs(path, exist_ok=True)
    full = os.path.join(path, filename)
    with open(full, 'w') as f:
        f.write(content)
    return full

def strip_html(text):
    """Strip HTML tags and decode entities."""
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</?(?:p|div|h[1-6]|li|tr|td|th|blockquote)[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = html_lib.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()

# =====================================================================
# PIPELINE 1: Daily Briefing (papers + AI news via FreshRSS)
# =====================================================================
def run_pipeline_1():
    log('=== Pipeline 1: Daily Briefing (papers + AI news) ===')
    with open(SOURCES_JSON) as f:
        sources_cfg = json.load(f)

    defaults = sources_cfg.get('defaults', {})
    templates = sources_cfg.get('prompt_templates', {})
    default_tmpl_key = defaults.get('prompt_template', 'one_line_summary')
    feeds_cfg = {'feeds': [s for s in sources_cfg['sources'] if s.get('type') == 'rss']}

    db = sqlite3.connect(FRESHRSS_DB)
    db.row_factory = sqlite3.Row
    full_map, base_map = _build_feed_url_map(db)
    saved = 0

    for feed in feeds_cfg['feeds']:
        if not feed.get('enabled', True):
            continue

        fid = _resolve_feed_id(feed.get('url'), full_map, base_map)
        if not fid:
            continue
        lookback = feed.get('lookback_hours') or defaults.get('lookback_hours', 24)
        name = feed['name']
        category = feed.get('category', 'papers')
        display_name = feed.get('display_name', name)
        model = feed.get('model') or defaults.get('model', 'anthropic/claude-haiku-4.5')
        use_content = feed.get('use_content', False)
        cutoff = int(time.time()) - lookback * 3600

        # --- SmolAI deep content path ---
        if use_content:
            entries = db.execute(
                'SELECT title, content, link, date FROM entry WHERE id_feed=? AND date>? ORDER BY date DESC LIMIT 3',
                [fid, cutoff]
            ).fetchall()
            if not entries:
                log(f'  {name}: 0 articles (deep content) — generating placeholder')
                placeholder = f'# {display_name} - {DATE}\n\n📭 过去 {lookback} 小时无新内容\n'
                save(category, f'{name}_briefing_{DATE}.md', placeholder)
                saved += 1
                continue
            log(f'  {name}: {len(entries)} articles (deep content)')

            tmpl_key = feed.get('prompt_template', 'smolai_categorized')
            prompt_template = templates.get(tmpl_key, '')

            for idx, entry in enumerate(entries):
                raw_content = entry['content'] or ''
                plain_text = strip_html(raw_content)
                if len(plain_text) > 12000:
                    # Truncate at word boundary to avoid splitting words
                    trunc_point = plain_text.rfind(' ', 0, 12000)
                    if trunc_point < 10000:
                        trunc_point = 12000
                    plain_text = plain_text[:trunc_point] + '\n\n[... content truncated ...]'
                if len(plain_text) < 100:
                    log(f'    entry {idx}: too short, skip')
                    continue

                if prompt_template:
                    prompt = prompt_template.replace('{content}', plain_text).replace('{date}', DATE)
                else:
                    prompt = f'Summarize the following AI news in Chinese by category:\n\n{plain_text}'

                batch_suffix = f'_part{idx+1}' if len(entries) > 1 else ''
                filename = f'{name}_briefing_{DATE}{batch_suffix}.md'
                try:
                    content = call_ai(prompt, model=model, max_tokens=2000)
                    header = f'# AI Daily Digest - {DATE}\n\n'
                    save(category, filename, header + content)
                    saved += 1
                    log(f'    -> saved {filename}')
                    time.sleep(1)
                except Exception as e:
                    log(f'    ERR: {e}')
            continue

        # --- Regular RSS title-based path ---
        tmpl_key = feed.get('prompt_template') or default_tmpl_key
        prompt_template = templates.get(tmpl_key) or templates.get('one_line_summary', '')
        if not prompt_template:
            log(f'  SKIP {name}: no prompt template')
            continue

        max_per_batch = feed.get('max_articles_per_batch')
        max_batches = feed.get('max_batches', 10)

        entries = db.execute(
            'SELECT title, link, date FROM entry WHERE id_feed=? AND date>? ORDER BY date DESC',
            [fid, cutoff]
        ).fetchall()
        if not entries:
            log(f'  {name}: 0 articles — generating placeholder')
            placeholder = f'# {display_name} - {DATE}\n\n📭 过去 {lookback} 小时无新内容\n'
            save(category, f'{name}_briefing_{DATE}.md', placeholder)
            saved += 1
            continue

        entries = list(entries)

        # 如果配置了 max_articles，只取前 N 篇
        max_articles = feed.get('max_articles')
        if max_articles and len(entries) > max_articles:
            log(f'  {name}: {len(entries)} articles (限制到 {max_articles} 篇)')
            entries = entries[:max_articles]
        else:
            log(f'  {name}: {len(entries)} articles')

        if not max_per_batch:
            batches = [entries]
        else:
            batches = [entries[i:i+max_per_batch] for i in range(0, len(entries), max_per_batch)][:max_batches]

        for idx, batch in enumerate(batches):
            article_list = '\n'.join(f'{i+1}. {e["title"]}' for i, e in enumerate(batch))
            prompt = prompt_template \
                .replace('{count}', str(len(batch))) \
                .replace('{display_name}', display_name) \
                .replace('{article_list}', article_list) \
                .replace('{date}', DATE)

            batch_suffix = f'_batch{idx+1}' if max_per_batch else ''
            filename = f'{name}_briefing_{DATE}{batch_suffix}.md'
            try:
                content = call_ai(prompt, model=model)
                save(category, filename, content)
                saved += 1
                log(f'    -> saved {filename}')
                time.sleep(0.5)
            except Exception as e:
                log(f'    ERR: {e}')

    db.close()
    log(f'  Pipeline 1 done: {saved} files saved')
    return saved

# =====================================================================
# PIPELINE 2: Code Trending (GitHub + HuggingFace)
# =====================================================================
def scrape_github_trending():
    """Scrape github.com/trending HTML page for real trending repos."""
    resp = requests.get(
        'https://github.com/trending?since=daily',
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
        timeout=30
    )
    resp.raise_for_status()
    page_html = resp.text

    items = []
    articles = re.findall(r'<article\s+class="Box-row[^"]*">([\s\S]*?)</article>', page_html)
    if not articles:
        articles = re.findall(r'<article[^>]*>([\s\S]*?)</article>', page_html)

    for art in articles[:25]:
        name_m = re.search(r'<h2[^>]*>\s*<a[^>]+href="(/[^"]+)"[^>]*>([\s\S]*?)</a>', art)
        if not name_m:
            continue
        repo_path = name_m.group(1).strip().lstrip('/')
        repo_name = re.sub(r'\s+', '', re.sub(r'<[^>]+>', '', name_m.group(2))).strip()
        if not repo_name:
            repo_name = repo_path

        desc_m = re.search(r'<p[^>]*>([\s\S]*?)</p>', art)
        description = strip_html(desc_m.group(1)).strip() if desc_m else 'No description'

        lang_m = re.search(r'itemprop="programmingLanguage"[^>]*>([^<]+)', art)
        language = lang_m.group(1).strip() if lang_m else ''

        stars_m = re.search(r'([\d,]+)\s*stars\s*today', art)
        stars_today = stars_m.group(1).replace(',', '') if stars_m else '0'

        total_m = re.findall(r'href="/[^"]+/stargazers"[^>]*>\s*([\d,]+)', art)
        total_stars = total_m[0].replace(',', '') if total_m else '0'

        items.append({
            'name': repo_name,
            'full_name': repo_path,
            'description': description,
            'language': language,
            'stars': total_stars,
            'stars_today': stars_today,
            'url': f'https://github.com/{repo_path}'
        })

    return items


def run_pipeline_2():
    log('=== Pipeline 2: Code Trending ===')
    with open(SOURCES_JSON) as f:
        sources_cfg = json.load(f)

    scraper_defaults = sources_cfg.get('defaults', {})
    scraper_model = scraper_defaults.get('model', 'anthropic/claude-haiku-4.5')
    saved = 0

    for source in sources_cfg['sources']:
        if source.get('category') != 'code' or source.get('enabled') is False:
            continue

        name = source['name']
        display_name = source['display_name']
        log(f'  {name}...')

        items_list = ''
        try:
            if name == 'github_trending':
                items = scrape_github_trending()
                if not items:
                    log(f'    no items (scrape failed)')
                    continue
                log(f'    {len(items)} repos')
                items_list = '\n'.join(
                    f'{i+1}. **{item["full_name"]}**'
                    f'{" ["+item["language"]+"]" if item["language"] else ""} '
                    f'(total {item["stars"]}, +{item["stars_today"]} today) - {item["description"]}\n'
                    f'   {item["url"]}'
                    for i, item in enumerate(items)
                )
            else:
                # HuggingFace API sources
                params = {k: str(v) for k, v in (source.get('params') or {}).items()}
                headers_dict = dict(source.get('headers', {}))
                headers_dict['User-Agent'] = 'DailyInfo-Bot/1.0'

                resp = requests.get(source['url'], params=params, headers=headers_dict, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                extract = source.get('extract', {})
                items_path = extract.get('items_path')
                raw_items = data.get(items_path, data) if items_path else data
                if not isinstance(raw_items, list):
                    raw_items = []
                raw_items = raw_items[:source.get('max_items', 25)]

                fields = extract.get('fields', {})
                items = [{out_k: item.get(src_k) for out_k, src_k in fields.items()} for item in raw_items]

                if not items:
                    log(f'    no items')
                    continue

                log(f'    {len(items)} items')

                if name == 'huggingface_models':
                    items_list = '\n'.join(
                        f'{i+1}. **{item.get("name","")}**'
                        f'{" ("+item["task"]+")" if item.get("task") else ""}'
                        f' - likes {item.get("likes",0)}, downloads {item.get("downloads",0)}'
                        for i, item in enumerate(items)
                    )
                elif name == 'huggingface_datasets':
                    items_list = '\n'.join(
                        f'{i+1}. **{item.get("name","")}**'
                        f' - likes {item.get("likes",0)}, downloads {item.get("downloads",0)}'
                        for i, item in enumerate(items)
                    )
                elif name == 'huggingface_spaces':
                    items_list = '\n'.join(
                        f'{i+1}. **{item.get("name","")}**'
                        f'{" ["+item["sdk"]+"]" if item.get("sdk") else ""}'
                        f' - likes {item.get("likes",0)}'
                        for i, item in enumerate(items)
                    )
                else:
                    items_list = '\n'.join(f'{i+1}. {json.dumps(item)}' for i, item in enumerate(items))

        except Exception as e:
            log(f'    FETCH ERR: {e}')
            continue

        prompt = (
            f'You are a senior tech editor. For each of the following {display_name} items, '
            f'generate a one-line Chinese summary highlighting core features/innovations.\n\n'
            f'{items_list}\n\n'
            f'Requirements:\n'
            f'- Keep original numbering and project name\n'
            f'- One line per item: number. **project name** - one-line Chinese description\n'
            f'- Be concise and precise, highlight technical value\n'
            f'- For code projects, explain what problem it solves\n'
            f'- For models/datasets, explain use cases'
        )

        try:
            content = call_ai(prompt, model=source.get('model', scraper_model), max_tokens=1500)
            save('code', f'{name}_briefing_{DATE}.md', f'# {display_name} - {DATE}\n\n{content}')
            saved += 1
            log(f'    -> saved {name}_briefing_{DATE}.md')
            time.sleep(1)
        except Exception as e:
            log(f'    AI ERR: {e}')

    log(f'  Pipeline 2 done: {saved} files saved')
    return saved

# =====================================================================
# PIPELINE 3: University News (DUT HTML scraping)
# =====================================================================
def parse_date_dlut_news(date_html):
    day_m = re.search(r'<span>(\d+)</span>', date_html)
    ym_m = re.search(r'(\d{4}-\d{2})', date_html)
    if day_m and ym_m:
        try:
            return datetime.datetime.strptime(f'{ym_m.group(1)}-{day_m.group(1).zfill(2)}', '%Y-%m-%d')
        except ValueError:
            pass
    return None

def parse_date_standard(date_str):
    date_str = re.sub(r'<[^>]+>', '', date_str).strip()
    try:
        return datetime.datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        pass
    # Try MM-DD format for current or previous year
    try:
        if re.match(r'^\d{1,2}-\d{1,2}$', date_str):
            dt = datetime.datetime.strptime(f'{NOW.year}-{date_str}', '%Y-%m-%d')
            if dt > NOW:
                dt = dt.replace(year=NOW.year - 1)
            return dt
    except ValueError:
        pass
    return None

def parse_date_dlut_future(date_html):
    cleaned = re.sub(r'<[^>]+>', ' ', date_html).strip()
    m = re.search(r'(\d{1,2})\s+(\d{4})[.\-](\d{2})', cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(2)), int(m.group(3)), int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r'(\d{4})[.\-](\d{2})\s+(\d{1,2})', cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None

def parse_date_dlut_scidep(date_html):
    cleaned = re.sub(r'<[^>]+>', ' ', date_html).strip()
    m = re.search(r'(\d{1,2})\s+(\d{4})-(\d{2})', cleaned)
    if m:
        try:
            return datetime.datetime(int(m.group(2)), int(m.group(3)), int(m.group(1)))
        except ValueError:
            pass
    return None

def parse_date_dlut_recruitment(date_html):
    """Parse DLUT job portal date format: can be HTML with MM-dd and yyyy, or ISO datetime string."""
    # If it's a datetime string (e.g., "2026-04-15 10:17:09"), parse directly
    if isinstance(date_html, str) and ' ' in date_html and len(date_html) > 10:
        try:
            return datetime.datetime.strptime(date_html[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            pass

    # Otherwise treat as HTML with MM-DD and yyyy
    cleaned = re.sub(r'<[^>]+>', ' ', str(date_html)).strip()
    # Look for MM-DD pattern and yyyy pattern
    md_m = re.search(r'(\d{2})-(\d{2})', cleaned)
    yyyy_m = re.search(r'(\d{4})', cleaned)
    if md_m and yyyy_m:
        try:
            return datetime.datetime.strptime(f'{yyyy_m.group(1)}-{md_m.group(1)}-{md_m.group(2)}', '%Y-%m-%d')
        except ValueError:
            pass
    # Fallback: treat as MM-DD and assume current year
    if md_m:
        try:
            dt = datetime.datetime.strptime(f'{NOW.year}-{md_m.group(1)}-{md_m.group(2)}', '%Y-%m-%d')
            if dt > NOW:
                dt = dt.replace(year=NOW.year - 1)
            return dt
        except ValueError:
            pass
    return None

DATE_PARSERS = {
    'dlut_news': parse_date_dlut_news,
    'standard': parse_date_standard,
    'dlut_future': parse_date_dlut_future,
    'dlut_scidep': parse_date_dlut_scidep,
    'dlut_recruitment': parse_date_dlut_recruitment,
}

def parse_html_v2(source, page_html):
    """Generic HTML parser with date filtering for all DUT site variants."""
    name = source['name']
    base_url = source.get('base_url', '')
    max_items = source.get('max_items', 10)
    date_format = source.get('date_format', 'standard')
    lookback_hours = source.get('lookback_hours', 48)
    date_parser = DATE_PARSERS.get(date_format, parse_date_standard)
    cutoff = NOW - datetime.timedelta(hours=lookback_hours)

    items = []

    if date_format == 'dlut_news':
        rgx = re.compile(
            r'<li[^>]*class=["\'][^"\']*bg-mask[^"\']*["\'][^>]*>'
            r'[\s\S]*?<time[^>]*>([\s\S]*?)</time>'
            r'[\s\S]*?<h4>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
            r'[\s\S]*?</li>', re.I
        )
        for m in rgx.finditer(page_html):
            time_html, href, title_raw = m.group(1), m.group(2), m.group(3)
            title = title_raw.strip()[:100]
            dt = date_parser(time_html)
            if dt and dt < cutoff:
                continue
            url = href if href.startswith('http') else base_url + href.lstrip('./')
            date_str = dt.strftime('%Y-%m-%d') if dt else 'unknown'
            items.append({'title': title, 'date': date_str, 'url': url})
            if len(items) >= max_items:
                break

    elif name == 'dlut_sche':
        rgx = re.compile(
            r'<li[^>]*style[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>'
            r'\s*<span[^>]*class=["\'][^"\']*date[^"\']*["\'][^>]*>([^<]+)</span>', re.I
        )
        for m in rgx.finditer(page_html):
            href, title, date_raw = m.group(1), m.group(2).strip(), m.group(3).strip()
            dt = date_parser(date_raw)
            if dt and dt < cutoff:
                continue
            url = href if href.startswith('http') else base_url + href.lstrip('./')
            date_str = dt.strftime('%Y-%m-%d') if dt else date_raw
            items.append({'title': title, 'date': date_str, 'url': url})
            if len(items) >= max_items:
                break

    elif name == 'dlut_futureschool':
        rgx = re.compile(
            r'<li[^>]*>'
            r'[\s\S]*?<div[^>]*class=["\'][^"\']*time[^"\']*["\'][^>]*>([\s\S]*?)</div>'
            r'[\s\S]*?<a[^>]+href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*name[^"\']*["\'][^>]*>\s*([^<]+?)\s*</a>'
            r'[\s\S]*?</li>', re.I
        )
        for m in rgx.finditer(page_html):
            date_html_inner, href, title = m.group(1), m.group(2), m.group(3).strip()
            dt = date_parser(date_html_inner)
            if dt and dt < cutoff:
                continue
            url = href if href.startswith('http') else base_url + href.lstrip('./')
            date_str = dt.strftime('%Y-%m-%d') if dt else 'unknown'
            items.append({'title': title, 'date': date_str, 'url': url})
            if len(items) >= max_items:
                break

    elif name == 'dlut_scidep':
        rgx = re.compile(
            r'<li[^>]*>\s*<a[^>]+href=["\']([^"\']+)["\'][^>]*>'
            r'[\s\S]*?<div[^>]*class=["\'][^"\']*tz-ul-date[^"\']*["\'][^>]*>([\s\S]*?)</div>'
            r'[\s\S]*?<div[^>]*class=["\'][^"\']*tz-ul-tt[^"\']*["\'][^>]*>([^<]+)</div>'
            r'[\s\S]*?</a>\s*</li>', re.I
        )
        for m in rgx.finditer(page_html):
            href, date_html_inner, title = m.group(1), m.group(2), m.group(3).strip()
            dt = date_parser(date_html_inner)
            if dt and dt < cutoff:
                continue
            url = href if href.startswith('http') else base_url + href.lstrip('./')
            date_str = dt.strftime('%Y-%m-%d') if dt else 'unknown'
            items.append({'title': title, 'date': date_str, 'url': url})
            if len(items) >= max_items:
                break

    return items


def parse_api_response_v1(source, api_data):
    """Parse API response to extract items with date filtering."""
    max_items = source.get('max_items', 10)
    lookback_hours = source.get('lookback_hours', 48)
    extract_config = source.get('extract', {})
    fields = extract_config.get('fields', {})
    list_url = source.get('list_url', source.get('url', ''))
    cutoff = NOW - datetime.timedelta(hours=lookback_hours)

    items = []

    # Assume api_data is a dict with 'object' key containing list or similar
    data_list = []
    if isinstance(api_data, dict):
        if 'object' in api_data and isinstance(api_data['object'], dict):
            data_list = api_data['object'].get('list', [])
        elif 'list' in api_data:
            data_list = api_data['list']
        elif isinstance(api_data, list):
            data_list = api_data

    for item in data_list:
        if not isinstance(item, dict):
            continue

        # Extract fields as configured
        title = item.get(fields.get('title', 'title'), '').strip()
        date_str = item.get(fields.get('date', 'date'), '')

        if not title:
            continue

        # Parse date
        dt = None
        if isinstance(date_str, str) and date_str:
            # Try to parse datetime-like strings (e.g., "2026-04-15 10:17:09")
            for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d']:
                try:
                    dt = datetime.datetime.strptime(date_str[:19], fmt)
                    break
                except ValueError:
                    pass

        # Filter by date
        if dt and dt < cutoff:
            continue

        date_display = dt.strftime('%Y-%m-%d') if dt else date_str[:10] if date_str else 'unknown'
        items.append({'title': title[:100], 'date': date_display, 'url': list_url})

        if len(items) >= max_items:
            break

    return items


def run_pipeline_3():
    log('=== Pipeline 3: University News & Recruitment ===')
    with open(SOURCES_JSON) as f:
        sources_cfg = json.load(f)

    scraper_defaults = sources_cfg.get('defaults', {})
    prompt_templates = sources_cfg.get('prompt_templates', {})
    saved = 0

    for source in sources_cfg['sources']:
        if source.get('category') != 'resource' or source.get('enabled') is False:
            continue

        name = source['name']
        display_name = source['display_name']
        lookback_hours = source.get('lookback_hours', 48)
        source_type = source.get('type') or source.get('source_type', 'scrape')
        log(f'  {name}...')

        items = []
        try:
            if source_type == 'api':
                # Handle API-based sources (e.g., recruitment info)
                method = source.get('method', 'GET').upper()
                params = source.get('params', {})
                if method == 'POST':
                    resp = requests.post(source['url'], data=params, timeout=20)
                else:
                    resp = requests.get(source['url'], params=params, timeout=20)
                resp.raise_for_status()
                api_data = resp.json()
                items = parse_api_response_v1(source, api_data)
            else:
                # Handle HTML scraping sources
                resp = requests.get(
                    source['url'], timeout=20,
                    headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
                )
                resp.encoding = resp.apparent_encoding or 'utf-8'
                page_html = resp.text
                items = parse_html_v2(source, page_html)
        except Exception as e:
            log(f'    FETCH ERR: {e}')
            continue

        if not items:
            no_update_content = (
                f'# {display_name} - {DATE}\n\n'
                f'📭 过去 {lookback_hours} 小时无新内容\n\n'
                f'---\n*来源: {source["url"]}*\n'
            )
            save('resource', f'{name}_briefing_{DATE}.md', no_update_content)
            saved += 1
            log(f'    no updates in {lookback_hours}h -> saved placeholder')
            continue

        log(f'    {len(items)} items (within {lookback_hours}h)')

        items_text = '\n'.join(
            f'{i+1}. [{item["date"]}] {item["title"]}'
            for i, item in enumerate(items)
        )
        tmpl_key = source.get('prompt_template', 'university_news')
        prompt_tmpl = prompt_templates.get(tmpl_key) or prompt_templates.get('university_news', '')
        prompt = prompt_tmpl.replace('{items}', f'{display_name}\n{items_text}')

        try:
            content = call_ai(
                prompt,
                model=scraper_defaults.get('model', 'anthropic/claude-haiku-4.5'),
                max_tokens=800
            )
            # Use list_url if available (for API sources), otherwise use API/scrape URL
            display_url = source.get('list_url', source.get('url', ''))
            full_content = (
                f'# {display_name} - {DATE}\n\n'
                f'{content}\n\n'
                f'---\n*{len(items)} items (past {lookback_hours}h)*\n\n'
                f'📍 查看全部：{display_url}\n'
            )
            save('resource', f'{name}_briefing_{DATE}.md', full_content)
            saved += 1
            log(f'    -> saved {name}_briefing_{DATE}.md')
            time.sleep(1)
        except Exception as e:
            log(f'    AI ERR: {e}')

    log(f'  Pipeline 3 done: {saved} files saved')
    return saved


# =====================================================================
# Main
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description='DailyInfo Pipeline Runner')
    parser.add_argument('--pipeline', type=int, choices=[1, 2, 3],
                        help='Run specific pipeline (1=RSS, 2=Code, 3=University). Default: all')
    args = parser.parse_args()

    log(f'DailyInfo Pipeline Runner — {DATE}')
    log(f'Project root: {PROJECT_ROOT}')
    log(f'Briefings dir: {BRIEFINGS_DIR}')

    pipelines = {1: run_pipeline_1, 2: run_pipeline_2, 3: run_pipeline_3}
    to_run = [args.pipeline] if args.pipeline else [1, 2, 3]
    total_saved = 0

    for p in to_run:
        try:
            total_saved += pipelines[p]()
        except Exception as e:
            log(f'Pipeline {p} FAILED: {e}')
            import traceback
            traceback.print_exc()

    # Print summary
    log('=== Summary ===')
    for d in ['papers', 'ai_news', 'code', 'resource']:
        path = os.path.join(BRIEFINGS_DIR, d)
        if os.path.exists(path):
            files = [f for f in sorted(os.listdir(path)) if DATE in f]
            log(f'  {d}/: {len(files)} today — {", ".join(files)}')

    log(f'Total: {total_saved} files saved')
    return 0 if total_saved > 0 else 1


if __name__ == '__main__':
    API_KEY = load_api_key()
    sys.exit(main())
