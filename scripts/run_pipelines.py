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
import re
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


DEFAULT_FALLBACK_MODEL = "deepseek/deepseek-chat-v3.1"

_BACKOFF_SECONDS = (2, 5, 10)


class BriefingGenerationError(ValueError):
    """Raised when an AI response is empty, truncated, or structurally incomplete."""


def _resolve_fallback_model(explicit: str | None) -> str:
    """Pick the fallback model: explicit arg > env override > built-in default."""
    return (
        explicit or os.environ.get("DAILYINFO_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    )


def _post_openrouter(model: str, prompt: str, max_tokens: int):
    """Issue a single OpenRouter chat completion call and return the parsed JSON."""
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
    return resp.json()


def call_ai(
    prompt: str,
    model: str = "moonshotai/kimi-k2.5",
    max_tokens: int = 1200,
    *,
    fallback_model: str | None = None,
) -> str:
    """Call OpenRouter with retries and a fallback model.

    Strategy: 3 attempts on the primary model with exponential backoff
    (2s / 5s / 10s), then up to 2 attempts on ``fallback_model``.
    Empty or refusal responses are logged with the provider-reported
    ``finish_reason`` to help diagnose truncation vs. content filtering.
    """
    fallback = _resolve_fallback_model(fallback_model)
    attempts_per_model = ((model, 3), (fallback, 2))

    for mdl, attempts in attempts_per_model:
        for i in range(attempts):
            try:
                data = _post_openrouter(mdl, prompt, max_tokens)
            except requests.RequestException as exc:
                log(f"  [call_ai] {mdl} attempt {i + 1}/{attempts} http_error={exc}")
                time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])
                continue

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or "unknown"
            content = (message.get("content") or "").strip()
            if content and finish_reason != "length":
                return content

            reason = (
                finish_reason or (data.get("error") or {}).get("message") or "empty"
            )
            log(
                f"  [call_ai] {mdl} attempt {i + 1}/{attempts} incomplete "
                f"(finish_reason={reason}, chars={len(content)})"
            )
            time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])

        if mdl != fallback:
            log(
                f"  [call_ai] primary {model} exhausted, switching to fallback {fallback}"
            )

    raise BriefingGenerationError(
        f"call_ai: empty response after retries (model={model}, fallback={fallback})"
    )


def _count_numbered_items(content: str) -> int:
    """Count markdown numbered list entries in a model-generated briefing."""
    return len(re.findall(r"(?m)^\s*\d+\.\s+\*\*", content))


def _looks_cut_off(content: str) -> bool:
    """Return True for common half-written markdown or sentence endings."""
    stripped = content.strip()
    if not stripped:
        return True
    if re.search(r"\*\*[^*\n]{1,160}$", stripped):
        return True
    if stripped.endswith(("**", "*", "`", "：", ":", "，", ",")):
        return True
    if "Today's Highlight" in stripped and stripped[-1] not in "。！？.!?)）】”’":
        return True
    return False


def validate_briefing_content(content: str, expected_count: int) -> None:
    """Validate that a regular RSS briefing appears complete before saving."""
    if not content.strip():
        raise BriefingGenerationError("empty briefing")
    actual_count = _count_numbered_items(content)
    if expected_count > 1 and actual_count < expected_count:
        raise BriefingGenerationError(
            f"incomplete briefing: expected {expected_count} items, got {actual_count}"
        )
    if _looks_cut_off(content):
        raise BriefingGenerationError("briefing appears cut off")


def _build_regular_prompt(prompt_template: str, ds: RSSDataSource, batch: list) -> str:
    article_list = ds.format_items(batch)
    return (
        prompt_template.replace("{count}", str(len(batch)))
        .replace("{display_name}", ds.display_name)
        .replace("{article_list}", article_list)
        .replace("{date}", DATE)
    )


