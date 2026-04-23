#!/usr/bin/env python3
"""DailyInfo Pipeline Runner — generates daily briefing files.

Reads RSS feeds from FreshRSS, scrapes GitHub/HuggingFace trending,
scrapes DUT university news, then calls OpenRouter AI for summaries.
Output files are saved to ~/.myagentdata/dailyinfo/briefings/{category}/.

Usage:
    python3 scripts/run_pipelines.py              # run all 3 pipelines
    python3 scripts/run_pipelines.py --pipeline 1  # RSS papers + AI news only
    python3 scripts/run_pipelines.py --pipeline 2  # code trending only
    python3 scripts/run_pipelines.py --pipeline 3  # university news only
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

import requests

from datasource import DataSource, RSSDataSource, build_feed_url_map
from paths import BRIEFINGS_DIR, FRESHRSS_DATA, PUSHED_DIR

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
SOURCES_JSON = os.path.join(CONFIG_DIR, "sources.json")
DATE = datetime.datetime.now().strftime("%Y-%m-%d")

API_KEY = ""


def _get_freshrss_user() -> str:
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("FRESHRSS_USER="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    try:
        with open(SOURCES_JSON) as f:
            val = json.load(f).get("defaults", {}).get("freshrss_user", "")
            if val:
                return val
    except Exception:
        pass
    return os.environ.get("USER", "owen")


def _get_freshrss_db() -> str:
    user = _get_freshrss_user()
    path = str(FRESHRSS_DATA / "users" / user / "db.sqlite")
    if not os.path.exists(path):
        print(
            f"[WARN] FreshRSS DB not found: {path}\n"
            f"       Set FRESHRSS_USER in .env to match your FreshRSS username.",
            file=sys.stderr,
        )
    return path


FRESHRSS_DB = _get_freshrss_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def load_api_key() -> str:
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values

            key = dotenv_values(env_path).get("OPENROUTER_API_KEY", "")
            if key and not key.startswith("your_"):
                return key
        except ImportError:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY=") and "your_" not in line:
                        return line.split("=", 1)[1].strip()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    log("ERROR: No OPENROUTER_API_KEY found in .env or environment")
    sys.exit(1)


def call_ai(
    prompt: str, model: str = "moonshotai/kimi-k2.5", max_tokens: int = 1200
) -> str:
    for attempt in range(3):
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if content:
            return content
        log(f"  [call_ai] empty response, retry {attempt + 1}/3")
        time.sleep(2)
    raise ValueError("call_ai: empty response after 3 retries")


def save(directory: str, filename: str, content: str) -> str:
    path = BRIEFINGS_DIR / directory
    path.mkdir(parents=True, exist_ok=True)
    full = path / filename
    with open(full, "w") as f:
        f.write(content)
    return str(full)


def _already_pushed_within(name: str, category: str, lookback_hours: int) -> bool:
    """Return True if this source already produced a pushed briefing recently.

    Used to skip redundant AI calls on low-frequency (weekly/biweekly) sources
    where lookback_hours > 24 and we don't want to regenerate the same window.
    """
    pushed_dir = PUSHED_DIR / category
    if not pushed_dir.is_dir():
        return False
    cutoff = time.time() - lookback_hours * 3600
    prefix = f"{name}_briefing_"
    for fpath in pushed_dir.iterdir():
        if fpath.name.startswith(prefix) and fpath.name.endswith(".md"):
            if fpath.stat().st_mtime > cutoff:
                return True
    return False


def _load_sources() -> tuple[dict, dict, dict]:
    with open(SOURCES_JSON) as f:
        cfg = json.load(f)
    return cfg, cfg.get("defaults", {}), cfg.get("prompt_templates", {})


# =====================================================================
# PIPELINE 1: papers + AI news via FreshRSS
# =====================================================================
def run_pipeline_1() -> int:
    log("=== Pipeline 1: Daily Briefing (papers + AI news) ===")
    cfg, defaults, templates = _load_sources()
    default_tmpl_key = defaults.get("prompt_template", "one_line_summary")
    model_default = defaults.get("model", "moonshotai/kimi-k2.5")

    try:
        db = sqlite3.connect(FRESHRSS_DB)
    except Exception as e:
        user = _get_freshrss_user()
        log(f"Pipeline 1 FAILED: cannot open FreshRSS DB ({e})")
        log(f"  DB path: {FRESHRSS_DB}")
        log(f"  Fix: set FRESHRSS_USER={user} in .env, or correct the username.")
        return 0
    db.row_factory = sqlite3.Row
    full_map, base_map = build_feed_url_map(db)
    saved = 0

    for feed_cfg in cfg["sources"]:
        if feed_cfg.get("type") != "rss" or not feed_cfg.get("enabled", True):
            continue

        ds = DataSource.create(
            feed_cfg, defaults, db=db, full_map=full_map, base_map=base_map
        )
        assert isinstance(ds, RSSDataSource)
        items = ds.fetch()
        name, category = ds.name, ds.category
        model = feed_cfg.get("model") or model_default

        if not items:
            log(f"  {name}: 0 articles — placeholder")
            placeholder = f"# {ds.display_name} - {DATE}\n\n📭 过去 {ds.lookback_hours} 小时无新内容\n"
            save(category, f"{name}_briefing_{DATE}.md", placeholder)
            saved += 1
            continue

        if ds.lookback_hours > 24 and _already_pushed_within(
            name, category, ds.lookback_hours
        ):
            log(
                f"  {name}: {len(items)} articles — already pushed within {ds.lookback_hours}h, skip"
            )
            continue

        # SmolAI deep-content path (one AI call per article)
        if feed_cfg.get("use_content"):
            log(f"  {name}: {len(items)} articles (deep content)")
            tmpl_key = feed_cfg.get("prompt_template", "smolai_categorized")
            tmpl = templates.get(tmpl_key, "")
            for idx, item in enumerate(items):
                prompt = (
                    tmpl.replace("{content}", item.content).replace("{date}", DATE)
                    if tmpl
                    else f"Summarize the following AI news in Chinese by category:\n\n{item.content}"
                )
                suffix = f"_part{idx+1}" if len(items) > 1 else ""
                filename = f"{name}_briefing_{DATE}{suffix}.md"
                try:
                    content_text = call_ai(prompt, model=model, max_tokens=2000)
                    save(
                        category,
                        filename,
                        f"# AI Daily Digest - {DATE}\n\n{content_text}",
                    )
                    saved += 1
                    log(f"    -> saved {filename}")
                    time.sleep(1)
                except Exception as e:
                    log(f"    ERR: {e}")
            continue

        # Regular path — batch by max_articles_per_batch
        log(f"  {name}: {len(items)} articles")
        tmpl_key = feed_cfg.get("prompt_template") or default_tmpl_key
        prompt_template = templates.get(tmpl_key) or templates.get(
            "one_line_summary", ""
        )
        if not prompt_template:
            log(f"  SKIP {name}: no prompt template")
            continue

        batches = ds.get_batches(items)
        for idx, batch in enumerate(batches):
            article_list = ds.format_items(batch)
            prompt = (
                prompt_template.replace("{count}", str(len(batch)))
                .replace("{display_name}", ds.display_name)
                .replace("{article_list}", article_list)
                .replace("{date}", DATE)
            )
            suffix = f"_batch{idx+1}" if feed_cfg.get("max_articles_per_batch") else ""
            filename = f"{name}_briefing_{DATE}{suffix}.md"
            try:
                content_text = call_ai(prompt, model=model)
                save(category, filename, content_text)
                saved += 1
                log(f"    -> saved {filename}")
                time.sleep(0.5)
            except Exception as e:
                log(f"    ERR: {e}")

    db.close()
    log(f"  Pipeline 1 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 2: Code Trending (GitHub + HuggingFace)
# =====================================================================
def run_pipeline_2() -> int:
    log("=== Pipeline 2: Code Trending ===")
    cfg, defaults, templates = _load_sources()
    model_default = defaults.get("model", "moonshotai/kimi-k2.5")
    code_tmpl = templates.get("code_trending", "")
    saved = 0

    for source_cfg in cfg["sources"]:
        if source_cfg.get("category") != "code" or source_cfg.get("enabled") is False:
            continue

        ds = DataSource.create(source_cfg, defaults)
        log(f"  {ds.name}...")

        try:
            items = ds.fetch()
        except Exception as e:
            log(f"    FETCH ERR: {e}")
            continue

        if not items:
            log(f"    no items")
            continue

        log(f"    {len(items)} items")
        items_list = ds.format_items(items)

        tmpl_key = source_cfg.get("prompt_template") or "code_trending"
        prompt_tmpl = templates.get(tmpl_key) or code_tmpl
        if prompt_tmpl:
            prompt = (
                prompt_tmpl.replace("{items}", items_list)
                .replace("{display_name}", ds.display_name)
                .replace("{date}", DATE)
            )
        else:
            prompt = (
                f"请为以下 {ds.display_name} 的每一条目写一行中文简介，突出核心功能或技术亮点。\n\n"
                f"{items_list}\n\n"
                f"输出要求（严格遵守）：\n"
                f"- 直接输出列表，不要任何前言、说明或总结\n"
                f"- 每行格式：序号. **项目名** - 一句中文描述\n"
                f"- 保持原始序号和项目名称不变\n"
                f"- 每条目必须输出，不能跳过或合并\n"
                f"- 全部使用中文，不得使用英文解释"
            )
        try:
            content_text = call_ai(
                prompt, model=source_cfg.get("model", model_default), max_tokens=2500
            )
            save(
                "code",
                f"{ds.name}_briefing_{DATE}.md",
                f"# {ds.display_name} - {DATE}\n\n{content_text}",
            )
            saved += 1
            log(f"    -> saved {ds.name}_briefing_{DATE}.md")
            time.sleep(1)
        except Exception as e:
            log(f"    AI ERR: {e}")

    log(f"  Pipeline 2 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 3: University News (DLUT HTML + API)
# =====================================================================
def run_pipeline_3() -> int:
    log("=== Pipeline 3: University News & Recruitment ===")
    cfg, defaults, prompt_templates = _load_sources()
    model_default = defaults.get("model", "moonshotai/kimi-k2.5")
    saved = 0

    for source_cfg in cfg["sources"]:
        if (
            source_cfg.get("category") != "resource"
            or source_cfg.get("enabled") is False
        ):
            continue

        ds = DataSource.create(source_cfg, defaults)
        log(f"  {ds.name}...")

        try:
            items = ds.fetch()
        except Exception as e:
            log(f"    FETCH ERR: {e}")
            continue

        if not items:
            no_update = (
                f"# {ds.display_name} - {DATE}\n\n"
                f"📭 过去 {ds.lookback_hours} 小时无新内容\n\n"
                f'---\n*来源: {source_cfg["url"]}*\n'
            )
            save("resource", f"{ds.name}_briefing_{DATE}.md", no_update)
            saved += 1
            log(f"    no updates -> placeholder")
            continue

        log(f"    {len(items)} items (within {ds.lookback_hours}h)")
        items_text = ds.format_items(items)
        tmpl_key = source_cfg.get("prompt_template", "university_news")
        prompt_tmpl = prompt_templates.get(tmpl_key) or prompt_templates.get(
            "university_news", ""
        )
        prompt = prompt_tmpl.replace("{items}", f"{ds.display_name}\n{items_text}")

        try:
            content_text = call_ai(prompt, model=model_default, max_tokens=1200)
            display_url = source_cfg.get("list_url", source_cfg.get("url", ""))
            full_content = (
                f"# {ds.display_name} - {DATE}\n\n"
                f"{content_text}\n\n"
                f"---\n*{len(items)} items (past {ds.lookback_hours}h)*\n\n"
                f"📍 查看全部：{display_url}\n"
            )
            save("resource", f"{ds.name}_briefing_{DATE}.md", full_content)
            saved += 1
            log(f"    -> saved {ds.name}_briefing_{DATE}.md")
            time.sleep(1)
        except Exception as e:
            log(f"    AI ERR: {e}")

    log(f"  Pipeline 3 done: {saved} files saved")
    return saved


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="DailyInfo Pipeline Runner")
    parser.add_argument(
        "--pipeline",
        type=int,
        choices=[1, 2, 3],
        help="Run specific pipeline (1=RSS, 2=Code, 3=University). Default: all",
    )
    args = parser.parse_args()

    global API_KEY
    API_KEY = load_api_key()

    log(f"DailyInfo Pipeline Runner — {DATE}")
    log(f"Project root: {PROJECT_ROOT}")
    log(f"Briefings dir: {BRIEFINGS_DIR}")

    pipelines = {1: run_pipeline_1, 2: run_pipeline_2, 3: run_pipeline_3}
    to_run = [args.pipeline] if args.pipeline else [1, 2, 3]
    total_saved = 0

    for p in to_run:
        try:
            total_saved += pipelines[p]()
        except Exception as e:
            log(f"Pipeline {p} FAILED: {e}")
            import traceback

            traceback.print_exc()

    log("=== Summary ===")
    for d in ["papers", "ai_news", "code", "resource"]:
        path = BRIEFINGS_DIR / d
        if path.exists():
            files = [f.name for f in sorted(path.iterdir()) if DATE in f.name]
            log(f'  {d}/: {len(files)} today — {", ".join(files)}')

    log(f"Total: {total_saved} files saved")
    return 0 if total_saved > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
