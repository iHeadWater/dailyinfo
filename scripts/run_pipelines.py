#!/usr/bin/env python3
"""DailyInfo Pipeline Runner — generates daily briefing files.

Reads RSS feeds from FreshRSS, scrapes GitHub/HuggingFace trending,
scrapes DUT university news, then calls DeepSeek AI for summaries (Zhipu GLM fallback).
Output files are saved to ~/.myagentdata/dailyinfo/briefings/{category}/.

Usage:
    python3 scripts/run_pipelines.py              # run all 3 pipelines
    python3 scripts/run_pipelines.py --pipeline 1  # RSS papers + AI news only
    python3 scripts/run_pipelines.py --pipeline 2  # code trending only
    python3 scripts/run_pipelines.py --pipeline 3  # university news only
"""

import argparse
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
import re
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

import requests

from datasource import DataSource, RSSDataSource, build_feed_url_map
from logsafe import http_error_detail, one_line
from paths import BRIEFINGS_DIR, ENV_FILE, FRESHRSS_DATA, PUSHED_DIR, STATE_DIR
from publication import (
    DELIVERY_SINKS,
    DeliveryStateStore,
    PublicationBriefingInput,
    PublicationBundle,
    PublicationFinalizer,
    PublicationStore,
    merge_bundle,
)
from publication.pipeline import (
    PublicationRunCollector,
    StructuredResultError,
    now_utc,
    results_from_response,
    source_ref,
    structured_entries,
    structured_prompt,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
SOURCES_JSON = os.path.join(CONFIG_DIR, "sources.json")
CONTENT_TIMEZONE = ZoneInfo("Asia/Shanghai")
DATE = datetime.datetime.now(CONTENT_TIMEZONE).strftime("%Y-%m-%d")


def _get_freshrss_user() -> str:
    # Via paths.ENV_FILE, not PROJECT_ROOT: this runs at import time (see
    # FRESHRESS_DB below), it is the one remaining route by which a test run
    # opened the developer's real .env, and paths.ENV_FILE is the seam that
    # DAILYINFO_ENV_FILE redirects.
    env_path = str(ENV_FILE)
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
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


_ARXIV_MARKER_NAME = ".arxiv_generating"


def _create_arxiv_marker() -> None:
    """Signal that arXiv generation is in progress so push_to_discord waits."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / _ARXIV_MARKER_NAME).write_text(DATE, encoding="utf-8")
    log("  [arxiv] marker created - push will wait for generation to finish")


def _remove_arxiv_marker() -> None:
    """Remove the arXiv generation marker so push_to_discord can proceed."""
    try:
        (STATE_DIR / _ARXIV_MARKER_NAME).unlink(missing_ok=True)
    except OSError:
        pass
    log("  [arxiv] marker removed - push may proceed")


def load_glm_key() -> str:
    """Load the Zhipu GLM fallback key, or ``""`` when it is not configured.

    Mirrors :func:`load_deepseek_key` but must not exit: the fallback is
    optional, so a missing key only disables it.
    """
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values

            key = dotenv_values(env_path).get("GLM_API_KEY", "")
            if key and "your_" not in key:
                return key
        except ImportError:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("GLM_API_KEY="):
                        continue
                    # Test the value, not the line: a real key may be trailed
                    # by a comment that happens to mention "your_".
                    value = line.split("=", 1)[1].split("#", 1)[0].strip()
                    if value and "your_" not in value:
                        return value
    key = os.environ.get("GLM_API_KEY", "")
    if key and "your_" not in key:
        return key
    return ""


def load_deepseek_key() -> str:
    """Load the primary DeepSeek API key."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        try:
            from dotenv import dotenv_values

            key = dotenv_values(env_path).get("DEEPSEEK_API_KEY", "")
            if key and "your_" not in key:
                return key
        except ImportError:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("DEEPSEEK_API_KEY="):
                        continue
                    # Test the value, not the line: a real key may be trailed
                    # by a comment that happens to mention "your_".
                    value = line.split("=", 1)[1].split("#", 1)[0].strip()
                    if value and "your_" not in value:
                        return value
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key and "your_" not in key:
        return key
    log("ERROR: No DEEPSEEK_API_KEY found in .env or environment")
    sys.exit(1)


DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
GLM_API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

DEFAULT_FALLBACK_MODEL = "glm-5.3-flash"

# GLM-5.3-flash always thinks, and thinking tokens are billed against
# ``max_tokens`` — at the default effort it can spend the whole budget before
# emitting any content. "low" is the cheapest level it accepts (low/high/max).
GLM_REASONING_LOW = {"reasoning_effort": "low"}

# Deep-content sources feed whole articles to a reasoning model whose
# reasoning is billed against max_tokens -- a full AINews post measured
# ~8.5k tokens before its first output token. Live-verified with a
# production-size prompt against both the primary and the GLM fallback.
_DEEP_CONTENT_MAX_TOKENS = 64000

_BACKOFF_SECONDS = (2, 5, 10)

# The required structured response can exceed the old 1200-token ceiling.
# ``max_tokens`` is the completion budget (not the input prompt length) for
# this OpenAI-compatible API: the canonical publication paths use this value,
# while the older pipeline paths keep their own tuned per-pipeline budgets.
DEFAULT_AI_OUTPUT_TOKENS = 4000


class BriefingGenerationError(ValueError):
    """Raised when an AI response is empty, truncated, or structurally incomplete."""


def _read_dotenv_value(name: str) -> str:
    """Read one plain setting from ``.env``, or ``""`` when it is absent.

    `.env.example` documents ``DAILYINFO_FALLBACK_MODEL`` as a .env entry, but
    nothing injects .env into the environment -- reading ``os.environ`` alone
    silently ignores what an operator configured there.
    """
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return ""
    try:
        from dotenv import dotenv_values

        return dotenv_values(env_path).get(name, "") or ""
    except ImportError:
        prefix = f"{name}="
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line.startswith(prefix):
                    continue
                value = line[len(prefix) :].split("#", 1)[0].strip()
                return value.strip('"').strip("'")
    return ""


def _resolve_fallback_model(explicit: str | None) -> str:
    """Pick the fallback model: explicit arg > env > .env > built-in default."""
    return (
        explicit
        or os.environ.get("DAILYINFO_FALLBACK_MODEL")
        or _read_dotenv_value("DAILYINFO_FALLBACK_MODEL")
        or DEFAULT_FALLBACK_MODEL
    )


def _warn_if_fallback_looks_like_openrouter(model: str) -> None:
    """Warn when the fallback model id looks like a leftover OpenRouter id.

    The value now has to be a Zhipu model name. An old ``moonshotai/...``
    left in ``.env`` would 400 on every attempt and leave only
    "empty response after retries" behind, naming the primary model --
    which is the wrong place to go looking.
    """
    if "/" in model:
        log(
            f"[WARN] DAILYINFO_FALLBACK_MODEL 形似 OpenRouter id（{model}）；"
            "智谱官方端点会拒绝，请改用智谱模型名"
        )


def _post_ai(
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    *,
    extra_body: dict | None = None,
):
    """Issue a single AI chat completion call and return the parsed JSON."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if extra_body:
        payload.update(extra_body)
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _get_deepseek_key() -> str:
    """Load and cache the primary DeepSeek API key (exits if missing)."""
    global _DEEPSEEK_KEY_CACHE
    if _DEEPSEEK_KEY_CACHE is None:
        _DEEPSEEK_KEY_CACHE = load_deepseek_key()
    return _DEEPSEEK_KEY_CACHE


_DEEPSEEK_KEY_CACHE: str | None = None


def _get_glm_key() -> str:
    """Load and cache the Zhipu fallback key (empty string when unset)."""
    global _GLM_KEY_CACHE
    if _GLM_KEY_CACHE is None:
        _GLM_KEY_CACHE = load_glm_key()
    return _GLM_KEY_CACHE


_GLM_KEY_CACHE: str | None = None


def call_ai(
    prompt: str,
    model: str = "deepseek-flash",
    max_tokens: int = 1200,
    *,
    fallback_model: str | None = None,
) -> str:
    """Call DeepSeek API with retries, falling back to Zhipu GLM.

    Strategy: 3 attempts on the primary model via DeepSeek API with
    exponential backoff (2s / 5s / 10s), then up to 2 attempts on
    ``fallback_model`` via the Zhipu official API.
    """
    fallback = _resolve_fallback_model(fallback_model)
    ds_key = _get_deepseek_key()
    glm_key = _get_glm_key()

    # ── Primary: DeepSeek API ──────────────────────────────────────
    for i in range(3):
        try:
            data = _post_ai(DEEPSEEK_API_URL, ds_key, model, prompt, max_tokens)
        except requests.RequestException as exc:
            log(
                f"  [call_ai] {model} attempt {i + 1}/3 "
                f"http_error={http_error_detail(exc, ds_key)}"
            )
            time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])
            continue

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "unknown"
        content = (message.get("content") or "").strip()
        if content and finish_reason != "length":
            return content

        # Read finish_reason raw: the coerced value above is already truthy, so
        # using it here would make the provider's own error message
        # unreachable. Both sides are coerced because a provider can return an
        # int or a non-dict error, and this string ends up in a log line.
        error = data.get("error")
        error_message = error.get("message") if isinstance(error, dict) else error
        reason = str(choice.get("finish_reason") or error_message or "unknown")
        log(
            f"  [call_ai] {model} attempt {i + 1}/3 incomplete "
            f"(finish_reason={one_line(reason, ds_key)}, chars={len(content)})"
        )
        time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])

    if not glm_key:
        raise BriefingGenerationError(
            f"call_ai: primary {model} exhausted and fallback disabled "
            "(GLM_API_KEY not configured)"
        )

    log(f"  [call_ai] primary {model} exhausted, switching to fallback {fallback}")

    # ── Fallback: Zhipu GLM ─────────────────────────────────────────
    for i in range(2):
        try:
            data = _post_ai(
                GLM_API_URL,
                glm_key,
                fallback,
                prompt,
                max_tokens,
                extra_body=GLM_REASONING_LOW,
            )
        except requests.RequestException as exc:
            log(
                f"  [call_ai] {fallback} attempt {i + 1}/2 "
                f"http_error={http_error_detail(exc, glm_key)}"
            )
            time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])
            continue

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "unknown"
        content = (message.get("content") or "").strip()
        if content and finish_reason != "length":
            return content

        # Read finish_reason raw: the coerced value above is already truthy, so
        # using it here would make the provider's own error message
        # unreachable. Both sides are coerced because a provider can return an
        # int or a non-dict error, and this string ends up in a log line.
        error = data.get("error")
        error_message = error.get("message") if isinstance(error, dict) else error
        reason = str(choice.get("finish_reason") or error_message or "unknown")
        log(
            f"  [call_ai] {fallback} attempt {i + 1}/2 incomplete "
            f"(finish_reason={one_line(reason, glm_key)}, chars={len(content)})"
        )
        time.sleep(_BACKOFF_SECONDS[min(i, len(_BACKOFF_SECONDS) - 1)])

    raise BriefingGenerationError(
        f"call_ai: empty response after retries (model={model}, fallback={fallback})"
    )


def _count_numbered_items(content: str) -> int:
    """Count markdown numbered list entries in a model-generated briefing."""
    return len(re.findall(r"(?m)^\s*\d+\.\s+\*\*", content))


def _normalise_title(text: str) -> str:
    """Normalise article titles for tolerant generated-output matching."""
    return re.sub(r"\s+", " ", text).strip().casefold()


def _count_matched_titles(content: str, expected_titles: list[str]) -> int:
    """Count how many input article titles appear in the generated briefing."""
    normalised_content = _normalise_title(content)
    return sum(
        1
        for title in expected_titles
        if title and _normalise_title(title) in normalised_content
    )


def _looks_cut_off(content: str) -> bool:
    """Return True for common half-written markdown or sentence endings."""
    stripped = content.strip()
    if not stripped:
        return True
    if re.search(r"\*\*[^*\n]{1,160}$", stripped):
        return True
    if stripped.endswith(("**", "*", "`", "：", ":", "，", ",")):
        return True
    return False


def validate_briefing_content(
    content: str, expected_count: int, expected_titles: list[str] | None = None
) -> None:
    """Validate that a regular RSS briefing appears complete before saving."""
    if not content.strip():
        raise BriefingGenerationError("empty briefing")
    actual_count = _count_numbered_items(content)
    matched_titles = (
        _count_matched_titles(content, expected_titles) if expected_titles else 0
    )
    if (
        expected_count > 1
        and actual_count < expected_count
        and matched_titles < expected_count
    ):
        raise BriefingGenerationError(
            f"incomplete briefing: expected {expected_count} items, "
            f"got {actual_count} numbered items and {matched_titles} title matches"
        )
    if _looks_cut_off(content):
        raise BriefingGenerationError("briefing appears cut off")


def _build_regular_prompt(prompt_template: str, ds: DataSource, batch: list) -> str:
    article_list = ds.format_items(batch)
    return (
        prompt_template.replace("{count}", str(len(batch)))
        .replace("{display_name}", ds.display_name)
        .replace("{article_list}", article_list)
        .replace("{date}", DATE)
    )


def _generate_regular_briefings(
    ds: DataSource,
    batch: list,
    prompt_template: str,
    model: str,
    *,
    max_tokens: int = 4000,
) -> list[tuple[str, list]]:
    """Generate one or more complete briefings, splitting oversized batches.

    Returns list of (content, batch_items) tuples so callers can track which
    items were successfully processed for commit_seen.
    """
    prompt = _build_regular_prompt(prompt_template, ds, batch)
    try:
        content = call_ai(prompt, model=model, max_tokens=max_tokens)
        validate_briefing_content(content, len(batch), [item.title for item in batch])
        log(
            f"    AI ok: source={ds.name}, articles={len(batch)}, "
            f"prompt_chars={len(prompt)}, response_chars={len(content)}"
        )
        return [(content, batch)]
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
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return str(full)


def _make_placeholder_briefing(ds, items: list) -> str:
    """Generate a placeholder briefing with titles and links for items that
    failed AI generation.

    These items are deliberately *not* committed to seen: seen state never
    expires, so committing one would drop it permanently instead of leaving it
    for the next run to fetch and retry.  The source's own lookback window
    bounds how long a failure can be retried."""
    lines = [f"# {ds.display_name} - {DATE}\n"]
    lines.append("⚠️ 以下文章 AI 摘要生成失败，仅保留标题和链接：\n")
    for idx, item in enumerate(items, 1):
        url_part = f"  [查看原文]({item.url})" if item.url else ""
        lines.append(f"{idx}. **{item.title}**\n{url_part}")
    return "\n".join(lines)


def _retry_failed_items(
    ds,
    failed_items: list,
    prompt_template: str,
    model: str,
) -> list[tuple[str, list]]:
    """Phase 2: retry failed items with smaller batches and more tokens.

    Returns list of (content, batch_items) tuples. Items that still fail
    after Phase 2 are returned with an empty batch_items list and
    placeholder content so the caller can commit them to seen.
    """
    if not failed_items:
        return []
    log(
        f"    Phase 2: retrying {len(failed_items)} failed articles "
        f"with conservative settings (batch=3, max_tokens=4000)"
    )
    results: list[tuple[str, list]] = []
    batch_size = 3
    for i in range(0, len(failed_items), batch_size):
        batch = failed_items[i : i + batch_size]
        try:
            results.extend(
                _generate_regular_briefings(
                    ds,
                    batch,
                    prompt_template,
                    model,
                    max_tokens=4000,
                )
            )
        except Exception as e:
            log(f"    Phase 2 retry failed for {len(batch)} articles: {e}")
            placeholder = _make_placeholder_briefing(ds, batch)
            results.append((placeholder, batch))  # batch kept for commit_seen
    return results


# Regex patterns for parsing AI-generated briefing parts
_RE_HEADER = re.compile(r"^## 📚 .+$", re.MULTILINE)
_RE_NUMBERED = re.compile(r"^(\d+)\.\s+\*\*", re.MULTILINE)


def _merge_briefing_parts(ds, parts: list[tuple[str, list]]) -> tuple[str, list]:
    """Merge multiple briefing parts into one cohesive document.

    Strips per-batch headers and renumbers articles sequentially.  The
    parts arrive in final form: the batch-level highlight section this
    merge used to collect (and move to the end) is gone from the prompts
    and from here -- text outside the numbered list is kept where the
    model put it.

    Returns (merged_content, all_items).
    """
    if not parts:
        return "", []
    if len(parts) == 1:
        return parts[0]

    all_items: list = []
    article_blocks: list[str] = []

    for content, items in parts:
        all_items.extend(items)

        # Remove per-batch header line (## 📚 ...)
        article_part = _RE_HEADER.sub("", content).strip()

        # Renumber articles sequentially
        current_num = len(all_items) - len(items)

        def _renumber(m):
            nonlocal current_num
            current_num += 1
            return f"{current_num}. **"

        article_part = _RE_NUMBERED.sub(_renumber, article_part)

        if article_part:
            article_blocks.append(article_part)

    # Build merged content
    total = len(all_items)
    header = f"## 📚 {ds.display_name} 今日简报 ({DATE}) - {total}篇文章"
    merged = header + "\n\n"
    merged += "\n\n".join(article_blocks)

    return merged, all_items


def _already_pushed_within(name: str, category: str, lookback_hours: int) -> bool:
    """Return True if this source already produced a pushed briefing recently.

    Used to skip redundant AI calls on low-frequency (weekly/biweekly) sources
    where lookback_hours > 24 and we don't want to regenerate the same window.
    ``--force`` overrides it, the same way it overrides
    ``_has_real_briefing_today``: without that, resuming a low-frequency source
    fetched nothing, because its own recent archive still sat inside the
    window, and the command reported success.
    """
    if _is_forced(name):
        return False
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

# Canonical gaps recorded during a run: sources that failed to produce
# structured output, and therefore are absent from that category's publication.
# The run still publishes what did succeed, but reports these so the gap is
# visible (both delivery sinks read the canonical bundle, so a gap here is a
# gap everywhere) and the exit code stays non-zero.
PUBLICATION_GAPS: list[str] = []

# The direct helper functions retain their historical Markdown-only behavior
# for callers/tests that use them as library helpers.  ``main`` enables the
# production boundary so the user-facing ``dailyinfo run`` is fully
# integrated without changing the old helper contract underneath it.
PUBLICATION_INTEGRATION: bool = False


def _is_forced(name: str) -> bool:
    """True when the caller explicitly requested a re-run for this source."""
    return FORCE_ALL or name in FORCE_SOURCES


def _has_real_briefing_today(name: str, category: str) -> bool:
    """Return True when a non-placeholder briefing for ``name`` already exists today.

    Used to skip redundant fetch+AI work when a pipeline is re-run on the same
    day. Scans both ``BRIEFINGS_DIR`` (generated but not yet pushed) and
    ``PUSHED_DIR`` (already pushed and archived today) so the check holds
    across the full lifecycle. Placeholder files -- every notice
    ``_is_placeholder_text`` recognizes, fetch failures and AI failures
    included -- do not count as real briefings so they can be regenerated
    when the failure clears; a real briefing that merely warns about
    something still counts. ``--force`` (``FORCE_ALL`` / ``FORCE_SOURCES``)
    overrides this check.
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
            if not _is_placeholder_text(text):
                return True
    return False


def _load_sources() -> tuple[dict, dict, dict]:
    with open(SOURCES_JSON) as f:
        cfg = json.load(f)
    return cfg, cfg.get("defaults", {}), cfg.get("prompt_templates", {})


class PublicationIntegrationError(RuntimeError):
    """Raised when a real pipeline result cannot be finalized canonically."""


def _build_structured_batch_prompt(
    ds: DataSource, batch: list, prompt_template: str
) -> tuple[str, list[str]]:
    """Build a prompt whose item correlation is explicit and batch-local."""

    entries, refs = structured_entries(ds, batch)
    base = (
        prompt_template.replace("{count}", str(len(batch)))
        .replace("{display_name}", ds.display_name)
        .replace("{article_list}", ds.format_items(batch))
        .replace("{items}", ds.format_items(batch))
        .replace("{date}", DATE)
    )
    return structured_prompt(base, entries, refs), refs


def _generate_structured_batch(
    ds: DataSource,
    batch: list,
    prompt_template: str,
    model: str,
    retrieved_at: datetime.datetime,
    *,
    max_tokens: int = DEFAULT_AI_OUTPUT_TOKENS,
    sections: dict[str, str] | None = None,
    display_titles: dict[str, str] | None = None,
) -> list:
    prompt, _ = _build_structured_batch_prompt(ds, batch, prompt_template)
    raw = call_ai(prompt, model=model, max_tokens=max_tokens)
    results = results_from_response(
        raw,
        batch,
        source_name=ds.name,
        retrieved_at=retrieved_at,
        sections=sections,
        display_titles=display_titles,
    )
    log(
        f"    structured AI ok: source={ds.name}, articles={len(batch)}, "
        f"prompt_chars={len(prompt)}, response_chars={len(raw)}"
    )
    return results


def _generate_structured_briefings(
    ds: DataSource,
    batch: list,
    prompt_template: str,
    model: str,
    retrieved_at: datetime.datetime,
    *,
    max_tokens: int = DEFAULT_AI_OUTPUT_TOKENS,
) -> list:
    """Generate complete structured results, splitting invalid large batches."""

    try:
        return _generate_structured_batch(
            ds,
            batch,
            prompt_template,
            model,
            retrieved_at,
            max_tokens=max_tokens,
        )
    except (BriefingGenerationError, StructuredResultError):
        if len(batch) <= 1:
            raise
        midpoint = max(1, len(batch) // 2)
        log(
            f"    structured AI incomplete for {ds.name} ({len(batch)} articles); "
            f"splitting into {midpoint}+{len(batch) - midpoint}"
        )
        return _generate_structured_briefings(
            ds,
            batch[:midpoint],
            prompt_template,
            model,
            retrieved_at,
            max_tokens=max_tokens,
        ) + _generate_structured_briefings(
            ds,
            batch[midpoint:],
            prompt_template,
            model,
            retrieved_at,
            max_tokens=max_tokens,
        )


def _structured_title(result) -> str:
    raw = result.raw_item
    if result.display_title:
        return result.display_title
    return getattr(raw, "title", "")


def _render_structured_list(
    results: list,
    *,
    header: str | None = None,
    separator: str = "\n",
) -> str:
    """Render items as a numbered list; ``separator`` goes between them.

    An item's own lines are joined tightly, the way the pre-canonical
    briefings were: a blank line between a title and its quote reads as a
    break in Discord rather than as one entry.
    """
    blocks = [header] if header else []
    for index, result in enumerate(results, 1):
        lines = [f"{index}. **{_structured_title(result)}**"]
        lines.append(f"   > {result.summary}")
        if result.why_it_matters:
            lines.append(f"   > **Why it matters:** {result.why_it_matters}")
        blocks.append("\n".join(lines))
    return separator.join(blocks)


def _render_regular_publication(ds: DataSource, results: list) -> str:
    return _render_structured_list(
        results,
        header=f"## 📚 {ds.display_name} 今日简报 ({DATE}) - {len(results)}篇文章",
        separator="\n\n",
    )


def _render_deep_publication(ds: DataSource, result) -> str:
    raw = result.raw_item
    url = getattr(raw, "url", "")
    source_line = f"\n\n[查看原文]({url})" if url else ""
    return (
        f"# AI Daily Digest - {DATE}\n\n## {raw.title}\n\n{result.summary}{source_line}"
    )


def _render_code_publication(ds: DataSource, results: list) -> str:
    return _render_structured_list(
        results,
        header=f"# {ds.display_name} - {DATE}",
        separator="\n\n",
    )


def _render_resource_publication(
    results: list,
    *,
    title: str,
    footer: str = "",
) -> str:
    sections: dict[str, list] = {}
    for result in results:
        sections.setdefault(result.section or "最新动态", []).append(result)
    blocks = [f"# {title} - {DATE}"]
    for section, section_results in sections.items():
        blocks.append(f"### {section}")
        for result in section_results:
            raw = result.raw_item
            blocks.append(f"**[{raw.date}] {raw.title}** — {result.summary}")
            if result.why_it_matters:
                blocks.append(f"> **Why it matters:** {result.why_it_matters}")
    if footer:
        blocks.append(footer)
    return "\n\n".join(blocks)


# The exact notices this pipeline writes when it has no content.  Matching the
# marker rather than "any ⚠️" matters: a real briefing may legitimately warn
# about something, and treating that as a placeholder would let a notice
# replace it.
_PLACEHOLDER_MARKERS = (
    "📭 过去",
    "⚠️ 获取失败",
    "⚠️ AI 生成失败",
    "⚠️ 以下文章 AI 摘要生成失败",
)


def _is_placeholder_text(text: str) -> bool:
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def _save_placeholder(directory: str, filename: str, text: str) -> bool:
    """Save a notice without overwriting a real briefing, and never raise.

    A second run of the same day -- a resume, a manual re-run -- would
    otherwise replace delivered Markdown with a zero-item notice.  Write errors
    are reported rather than raised: these calls sit in error handlers, and a
    disk error there used to replace the original failure and take the whole
    category down with it.
    """
    path = BRIEFINGS_DIR / directory / filename
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if existing and not _is_placeholder_text(existing):
        log(f"    keeping the existing {filename}: a notice would replace it")
        return False
    try:
        save(directory, filename, text)
    except Exception as exc:
        log(f"    WRITE ERR: {exc}")
        return False
    return True


_STORE_LOCK_TIMEOUT_SECONDS = 30


@contextmanager
def _store_lock():
    """Serialise load-merge-save on the canonical store across processes.

    ``dailyinfo resume`` runs outside the scheduled slot and can overlap a cron
    run.  Both would load the same bundle and the later save would win, so the
    other's merged source is silently gone -- with both sinks already reporting
    the day as delivered.

    Waiting is right, but not forever: the lock only ever spans a load and a
    save, so a holder that outlives the timeout is stuck, and failing loudly
    beats hanging a cron-shaped process.
    """
    root = PublicationStore().root
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".lock").open("a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + _STORE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "another run has held the publication store lock for "
                        f"{_STORE_LOCK_TIMEOUT_SECONDS}s"
                    )
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _content_changed(before: PublicationBundle, after: PublicationBundle) -> bool:
    """Whether re-publishing changed what the bundle says.

    Only content counts: ``updated_at`` moves on every re-publication, so
    comparing whole bundles would call an identical re-publication a change and
    cause a second delivery.
    """
    return {item.id for item in before.items} != {
        item.id for item in after.items
    } or before.briefing.body != after.briefing.body


def _void_delivery_state(briefing_id: str) -> list[str]:
    """Mark this briefing undelivered, because its content changed.

    Returns the sinks whose record could not be voided: a failure here means
    the corrected content will never be sent to that sink while it keeps
    reporting the day as delivered, so the caller has to surface it.

    Delivery state is keyed on identity alone, so a briefing that gained a
    source after delivery stays ``success`` for both sinks and the next push or
    publish skips it -- the recovered content reaches nobody while both sinks
    report the day as delivered.
    """
    store = DeliveryStateStore()
    failed: list[str] = []
    for sink in DELIVERY_SINKS:
        try:
            store.void(briefing_id, sink)
            log(
                f"  [delivery] {briefing_id}:{sink} queued for re-delivery "
                "(content changed)"
            )
        except Exception as exc:
            log(f"  [delivery] could not void {briefing_id}:{sink}: {exc}")
            failed.append(sink)
    return failed


def _published_item_ids(category: str) -> set[str]:
    """The identities today's briefing for ``category`` already carries.

    A run seeds its collector with these, so an item that is somehow fetched
    again (seen state lost, operator hand-merge) is not re-rendered: a chunk
    that mixes an already-published item with a new one would otherwise bring
    the old item's prose with it and duplicate it in the body.
    """
    try:
        bundle = PublicationStore().load_bundle(f"{category}-{DATE}")
    except FileNotFoundError:
        return set()
    except Exception as exc:
        # Degrading to "nothing is published" would re-render items the day
        # already carries, which is what the seeding exists to prevent.
        log(f"  [publication] cannot read {category}-{DATE} for seeding: {exc}")
        return set()
    return {item.id for item in bundle.items}


def _finalize_category_publication(
    category: str, collector: PublicationRunCollector
) -> None:
    """Finalize exactly one category/date briefing after all sources finish."""

    publication_id = f"{category}-{DATE}"
    if collector.failures:
        # The sources that did produce structured results are already in the
        # collector, so a source-level failure costs that source and not the
        # category: both sinks read this one bundle, and publishing the part
        # that exists beats publishing nothing.  Item-level integrity still
        # fails closed in the finalizer/validation below.
        action = "partial" if collector.results else "failed"
        log(
            f"  publication_id={publication_id} category={category} "
            f"action={action} item_count={len(collector.results)} "
            f"failed_sources={len(collector.failures)}"
        )
        PUBLICATION_GAPS.append(f"{category}: " + "; ".join(collector.failures))
    if not collector.results:
        if not collector.failures:
            log(
                f"  publication_id={publication_id} category={category} "
                "action=skip item_count=0"
            )
        return
    body = collector.body
    if not body:
        log(
            f"  publication_id={publication_id} category={category} "
            f"action=fail item_count={len(collector.results)}"
        )
        raise PublicationIntegrationError(
            f"{category} publication has items but no canonical body"
        )
    published_at = now_utc()
    try:
        with _store_lock():
            store = PublicationStore()
            try:
                existing = store.load_bundle(publication_id)
            except FileNotFoundError:
                existing = None
            before_ids = {item.id for item in existing.items} if existing else set()
            bundle = _finalize_run(category, collector, existing, published_at)
            if existing is not None and _content_changed(existing, bundle):
                # Before the save: a crash in between then leaves content that
                # differs from a success record, which costs a duplicate
                # delivery rather than a silent one.
                for sink in _void_delivery_state(publication_id):
                    PUBLICATION_GAPS.append(
                        f"{category}: could not void the {sink} delivery record; "
                        "the merged content will not be re-sent to it"
                    )
            result = store.save(bundle)
            collector.added_item_ids = {item.id for item in bundle.items} - before_ids
            collector.commit_deferred_seen()
    except Exception as exc:
        log(
            f"  publication_id={publication_id} category={category} "
            f"action=fail item_count={len(collector.results)}"
        )
        raise PublicationIntegrationError(
            f"{category} publication finalization failed: {exc}"
        ) from exc
    log(
        f"  publication_id={bundle.briefing.id} category={category} "
        f"action={result.action} item_count={len(bundle.items)}"
    )
    if collector.dropped_duplicates:
        # Reported after finalization, which is also where a merge can add to
        # this list: an identity the bundle already carries is only detectable
        # once the bundle is loaded.
        shown = collector.dropped_duplicates[:5]
        rest = len(collector.dropped_duplicates) - len(shown)
        log(
            f"  publication_id={publication_id} category={category} "
            f"action=dedup duplicates={len(collector.dropped_duplicates)} "
            + "; ".join(shown)
            + (f"; +{rest} more" if rest else "")
        )


def _finalize_run(
    category: str,
    collector: PublicationRunCollector,
    existing: PublicationBundle | None,
    published_at: datetime.datetime,
) -> PublicationBundle:
    """Finalize one run, folding it into the bundle the day already has.

    A forced single-source re-run (``dailyinfo run -f <source>``) collects that
    one source, and saving it as the day's bundle would drop every other source
    from both delivery sinks.  So an existing bundle is always merged into:

    - items are unioned by identity, never replaced: ``fetch`` is seen-filtered,
      so a re-run sees only new items, and taking that subset as the bundle
      would drop everything published earlier;
    - a chunk is appended when any of the identities it was rendered from is
      new to the bundle, which lets a resumed source contribute prose without
      stacking a re-render on top of itself.

    There is deliberately no "the run covered everything, replace the body"
    branch: the collector is seeded with the day's identities, so such a run
    cannot occur, and one that did would drop the prose of every item it did
    not re-render.
    """
    finalizer = PublicationFinalizer(business_timezone=str(CONTENT_TIMEZONE))
    briefing = PublicationBriefingInput(
        category=category,
        date=DATE,
        title=f"DailyInfo {category} briefing",
        generated_at=published_at,
        published_at=published_at,
        # Re-publishing an identity the store already has is an update, whether
        # it replaced the body or merged into it; a first publication is not.
        updated_at=published_at if existing is not None else None,
        body=collector.body,
    )
    item_inputs = collector.item_inputs(published_at=published_at)
    if existing is None:
        return finalizer.finalize(briefing, item_inputs)

    bundle_ids = {item.id for item in existing.items}
    bundle_sources = {item.source.name for item in existing.items}
    chunks = [
        part.text
        for part in collector.body_parts
        if set(part.item_ids) - bundle_ids
        or (not part.item_ids and part.source_name not in bundle_sources)
    ]
    return merge_bundle(
        existing,
        item_inputs,
        chunks,
        updated_at=published_at,
        dropped=collector.dropped_duplicates,
    )


# =====================================================================
# Shared pipeline helpers
# =====================================================================


def _filter_sources(cfg: dict, category: str, *types: str) -> list[dict]:
    """Return enabled sources matching the given category and types."""
    return [
        s
        for s in cfg["sources"]
        if s.get("category") == category
        and s.get("type") in types
        and s.get("enabled", True)
    ]


def _process_regular_source_publication(
    ds,
    feed_cfg: dict,
    model_default: str,
    templates: dict,
    default_tmpl_key: str,
    collector: PublicationRunCollector,
) -> int:
    """Structured counterpart of the legacy regular-source processor."""

    name, category = ds.name, ds.category
    try:
        retrieved_at = now_utc()
        items = ds.fetch()
    except Exception as exc:
        log(f"    FETCH ERR: {exc}")
        collector.add_failure(f"{name}: fetch failed: {exc}")
        _save_placeholder(
            category,
            f"{name}_briefing_{DATE}.md",
            f"# {ds.display_name} - {DATE}\n\n⚠️ 获取失败\n",
        )
        return 1

    if not items:
        log(f"  {name}: 0 new articles - placeholder")
        collector.defer_seen(ds, items)
        _save_placeholder(
            category,
            f"{name}_briefing_{DATE}.md",
            f"# {ds.display_name} - {DATE}\n\n"
            f"📭 过去 {ds.lookback_hours} 小时无新内容\n",
        )
        if isinstance(ds, RSSDataSource):
            from freshrss_cache import record_zero_result

            zero_days = record_zero_result(STATE_DIR, name, DATE)
            if zero_days >= 2:
                log(
                    f"  [WARN] {name}: {zero_days} consecutive days with 0 articles — "
                    "FreshRSS cache may be stuck — run: dailyinfo cache-clear"
                )
        return 1

    if isinstance(ds, RSSDataSource):
        from freshrss_cache import reset_zero_result

        reset_zero_result(STATE_DIR, name)

    if ds.lookback_hours > 24 and _already_pushed_within(
        name, category, ds.lookback_hours
    ):
        log(f"  {name}: {len(items)} articles - already pushed recently, skip")
        return 0

    model = feed_cfg.get("model") or model_default
    tmpl_key = feed_cfg.get("prompt_template") or default_tmpl_key
    prompt_template = templates.get(tmpl_key) or templates.get("one_line_summary", "")
    if not prompt_template:
        collector.add_failure(f"{name}: no prompt template")
        return 0

    total = getattr(ds, "_total_before_filter", len(items))
    log(
        f"  {name}: {len(items)} new articles"
        + (f" ({total - len(items)} duplicates skipped)" if total != len(items) else "")
    )
    structured_results: list = []
    failed_items: list = []
    for index, batch in enumerate(ds.get_batches(items), 1):
        try:
            structured_results.extend(
                _generate_structured_briefings(
                    ds, batch, prompt_template, model, retrieved_at
                )
            )
        except Exception as exc:
            log(f"    structured ERR batch {index}: {exc}")
            failed_items.extend(batch)

    if failed_items:
        log(f"    retrying {len(failed_items)} failed structured articles")
        retry_results: list = []
        retry_failed: list = []
        for start in range(0, len(failed_items), 3):
            batch = failed_items[start : start + 3]
            try:
                retry_results.extend(
                    _generate_structured_briefings(
                        ds,
                        batch,
                        prompt_template,
                        model,
                        retrieved_at,
                        max_tokens=DEFAULT_AI_OUTPUT_TOKENS,
                    )
                )
            except Exception as exc:
                log(f"    structured retry failed for {len(batch)} articles: {exc}")
                retry_failed.extend(batch)
        structured_results.extend(retry_results)
        failed_items = retry_failed

    if failed_items:
        collector.add_failure(
            f"{name}: {len(failed_items)} item(s) lacked valid structured AI output"
        )

    # Filter before rendering: the body and the item list have to describe the
    # same set, or the Markdown would keep a paragraph the bundle dropped.
    published: list = []
    structured_results = collector.take_new(structured_results)
    if structured_results:
        try:
            content = _render_regular_publication(ds, structured_results)
            save(category, f"{name}_briefing_{DATE}.md", content)
        except Exception as exc:
            # One source's write failure must not take the category down, and
            # the items stay out of the bundle and out of seen, so the next run
            # retries them.
            log(f"    WRITE ERR: {exc}")
            collector.add_failure(f"{name}: briefing write failed: {exc}")
        else:
            collector.add(structured_results)
            collector.add_body(content, source_name=name)
            published = structured_results
            log(f"    -> saved {name}_briefing_{DATE}.md")
    if failed_items:
        # Preserve the existing retry/placeholder behavior for legacy sinks;
        # the failed items are never added to the canonical collector.
        _save_placeholder(
            category,
            f"{name}_briefing_{DATE}_failed.md",
            _make_placeholder_briefing(ds, failed_items),
        )
    # Only what was published is marked seen.  Seen state never expires, so
    # committing an item whose write or AI call failed would lose it for good
    # instead of leaving it for the next run to retry.
    collector.defer_seen(ds, [result.raw_item for result in published])
    return 1 if structured_results or failed_items else 0


def _process_deep_content_source_publication(
    ds,
    feed_cfg: dict,
    model_default: str,
    templates: dict,
    collector: PublicationRunCollector,
) -> int:
    """Process a deep-content RSS source with one structured call per item."""

    name, category = ds.name, ds.category
    retrieved_at = now_utc()
    try:
        items = ds.fetch()
    except Exception as exc:
        # Unguarded, this aborted the whole category run: the sources after it
        # never ran and nothing recorded why the category came up short.
        log(f"    FETCH ERR: {exc}")
        collector.add_failure(f"{name}: fetch failed: {exc}")
        return 1
    if not items:
        collector.defer_seen(ds, items)
        return 0

    model = feed_cfg.get("model") or model_default
    tmpl_key = feed_cfg.get("prompt_template", "smolai_categorized")
    tmpl = templates.get(tmpl_key, "")
    committed_items: list = []
    saved = 0

    for index, item in enumerate(items, 1):
        base = (
            tmpl.replace("{content}", item.content).replace("{date}", DATE)
            if tmpl
            else (
                "Summarize the following AI news in Chinese by category:\n\n"
                f"{item.content}"
            )
        )
        ref = source_ref(0)
        entries = f"[source_ref={ref}]\nTitle: {item.title}\nContent:\n{item.content}"
        prompt = structured_prompt(base, entries, [ref], markdown_summary=True)
        try:
            raw = call_ai(prompt, model=model, max_tokens=_DEEP_CONTENT_MAX_TOKENS)
            result = results_from_response(
                raw,
                [item],
                source_name=name,
                retrieved_at=retrieved_at,
                allow_literal_newlines=True,
            )[0]
            if not collector.take_new([result]):
                # Repeats an item already collected for this category, so it is
                # published once and nothing is written for this copy.
                committed_items.append(item)
                continue
            content = _render_deep_publication(ds, result)
            suffix = f"_part{index}" if len(items) > 1 else ""
            save(category, f"{name}_briefing_{DATE}{suffix}.md", content)
            collector.add([result])
            collector.add_body(content, source_name=name)
            committed_items.append(item)
            saved += 1
            log(f"    -> saved {name}_briefing_{DATE}{suffix}.md")
        except Exception as exc:
            log(f"    structured ERR for {name} item {index}: {exc}")
            try:
                raw = call_ai(prompt, model=model, max_tokens=_DEEP_CONTENT_MAX_TOKENS)
                result = results_from_response(
                    raw,
                    [item],
                    source_name=name,
                    retrieved_at=retrieved_at,
                    allow_literal_newlines=True,
                )[0]
                if not collector.take_new([result]):
                    committed_items.append(item)
                    continue
                content = _render_deep_publication(ds, result)
                filename = f"{name}_briefing_{DATE}_retry{index}.md"
                save(category, filename, content)
                collector.add([result])
                collector.add_body(content, source_name=name)
                committed_items.append(item)
                saved += 1
                log(f"    -> saved {filename}")
            except Exception as retry_exc:
                collector.add_failure(f"{name} item {index}: {retry_exc}")
                filename = f"{name}_briefing_{DATE}_failed{index}.md"
                if _save_placeholder(
                    category, filename, _make_placeholder_briefing(ds, [item])
                ):
                    saved += 1
                # Deliberately not committed to seen: seen state never expires,
                # so a failed item committed here could never be retried.

    collector.defer_seen(ds, committed_items)
    return saved


def _ensure_rss_subscriptions(cfg: dict, db, category: str) -> list[str]:
    """Subscribe this category's enabled RSS sources; returns the new names.

    Adding a source to config/sources.json is the whole of the setup: the
    next pipeline run for its category creates the FreshRSS subscription.
    A locked or read-only database degrades to a warning -- the rest of the
    category run still works, and the next run retries the subscription.
    """
    from datasource import build_feed_url_map, resolve_feed_id
    from freshrss_admin import ensure_subscription, resolve_category

    added: list[str] = []
    try:
        full_map, base_map = build_feed_url_map(db)
        for source in _filter_sources(cfg, category, "rss"):
            # Canonicalise exactly as ensure_subscription will, or a padded
            # config URL slips past both checks below and inserts a twin.
            url = (source.get("url") or "").strip()
            category_name = source.get("freshrss_category")
            if category_name and resolve_category(db, category_name) is None:
                log(
                    f"  [WARN] {source['name']}: FreshRSS category"
                    f" {category_name!r} not found -> Uncategorized"
                )
            similar = resolve_feed_id(url, full_map, base_map) if url else None
            exact = db.execute("SELECT 1 FROM feed WHERE url = ?", [url]).fetchone()
            if similar and not exact:
                # The stored feed's name, not its URL: subscription URLs can
                # carry private tokens, and this log line is for a human.
                stored = db.execute(
                    "SELECT name FROM feed WHERE id = ?", [similar]
                ).fetchone()
                log(
                    f"  [WARN] {source['name']}: a similar feed is already"
                    f" subscribed ({stored[0]}) -- subscribing the exact"
                    " config URL anyway"
                )
            if ensure_subscription(
                db,
                url=url,
                name=source.get("display_name") or source.get("name", ""),
                category=category_name,
            ):
                added.append(source["name"])
    except sqlite3.OperationalError as e:
        log(f"  [WARN] subscription sync skipped: {e}")
    return added


def _process_regular_source(
    ds,
    feed_cfg: dict,
    model_default: str,
    templates: dict,
    default_tmpl_key: str,
    collector: PublicationRunCollector | None = None,
) -> int:
    """Process a single source: fetch -> batch -> AI -> merge -> save -> commit.

    Returns number of files saved (0 or 1).
    """
    # A caller that passes a collector is asking for the canonical path, so the
    # collector decides as well as the flag does.  Without this, a caller that
    # never ran `main` (the resume command is one) fell through to the legacy
    # path: the collector stayed empty and the legacy path marked the items
    # seen, so the content it fetched was lost rather than published.
    if PUBLICATION_INTEGRATION or collector is not None:
        own_collector = collector or PublicationRunCollector(
            ds.category, known_item_ids=_published_item_ids(ds.category)
        )
        saved = _process_regular_source_publication(
            ds, feed_cfg, model_default, templates, default_tmpl_key, own_collector
        )
        if collector is None:
            _finalize_category_publication(ds.category, own_collector)
        return saved

    name, category = ds.name, ds.category

    try:
        items = ds.fetch()
    except Exception as e:
        log(f"    FETCH ERR: {e}")
        placeholder = f"# {ds.display_name} - {DATE}\n\n" + "⚠️ 获取失败\n"
        save(category, f"{name}_briefing_{DATE}.md", placeholder)
        return 1

    if not items:
        log(f"  {name}: 0 new articles - placeholder")
        ds.commit_seen(items)
        placeholder = (
            f"# {ds.display_name} - {DATE}\n\n"
            + "\U0001f4ed 过去 {ds.lookback_hours} 小时无新内容\n"
        )
        save(category, f"{name}_briefing_{DATE}.md", placeholder)
        if isinstance(ds, RSSDataSource):
            from freshrss_cache import record_zero_result

            zero_days = record_zero_result(STATE_DIR, name, DATE)
            if zero_days >= 2:
                log(
                    f"  [WARN] {name}: {zero_days} consecutive days with 0 articles — "
                    f"FreshRSS cache may be stuck. Run: dailyinfo cache-clear"
                )
        return 1

    if isinstance(ds, RSSDataSource):
        from freshrss_cache import reset_zero_result

        reset_zero_result(STATE_DIR, name)

    if ds.lookback_hours > 24 and _already_pushed_within(
        name, category, ds.lookback_hours
    ):
        log(
            f"  {name}: {len(items)} articles - already pushed within "
            f"{ds.lookback_hours}h, skip"
        )
        return 0

    model = feed_cfg.get("model") or model_default
    tmpl_key = feed_cfg.get("prompt_template") or default_tmpl_key
    prompt_template = templates.get(tmpl_key) or templates.get("one_line_summary", "")
    if not prompt_template:
        log(f"  SKIP {name}: no prompt template")
        return 0

    # Log article count (with dup info for RSS sources)
    total = getattr(ds, "_total_before_filter", len(items))
    new = len(items)
    dup = total - new
    log(
        f"  {name}: {new} new articles"
        + (f" ({dup} duplicates skipped)" if dup else "")
    )

    generated_parts: list[tuple[str, list]] = []
    failed_items: list = []
    for idx, batch in enumerate(ds.get_batches(items)):
        try:
            generated_parts.extend(
                _generate_regular_briefings(ds, batch, prompt_template, model)
            )
        except Exception as e:
            log(f"    ERR batch {idx + 1}: {e}")
            failed_items.extend(batch)

    if failed_items:
        generated_parts.extend(
            _retry_failed_items(ds, failed_items, prompt_template, model)
        )

    merged_content, all_items = _merge_briefing_parts(ds, generated_parts)
    if merged_content:
        filename = f"{name}_briefing_{DATE}.md"
        try:
            save(category, filename, merged_content)
            log(f"    -> saved {filename}")
        except Exception as e:
            log(f"    SAVE ERR: {e}")

    ds.commit_seen(all_items)
    return 1 if merged_content else 0


def _process_deep_content_source(
    ds, feed_cfg: dict, model_default: str, templates: dict
) -> int:
    """Process a use_content source: one AI call per article, per-article files.

    Returns number of files saved.
    """
    if PUBLICATION_INTEGRATION:
        collector = PublicationRunCollector(
            ds.category, known_item_ids=_published_item_ids(ds.category)
        )
        saved = _process_deep_content_source_publication(
            ds, feed_cfg, model_default, templates, collector
        )
        _finalize_category_publication(ds.category, collector)
        return saved

    name, category = ds.name, ds.category
    model = feed_cfg.get("model") or model_default
    tmpl_key = feed_cfg.get("prompt_template")
    tmpl = templates.get(tmpl_key, "") if tmpl_key else ""
    if not tmpl:
        log(f"  SKIP {name}: no prompt template")
        return 0
    saved = 0

    committed_items: list = []
    failed_items: list = []
    for idx, item in enumerate(items := ds.fetch()):
        prompt = tmpl.replace("{content}", item.content).replace("{date}", DATE)
        suffix = f"_part{idx + 1}" if len(items) > 1 else ""
        filename = f"{name}_briefing_{DATE}{suffix}.md"
        try:
            content_text = call_ai(
                prompt, model=model, max_tokens=_DEEP_CONTENT_MAX_TOKENS
            )
            save(category, filename, f"# AI Daily Digest - {DATE}\n\n{content_text}")
            saved += 1
            committed_items.append(item)
            log(f"    -> saved {filename}")
            time.sleep(1)
        except Exception as e:
            log(f"    ERR: {e}")
            failed_items.append(item)

    if failed_items:
        log(f"    Phase 2: retrying {len(failed_items)} failed deep-content articles")
        for retry_idx, item in enumerate(failed_items, start=1):
            prompt = tmpl.replace("{content}", item.content).replace("{date}", DATE)
            try:
                content_text = call_ai(
                    prompt, model=model, max_tokens=_DEEP_CONTENT_MAX_TOKENS
                )
                filename = f"{name}_briefing_{DATE}_retry{retry_idx}.md"
                save(
                    category, filename, f"# AI Daily Digest - {DATE}\n\n{content_text}"
                )
                saved += 1
                committed_items.append(item)
                log(f"    -> saved retry {filename}")
            except Exception as e:
                log(f"    Phase 2 retry failed: {e} -> placeholder")
                placeholder = _make_placeholder_briefing(ds, [item])
                filename = f"{name}_briefing_{DATE}_failed{retry_idx}.md"
                save(category, filename, placeholder)
                saved += 1
                committed_items.append(item)

    ds.commit_seen(committed_items)
    return saved


def _run_category_pipeline(
    category: str,
    *,
    create_marker: bool = False,
    deep_content: bool = False,
    collector: PublicationRunCollector | None = None,
    only_source: str | None = None,
) -> int:
    """Generic pipeline for a single category.

    Handles both RSS and non-RSS sources. If *create_marker* is True,
    the arXiv generation marker is created before processing and removed
    in a finally block. If *deep_content* is True, the use_content
    path is used instead of the regular batched path. A *collector* may be
    supplied by a caller that needs the run's own output -- the resume path
    does, to send the chunk it just recovered. *only_source* restricts the run
    to one named source, which is what a resume wants: relying on the
    "briefing already exists" skip instead would re-run every source whose
    today-file is a zero-item placeholder.
    """
    cfg, defaults, templates = _load_sources()
    model_default = defaults.get("model", "deepseek-flash")
    default_tmpl_key = defaults.get("prompt_template", "one_line_summary")
    publication_collector = collector
    if publication_collector is None and PUBLICATION_INTEGRATION:
        publication_collector = PublicationRunCollector(
            category, known_item_ids=_published_item_ids(category)
        )

    # --- RSS sources ---
    try:
        db = sqlite3.connect(FRESHRSS_DB, timeout=30)
    except Exception as e:
        user = _get_freshrss_user()
        log(f"Pipeline {category} FAILED: cannot open FreshRSS DB ({e})")
        log(f"  DB path: {FRESHRSS_DB}")
        log(f"  Fix: set FRESHRSS_USER={user} in .env, or correct the username.")
        if publication_collector is not None:
            # A resume asked for one source and got nothing; without this it
            # would report "nothing new" for a database it could not read.
            publication_collector.add_failure(
                f"{category}: cannot open the FreshRSS DB ({e})"
            )
        return 0
    db.row_factory = sqlite3.Row
    for name in _ensure_rss_subscriptions(cfg, db, category):
        log(f"  subscribed {name} -> FreshRSS")
    full_map, base_map = build_feed_url_map(db)

    if create_marker:
        _create_arxiv_marker()

    try:
        saved = 0
        rss_sources = _filter_sources(cfg, category, "rss")
        for feed_cfg in rss_sources:
            ds = DataSource.create(
                feed_cfg, defaults, db=db, full_map=full_map, base_map=base_map
            )
            assert isinstance(ds, RSSDataSource)
            if only_source is not None and ds.name != only_source:
                continue
            if _has_real_briefing_today(ds.name, ds.category):
                log(
                    f"  {ds.name}: briefing already exists for {DATE}, skip "
                    f"(use --force {ds.name} to regenerate)"
                )
                continue

            if deep_content:
                if publication_collector is None:
                    saved += _process_deep_content_source(
                        ds, feed_cfg, model_default, templates
                    )
                else:
                    saved += _process_deep_content_source_publication(
                        ds, feed_cfg, model_default, templates, publication_collector
                    )
            else:
                saved += _process_regular_source(
                    ds,
                    feed_cfg,
                    model_default,
                    templates,
                    default_tmpl_key,
                    publication_collector,
                )
    finally:
        db.close()
        if create_marker:
            _remove_arxiv_marker()

    # --- Non-RSS sources ---
    for source_cfg in _filter_sources(cfg, category, "scrape", "api"):
        ds = DataSource.create(source_cfg, defaults)
        if only_source is not None and ds.name != only_source:
            continue
        if _has_real_briefing_today(ds.name, ds.category):
            log(f"    briefing already exists for {DATE}, skip")
            continue
        log(f"  {ds.name}...")
        saved += _process_regular_source(
            ds,
            source_cfg,
            model_default,
            templates,
            default_tmpl_key,
            publication_collector,
        )

    if publication_collector is not None:
        _finalize_category_publication(category, publication_collector)

    return saved


# =====================================================================
# PIPELINE 1: Papers
# =====================================================================
def run_pipeline_papers() -> int:
    log("=== Pipeline 1: Papers ===")
    saved = _run_category_pipeline("papers")
    log(f"  Pipeline 1 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 2: AI News
# =====================================================================
def run_pipeline_ai_news() -> int:
    log("=== Pipeline 2: AI News ===")
    saved = _run_category_pipeline("ai_news", deep_content=True)
    log(f"  Pipeline 2 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 3: arXiv
# =====================================================================
def run_pipeline_arxiv() -> int:
    log("=== Pipeline 3: arXiv ===")
    saved = _run_category_pipeline("arxiv", create_marker=True)
    log(f"  Pipeline 3 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 4: Code Trending (GitHub + HuggingFace)
# =====================================================================
def _run_pipeline_code_publication() -> int:
    """Run code sources and finalize one canonical code briefing."""

    log("=== Pipeline 4: Code Trending ===")
    cfg, defaults, templates = _load_sources()
    model_default = defaults.get("model", "deepseek-v4-flash")
    code_tmpl = templates.get("code_trending", "")
    collector = PublicationRunCollector(
        "code", known_item_ids=_published_item_ids("code")
    )
    saved = 0

    for source_cfg in cfg["sources"]:
        if source_cfg.get("category") != "code" or source_cfg.get("enabled") is False:
            continue
        ds = DataSource.create(source_cfg, defaults)
        log(f"  {ds.name}...")
        if _has_real_briefing_today(ds.name, "code"):
            log(f"    briefing already exists for {DATE}, skip")
            continue

        items = None
        for attempt in range(3):
            try:
                retrieved_at = now_utc()
                items = ds.fetch()
                break
            except Exception as exc:
                log(f"    FETCH ERR (attempt {attempt + 1}/3): {exc}")
                if attempt < 2:
                    time.sleep(_BACKOFF_SECONDS[attempt])
        if items is None:
            collector.add_failure(f"{ds.name}: fetch failed")
            _save_placeholder(
                "code",
                f"{ds.name}_briefing_{DATE}.md",
                f"# {ds.display_name} - {DATE}\n\n⚠️ 获取失败\n",
            )
            continue
        if not items:
            log("    no items")
            continue

        tmpl_key = source_cfg.get("prompt_template") or "code_trending"
        prompt_tmpl = templates.get(tmpl_key) or code_tmpl
        if not prompt_tmpl:
            collector.add_failure(f"{ds.name}: no prompt template")
            continue
        prompt, _ = _build_structured_batch_prompt(ds, items, prompt_tmpl)
        display_titles = {
            source_ref(index): item.extra.get("full_name")
            or item.extra.get("name")
            or item.title
            for index, item in enumerate(items)
        }
        try:
            raw = call_ai(
                prompt,
                model=source_cfg.get("model", model_default),
                max_tokens=DEFAULT_AI_OUTPUT_TOKENS,
            )
            results = results_from_response(
                raw,
                items,
                source_name=ds.name,
                retrieved_at=retrieved_at,
                display_titles=display_titles,
            )
            results = collector.take_new(results)
            if not results:
                continue
            content = _render_code_publication(ds, results)
            save("code", f"{ds.name}_briefing_{DATE}.md", content)
            collector.add(results)
            collector.add_body(content, source_name=ds.name)
            saved += 1
            log(f"    -> saved {ds.name}_briefing_{DATE}.md")
        except Exception as exc:
            collector.add_failure(f"{ds.name}: invalid structured AI output: {exc}")
            _save_placeholder(
                "code",
                f"{ds.name}_briefing_{DATE}_failed.md",
                f"# {ds.display_name} - {DATE}\n\n⚠️ AI 生成失败\n",
            )

    _finalize_category_publication("code", collector)
    log(f"  Pipeline 4 done: {saved} files saved")
    return saved


def run_pipeline_code() -> int:
    if PUBLICATION_INTEGRATION:
        return _run_pipeline_code_publication()

    log("=== Pipeline 4: Code Trending ===")
    cfg, defaults, templates = _load_sources()
    model_default = defaults.get("model", "deepseek-flash")
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

        items = None
        for _attempt in range(3):
            try:
                items = ds.fetch()
                break
            except Exception as e:
                log(f"    FETCH ERR (attempt {_attempt + 1}/3): {e}")
                if _attempt < 2:
                    time.sleep(_BACKOFF_SECONDS[_attempt])
        if items is None:
            placeholder = f"# {ds.display_name} - {DATE}\n\n⚠️ 获取失败\n"
            save("code", f"{ds.name}_briefing_{DATE}.md", placeholder)
            saved += 1
            continue

        if not items:
            log("    no items")
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
                f"请为以下 {ds.display_name} 的每一条目写一行中文简介，"
                "突出核心功能或技术亮点。\n\n"
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
                prompt,
                model=source_cfg.get("model", model_default),
                max_tokens=2500,
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
            placeholder = f"# {ds.display_name} - {DATE}\n\n⚠️ AI 生成失败\n"
            save("code", f"{ds.name}_briefing_{DATE}.md", placeholder)
            saved += 1

    log(f"  Pipeline 4 done: {saved} files saved")
    return saved


# =====================================================================
# PIPELINE 5: University News (DLUT HTML + API)
# =====================================================================
_DLUT_NEWS_GROUP = "dlut_news"
_DLUT_NEWS_SECTION_ORDER = [
    "综合新闻",
    "人才培养",
    "学术科研",
    "合作交流",
    "一线风采",
    "学院动态",
]


def _run_pipeline_resource_publication() -> int:
    """Run DLUT news/recruitment adapters and finalize canonical resource data."""

    log("=== Pipeline 5: University News & Recruitment ===")
    cfg, defaults, prompt_templates = _load_sources()
    model_default = defaults.get("model", "deepseek-v4-flash")
    collector = PublicationRunCollector(
        "resource", known_item_ids=_published_item_ids("resource")
    )
    saved = 0

    news_sources = [
        s
        for s in cfg["sources"]
        if s.get("category") == "resource"
        and s.get("news_group") == _DLUT_NEWS_GROUP
        and s.get("enabled", True) is not False
    ]
    if news_sources and not _has_real_briefing_today(_DLUT_NEWS_GROUP, "resource"):
        records: list[tuple[dict, str, object, datetime.datetime]] = []
        seen_urls: set[str] = set()
        for source_cfg in news_sources:
            ds = DataSource.create(source_cfg, defaults)
            section = source_cfg.get("section", ds.display_name)
            try:
                retrieved_at = now_utc()
                raw_items = ds.fetch()
            except Exception as exc:
                collector.add_failure(f"{ds.name}: fetch failed: {exc}")
                continue
            for item in raw_items:
                if item.url and item.url in seen_urls:
                    continue
                if item.url:
                    seen_urls.add(item.url)
                records.append((source_cfg, section, item, retrieved_at))

        if records:
            entries = []
            refs = []
            source_names = []
            sections: dict[str, str] = {}
            for index, (source_cfg, section, item, _item_retrieved_at) in enumerate(
                records
            ):
                ref = source_ref(index)
                refs.append(ref)
                source_names.append(source_cfg["name"])
                sections[ref] = section
                entries.append(
                    f"[source_ref={ref}] [{section}] [{item.date}] "
                    f"{item.title}\n{item.url}"
                )
            items_text = "\n".join(entries)
            tmpl = prompt_templates.get("university_news_unified", "")
            base = tmpl.replace("{items}", items_text).replace("{date}", DATE)
            prompt = structured_prompt(base, items_text, refs)
            try:
                raw = call_ai(
                    prompt, model=model_default, max_tokens=DEFAULT_AI_OUTPUT_TOKENS
                )
                results = results_from_response(
                    raw,
                    [record[2] for record in records],
                    retrieved_at=[record[3] for record in records],
                    source_names=source_names,
                    sections=sections,
                )
                results = collector.take_new(results)
                if results:
                    content = _render_resource_publication(
                        results,
                        title="大连理工大学校园动态",
                        footer=(
                            f"---\n*共 {len(results)} 条动态，来自 "
                            f"{len(news_sources)} 个信源汇总*"
                        ),
                    )
                    save("resource", f"{_DLUT_NEWS_GROUP}_briefing_{DATE}.md", content)
                    collector.add(results)
                    collector.add_body(content, source_name=_DLUT_NEWS_GROUP)
                    saved += 1
                    log(f"    -> saved {_DLUT_NEWS_GROUP}_briefing_{DATE}.md")
            except Exception as exc:
                collector.add_failure(
                    f"{_DLUT_NEWS_GROUP}: invalid structured AI output: {exc}"
                )
                _save_placeholder(
                    "resource",
                    f"{_DLUT_NEWS_GROUP}_briefing_{DATE}_failed.md",
                    f"# 大连理工大学校园动态 - {DATE}\n\n⚠️ AI 生成失败\n",
                )
        else:
            log("    no updates -> no canonical publication")

    for source_cfg in cfg["sources"]:
        if (
            source_cfg.get("category") != "resource"
            or source_cfg.get("enabled") is False
            or source_cfg.get("news_group") == _DLUT_NEWS_GROUP
        ):
            continue
        ds = DataSource.create(source_cfg, defaults)
        log(f"  {ds.name}...")
        if _has_real_briefing_today(ds.name, "resource"):
            log(f"    briefing already exists for {DATE}, skip")
            continue
        try:
            retrieved_at = now_utc()
            items = ds.fetch()
        except Exception as exc:
            collector.add_failure(f"{ds.name}: fetch failed: {exc}")
            log(f"    FETCH ERR: {exc}")
            continue
        if not items:
            _save_placeholder(
                "resource",
                f"{ds.name}_briefing_{DATE}.md",
                f"# {ds.display_name} - {DATE}\n\n"
                f"📭 过去 {ds.lookback_hours} 小时无新内容\n",
            )
            saved += 1
            continue
        items_text = ds.format_items(items)
        tmpl_key = source_cfg.get("prompt_template", "university_news")
        base_template = prompt_templates.get(tmpl_key) or prompt_templates.get(
            "university_news", ""
        )
        base = base_template.replace(
            "{items}", f"{ds.display_name}\n{items_text}"
        ).replace("{date}", DATE)
        entries, refs = structured_entries(ds, items)
        prompt = structured_prompt(base, entries, refs)
        try:
            raw = call_ai(
                prompt, model=model_default, max_tokens=DEFAULT_AI_OUTPUT_TOKENS
            )
            results = results_from_response(
                raw,
                items,
                source_name=ds.name,
                retrieved_at=retrieved_at,
                sections={ref: ds.display_name for ref in refs},
            )
            results = collector.take_new(results)
            if not results:
                continue
            content = _render_resource_publication(
                results,
                title=ds.display_name,
                footer=f"---\n*{len(results)} items (past {ds.lookback_hours}h)*",
            )
            save("resource", f"{ds.name}_briefing_{DATE}.md", content)
            collector.add(results)
            collector.add_body(content, source_name=ds.name)
            saved += 1
            log(f"    -> saved {ds.name}_briefing_{DATE}.md")
        except Exception as exc:
            collector.add_failure(f"{ds.name}: invalid structured AI output: {exc}")

    _finalize_category_publication("resource", collector)
    log(f"  Pipeline 5 done: {saved} files saved")
    return saved


def _generate_unified_news(
    news_sources: list,
    defaults: dict,
    prompt_templates: dict,
    model_default: str,
) -> int:
    """Batch-fetch all news-group sources, merge by section, URL-dedup, one AI call."""
    sections: dict = {}  # section_name -> list[Item]
    seen_urls: set = set()

    for src in news_sources:
        ds = DataSource.create(src, defaults)
        section = src.get("section", ds.display_name)
        log(f"  {ds.name} [{section}]...")
        try:
            raw = ds.fetch()
        except Exception as e:
            log(f"    FETCH ERR: {e}")
            raw = []

        deduped = []
        for item in raw:
            if item.url and item.url in seen_urls:
                continue
            if item.url:
                seen_urls.add(item.url)
            deduped.append(item)

        sections.setdefault(section, []).extend(deduped)
        log(f"    {len(deduped)} items")

    total = sum(len(v) for v in sections.values())

    if total == 0:
        placeholder = (
            f"# 大连理工大学校园动态 - {DATE}\n\n" f"\U0001f4ed 过去 48 小时无新内容\n"
        )
        save("resource", f"{_DLUT_NEWS_GROUP}_briefing_{DATE}.md", placeholder)
        log("    no updates -> placeholder")
        return 1

    section_parts = []
    for section_name in _DLUT_NEWS_SECTION_ORDER:
        items = sections.get(section_name, [])
        if not items:
            continue
        lines = [f"{i+1}. [{item.date}] {item.title}" for i, item in enumerate(items)]
        section_parts.append(f"### {section_name}\n" + "\n".join(lines))

    items_text = "\n\n".join(section_parts)
    tmpl = prompt_templates.get("university_news_unified", "")
    prompt = tmpl.replace("{items}", items_text).replace("{date}", DATE)

    try:
        content_text = call_ai(prompt, model=model_default, max_tokens=3000)
        full_content = (
            f"# 大连理工大学校园动态 - {DATE}\n\n"
            f"{content_text}\n\n"
            f"---\n*共 {total} 条动态，来自 {len(news_sources)} 个信源汇总*\n"
        )
        save("resource", f"{_DLUT_NEWS_GROUP}_briefing_{DATE}.md", full_content)
        log(f"    -> saved {_DLUT_NEWS_GROUP}_briefing_{DATE}.md")
        return 1
    except Exception as e:
        log(f"    AI ERR: {e}")
        return 0


def run_pipeline_resource() -> int:
    if PUBLICATION_INTEGRATION:
        return _run_pipeline_resource_publication()

    log("=== Pipeline 5: University News & Recruitment ===")
    cfg, defaults, prompt_templates = _load_sources()
    model_default = defaults.get("model", "deepseek-flash")
    saved = 0

    # --- Part A: unified news briefing (8 news sources -> 1 file) ---
    news_sources = [
        s
        for s in cfg["sources"]
        if s.get("category") == "resource"
        and s.get("news_group") == _DLUT_NEWS_GROUP
        and s.get("enabled", True) is not False
    ]
    if news_sources:
        if _has_real_briefing_today(_DLUT_NEWS_GROUP, "resource"):
            log(
                f"  unified news briefing already exists for {DATE}, skip "
                f"(use --force {_DLUT_NEWS_GROUP} to regenerate)"
            )
        else:
            saved += _generate_unified_news(
                news_sources, defaults, prompt_templates, model_default
            )

    # --- Part B: recruitment sources (per-source, logic unchanged) ---
    for source_cfg in cfg["sources"]:
        if (
            source_cfg.get("category") != "resource"
            or source_cfg.get("enabled") is False
            or source_cfg.get("news_group") == _DLUT_NEWS_GROUP
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
                f"\U0001f4ed 过去 {ds.lookback_hours} 小时无新内容\n\n"
                f'---\n*来源: {source_cfg["url"]}*\n'
            )
            save("resource", f"{ds.name}_briefing_{DATE}.md", no_update)
            saved += 1
            log("    no updates -> placeholder")
            continue

        log(f"    {len(items)} items (within {ds.lookback_hours}h)")
        items_text = ds.format_items(items)
        tmpl_key = source_cfg.get("prompt_template", "university_news")
        prompt_tmpl = prompt_templates.get(tmpl_key) or prompt_templates.get(
            "university_news", ""
        )
        prompt = prompt_tmpl.replace("{items}", f"{ds.display_name}\n{items_text}")

        try:
            content_text = call_ai(prompt, model=model_default, max_tokens=4000)
            display_url = source_cfg.get("list_url", source_cfg.get("url", ""))
            full_content = (
                f"# {ds.display_name} - {DATE}\n\n"
                f"{content_text}\n\n"
                f"---\n*{len(items)} items (past {ds.lookback_hours}h)*\n\n"
                f"\U0001f4cd 查看全部：{display_url}\n"
            )
            save("resource", f"{ds.name}_briefing_{DATE}.md", full_content)
            saved += 1
            log(f"    -> saved {ds.name}_briefing_{DATE}.md")
            time.sleep(1)
        except Exception as e:
            log(f"    AI ERR: {e}")

    log(f"  Pipeline 5 done: {saved} files saved")
    return saved


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="DailyInfo Pipeline Runner")
    parser.add_argument(
        "--pipeline",
        type=int,
        choices=[1, 2, 3, 4, 5],
        help=(
            "Run specific pipeline: 1=papers, 2=ai_news, 3=arxiv, "
            "4=code, 5=resource. Default: all"
        ),
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

    global FORCE_ALL, FORCE_SOURCES, PUBLICATION_INTEGRATION, PUBLICATION_GAPS
    if not _get_glm_key():
        log("[WARN] 兜底已禁用：未配置 GLM_API_KEY")
    _warn_if_fallback_looks_like_openrouter(_resolve_fallback_model(None))
    FORCE_ALL = "all" in args.force
    FORCE_SOURCES = set(args.force) - {"all"}
    PUBLICATION_INTEGRATION = True
    PUBLICATION_GAPS = []
    if FORCE_ALL or FORCE_SOURCES:
        log(
            "Force mode: "
            + ("ALL" if FORCE_ALL else "")
            + (f" sources={sorted(FORCE_SOURCES)}" if FORCE_SOURCES else "")
        )

    log(f"DailyInfo Pipeline Runner - {DATE}")
    log(f"Project root: {PROJECT_ROOT}")
    log(f"Briefings dir: {BRIEFINGS_DIR}")

    pipelines = {
        1: run_pipeline_papers,
        2: run_pipeline_ai_news,
        3: run_pipeline_arxiv,
        4: run_pipeline_code,
        5: run_pipeline_resource,
    }
    to_run = [args.pipeline] if args.pipeline else [1, 2, 3, 4, 5]
    total_saved = 0
    failed_pipelines = 0

    for p in to_run:
        try:
            total_saved += pipelines[p]()
        except Exception as e:
            failed_pipelines += 1
            log(f"Pipeline {p} FAILED: {e}")
            import traceback

            traceback.print_exc()

    log("=== Summary ===")
    for d in ["papers", "ai_news", "code", "resource", "arxiv"]:
        path = BRIEFINGS_DIR / d
        if path.exists():
            files = [f.name for f in sorted(path.iterdir()) if DATE in f.name]
            log(f'  {d}/: {len(files)} today - {", ".join(files)}')

    log(f"Total: {total_saved} files saved")
    if PUBLICATION_GAPS:
        log(
            "Canonical publication gaps: "
            + f"{len(PUBLICATION_GAPS)} - the parts that succeeded were "
            "published; these sources failed this run and are absent from what "
            "it published: " + "; ".join(PUBLICATION_GAPS)
        )
    return (
        0 if total_saved > 0 and failed_pipelines == 0 and not PUBLICATION_GAPS else 1
    )


if __name__ == "__main__":
    sys.exit(main())