def _generate_regular_briefings(
    ds: RSSDataSource,
    batch: list,
    prompt_template: str,
    model: str,
    *,
    max_tokens: int = 2500,
) -> list[str]:
    """Generate one or more complete briefings, splitting oversized batches."""
    prompt = _build_regular_prompt(prompt_template, ds, batch)
    try:
        content = call_ai(prompt, model=model, max_tokens=max_tokens)
        validate_briefing_content(content, len(batch))
        log(
            f"    AI ok: source={ds.name}, articles={len(batch)}, "
            f"prompt_chars={len(prompt)}, response_chars={len(content)}"
        )
        return [content]
    except BriefingGenerationError as exc:
        if len(batch) <= 1:
            raise
        midpoint = max(1, len(batch) // 2)
        log(
            f"    AI incomplete for {ds.name} ({len(batch)} articles): {exc}; "
            f"splitting into {midpoint}+{len(batch) - midpoint}"
        )
        return _generate_regular_briefings(
            ds, batch[:midpoint], prompt_template, model, max_tokens=max_tokens
        ) + _generate_regular_briefings(
            ds, batch[midpoint:], prompt_template, model, max_tokens=max_tokens
        )


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


# --- Idempotency / --force state -----------------------------------------
# Populated from CLI ``--force`` flags in ``main``. When ``FORCE_ALL`` is True
# every source is re-run; names in ``FORCE_SOURCES`` are selectively re-run.
FORCE_ALL: bool = False
FORCE_SOURCES: set[str] = set()


def _is_forced(name: str) -> bool:
    """True when the caller explicitly requested a re-run for this source."""
    return FORCE_ALL or name in FORCE_SOURCES


def _has_real_briefing_today(name: str, category: str) -> bool:
    """Return True when a non-placeholder briefing for ``name`` already exists today.

    Used to skip redundant fetch+AI work when a pipeline is re-run on the same
    day. Scans both ``BRIEFINGS_DIR`` (generated but not yet pushed) and
    ``PUSHED_DIR`` (already pushed and archived today) so the check holds
    across the full lifecycle. Placeholder files ("no new content" notices)
    do not count as real briefings so they can be regenerated if fresh items
    arrive later. ``--force`` (``FORCE_ALL`` / ``FORCE_SOURCES``) overrides
    this check.
    """
    if _is_forced(name):
        return False
    for base in (BRIEFINGS_DIR, PUSHED_DIR):
        cat_dir = base / category
        if not cat_dir.is_dir():
            continue
        for fpath in cat_dir.glob(f"{name}_briefing_{DATE}*.md"):
            try:
                text = fpath.read_text(encoding="utf-8")
            except Exception:
                continue
            if "📭 过去" not in text:
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
        name, category = ds.name, ds.category
        if _has_real_briefing_today(name, category):
            log(
                f"  {name}: briefing already exists for {DATE}, skip "
                f"(use --force {name} to regenerate)"
            )
            continue
        items = ds.fetch()
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

        generated_parts: list[str] = []
        batches = ds.get_batches(items)
        for idx, batch in enumerate(batches):
            try:
                generated_parts.extend(
                    _generate_regular_briefings(ds, batch, prompt_template, model)
                )
            except Exception as e:
                log(f"    ERR batch {idx + 1}: {e}")

        should_suffix = (
            bool(feed_cfg.get("max_articles_per_batch")) or len(generated_parts) > 1
        )
        for part_idx, content_text in enumerate(generated_parts, start=1):
            suffix = f"_batch{part_idx}" if should_suffix else ""
            filename = f"{name}_briefing_{DATE}{suffix}.md"
            try:
                save(category, filename, content_text)
                saved += 1
                log(f"    -> saved {filename}")
                time.sleep(0.5)
            except Exception as e:
                log(f"    SAVE ERR: {e}")

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

        if _has_real_briefing_today(ds.name, "code"):
            log(
                f"    briefing already exists for {DATE}, skip "
                f"(use --force {ds.name} to regenerate)"
            )
            continue

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

        if _has_real_briefing_today(ds.name, "resource"):
            log(
                f"    briefing already exists for {DATE}, skip "
                f"(use --force {ds.name} to regenerate)"
            )
            continue

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
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="SOURCE",
        help="Force regenerate. Pass 'all' to refresh everything or a source "
        "name to target one source. Repeatable.",
    )
    args = parser.parse_args()

    global API_KEY, FORCE_ALL, FORCE_SOURCES
    API_KEY = load_api_key()
    FORCE_ALL = "all" in args.force
    FORCE_SOURCES = set(args.force) - {"all"}
    if FORCE_ALL or FORCE_SOURCES:
        log(
            "Force mode: "
            + ("ALL" if FORCE_ALL else "")
            + (f" sources={sorted(FORCE_SOURCES)}" if FORCE_SOURCES else "")
        )

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
