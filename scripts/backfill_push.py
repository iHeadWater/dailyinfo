#!/usr/bin/env python3
"""一次性补推脚本 — 把过去未推送的期刊内容补推到 Discord。

用法:
    python3 scripts/backfill_push.py
    python3 scripts/backfill_push.py --dry-run   # 只生成 briefing 不推送
    python3 scripts/backfill_push.py --since 2026-04-01  # 指定起始日期
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

from datasource import build_feed_url_map, resolve_feed_id, strip_html
from paths import BRIEFINGS_DIR, PUSHED_DIR

SOURCES_JSON = os.path.join(PROJECT_ROOT, 'config', 'sources.json')
DISCORD_CHANNEL_ID = '1489102139597787181'

BACKFILL_TARGETS = {
    'science':           {'max_articles': 20, 'batch_label': '周刊精选'},
    'science_advances':  {'max_articles': 20, 'batch_label': '双周精选'},
    'pnas':              {'max_articles': 20, 'batch_label': '近期精选'},
    'grl':               {'max_articles': 20, 'batch_label': '近期精选'},
    'earths_future':     {'max_articles': 15, 'batch_label': '近期精选'},
    'james':             {'max_articles': 15, 'batch_label': '近期精选'},
    'earth_space_science': {'max_articles': 15, 'batch_label': '近期精选'},
    'reviews_geophysics': {'max_articles': 10, 'batch_label': '近期精选'},
}


def log(msg):
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def load_env(key):
    env_path = os.path.join(PROJECT_ROOT, '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f'{key}='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    return os.environ.get(key, '')


def call_ai(prompt, api_key, model='anthropic/claude-haiku-4-5', max_tokens=1500):
    import requests
    resp = requests.post(
        'https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']


def discord_send(token, channel_id, content, dry_run=False):
    if dry_run:
        print(f'  [DRY-RUN] would send {len(content)} chars to channel {channel_id}')
        return True
    chunks = []
    lines = content.split('\n')
    current = ''
    for line in lines:
        if len(current) + len(line) + 1 > 1950:
            chunks.append(current)
            current = line
        else:
            current = (current + '\n' + line) if current else line
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        data = json.dumps({'content': chunk}).encode('utf-8')
        req = urllib.request.Request(
            f'https://discord.com/api/v10/channels/{channel_id}/messages',
            data=data,
            headers={
                'Authorization': f'Bot {token}',
                'Content-Type': 'application/json',
                'User-Agent': 'DiscordBot (https://github.com/dailyinfo, 1.0)',
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status not in (200, 201):
                    log(f'  Discord error {resp.status} on chunk {i+1}')
                    return False
        except Exception as e:
            log(f'  Discord send failed: {e}')
            return False
        if i < len(chunks) - 1:
            time.sleep(1)
    return True


def archive(name, date_str, content):
    os.makedirs(PUSHED_DIR / 'papers', exist_ok=True)
    path = PUSHED_DIR / 'papers' / f'{name}_backfill_{date_str}.md'
    with open(path, 'w') as f:
        f.write(content)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--since', default='2026-03-22',
                        help='Backfill articles since this date (YYYY-MM-DD)')
    parser.add_argument('--targets', nargs='+', choices=list(BACKFILL_TARGETS.keys()),
                        help='Only backfill specific journals')
    args = parser.parse_args()

    api_key = load_env('OPENROUTER_API_KEY')
    if not api_key or api_key.startswith('your_'):
        log('ERROR: OPENROUTER_API_KEY not set in .env')
        sys.exit(1)

    discord_token = load_env('DISCORD_BOT_TOKEN')
    if not discord_token or discord_token.startswith('your_'):
        log('ERROR: DISCORD_BOT_TOKEN not set in .env')
        sys.exit(1)

    freshrss_user = load_env('FRESHRSS_USER') or os.environ.get('USER', 'owen')
    db_path = os.path.expanduser(f'~/.freshrss/data/users/{freshrss_user}/db.sqlite')
    if not os.path.exists(db_path):
        log(f'ERROR: FreshRSS DB not found: {db_path}')
        log(f'       Set FRESHRSS_USER in .env')
        sys.exit(1)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    full_map, base_map = build_feed_url_map(db)

    with open(SOURCES_JSON) as f:
        cfg = json.load(f)

    sources_by_name = {s['name']: s for s in cfg['sources'] if s.get('type') == 'rss'}
    templates = cfg.get('prompt_templates', {})
    one_line_tmpl = templates.get('one_line_summary', '')

    since_ts = datetime.datetime.strptime(args.since, '%Y-%m-%d').timestamp()
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    targets = args.targets or list(BACKFILL_TARGETS.keys())
    log(f'Backfill since {args.since}, targets: {", ".join(targets)}')
    if args.dry_run:
        log('[DRY-RUN mode — no actual Discord messages will be sent]')

    for name in targets:
        cfg_entry = sources_by_name.get(name)
        if not cfg_entry:
            log(f'  {name}: not found in sources.json, skip')
            continue

        feed_id = resolve_feed_id(cfg_entry.get('url', ''), full_map, base_map)
        if not feed_id:
            log(f'  {name}: cannot resolve feed_id from URL, skip')
            continue

        max_articles = BACKFILL_TARGETS[name]['max_articles']
        rows = db.execute(
            'SELECT title, link, date FROM entry WHERE id_feed=? AND date>? ORDER BY date DESC LIMIT ?',
            [feed_id, since_ts, max_articles]
        ).fetchall()

        if not rows:
            log(f'  {name}: no articles since {args.since}, skip')
            continue

        display_name = cfg_entry.get('display_name', name)
        date_range_start = datetime.datetime.fromtimestamp(rows[-1]['date']).strftime('%Y-%m-%d')
        date_range_end = datetime.datetime.fromtimestamp(rows[0]['date']).strftime('%Y-%m-%d')
        label = BACKFILL_TARGETS[name]['batch_label']

        log(f'  {name}: {len(rows)} articles ({date_range_start} ~ {date_range_end}), generating briefing...')

        article_list = '\n'.join(
            f'{i+1}. {r["title"]}'
            for i, r in enumerate(rows)
        )

        prompt = (
            one_line_tmpl
            .replace('{count}', str(len(rows)))
            .replace('{display_name}', f'{display_name}（{label}：{date_range_start} ~ {date_range_end}）')
            .replace('{article_list}', article_list)
            .replace('{date}', today)
        )

        try:
            briefing = call_ai(prompt, api_key)
        except Exception as e:
            log(f'  {name}: AI call failed: {e}')
            continue

        header = f'> 📬 **补推** | {display_name} {label}（{date_range_start} ~ {date_range_end}）\n\n'
        full_content = header + briefing

        ok = discord_send(discord_token, DISCORD_CHANNEL_ID, full_content, dry_run=args.dry_run)
        if ok:
            path = archive(name, today, full_content)
            log(f'    -> sent & archived: {os.path.basename(path)}')
        else:
            log(f'    -> Discord send failed, briefing NOT archived')

        time.sleep(2)

    db.close()
    log('Backfill done.')


if __name__ == '__main__':
    main()
