#!/usr/bin/env python3
"""推送每日简报到 Discord 频道"""

import os
import requests
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import tempfile
import time
import shutil

from logsafe import BODY_EXCERPT_CHARS, one_line
from paths import BRIEFINGS_DIR, CURRENT_ENV, PUSHED_DIR, STATE_DIR, get_channel_id
from publication import (
    CorruptDeliveryStateError,
    CorruptPublicationError,
    DeliveryCoordinator,
    DeliveryStateStore,
    DiscordPublisher,
    PublicationStore,
    sanitize_error,
)

DISCORD_API = "https://discord.com/api/v10"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_JSON = os.path.join(PROJECT_ROOT, "config", "sources.json")
DISCORD_CONTENT_LIMIT = 2000
DISCORD_CHUNK_LIMIT = 1950

_ARXIV_MARKER = STATE_DIR / ".arxiv_generating"
_ARXIV_POLL_INTERVAL = 30  # seconds between checks
_ARXIV_MAX_WAIT = 1800  # 30 minutes total timeout
_DISCORD_RETRY_DELAYS = (2, 5, 10)


def _wait_for_arxiv_generation(date: str) -> None:
    """If arXiv generation is in progress, poll until completion or timeout."""
    if not _ARXIV_MARKER.exists():
        return

    try:
        marker_date = _ARXIV_MARKER.read_text(encoding="utf-8").strip()
    except Exception:
        marker_date = ""

    if marker_date and marker_date != date:
        log(f"  [arxiv] stale marker for {marker_date}, ignoring (today is {date})")
        return

    log(f"  [arxiv] generation in progress, waiting (up to {_ARXIV_MAX_WAIT}s)...")
    waited = 0
    while _ARXIV_MARKER.exists() and waited < _ARXIV_MAX_WAIT:
        time.sleep(_ARXIV_POLL_INTERVAL)
        waited += _ARXIV_POLL_INTERVAL
        log(f"  [arxiv] still waiting... ({waited}s)")

    if _ARXIV_MARKER.exists():
        log(f"  [arxiv] timeout after {_ARXIV_MAX_WAIT}s, proceeding anyway")
    else:
        log(f"  [arxiv] generation finished after ~{waited}s")


def log(msg):
    """输出日志（附带当前环境标记）"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [env:{CURRENT_ENV}] {msg}", flush=True)


def _load_env_value(key):
    """Read a key from the environment or project .env, returning '' if missing."""
    val = os.environ.get(key, "")
    if val:
        return val
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return ""
    try:
        from dotenv import dotenv_values

        return dotenv_values(env_path).get(key, "") or ""
    except ImportError:
        prefix = f"{key}="
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line.startswith(prefix):
                    continue
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


DISCORD_BOT_TOKEN = _load_env_value("DISCORD_BOT_TOKEN")
if not DISCORD_BOT_TOKEN or "your_" in DISCORD_BOT_TOKEN:
    log("❌ 错误：DISCORD_BOT_TOKEN 未设置或仍是占位符")
    exit(1)

# Channel IDs are resolved per-category using the env-aware config module.
# In dev/staging environments, the keys are suffixed (e.g. DISCORD_CHANNEL_PAPERS_DEV).
# Missing entries cause that category to be skipped at push time, not a fatal error.
DISCORD_CHANNELS = {
    category: get_channel_id(category)
    for category in ("papers", "ai_news", "code", "resource", "arxiv", "weekly")
}
# arxiv shares the ai_news Discord channel
if not DISCORD_CHANNELS.get("arxiv"):
    DISCORD_CHANNELS["arxiv"] = DISCORD_CHANNELS.get("ai_news")

log(
    f"环境: {CURRENT_ENV}  频道映射: { {k: (v or '(未配置)') for k, v in DISCORD_CHANNELS.items()} }"
)


def _today() -> str:
    """Return today's date string (YYYY-MM-DD), evaluated at call time."""
    return datetime.now().strftime("%Y-%m-%d")


# Module-level default kept for backwards compat with tooling that may read it,
# but all code paths resolve the actual date via ``_today()`` or an explicit
# ``date`` argument so callers can backfill past days.
DATE = _today()


def split_message(content, max_length=DISCORD_CHUNK_LIMIT):
    """Split long content into Discord-sized message bodies."""
    if len(content) <= max_length:
        return [content]

    messages = []
    current = ""

    for line in content.split("\n"):
        if len(line) > max_length:
            if current:
                messages.append(current)
                current = ""
            for start in range(0, len(line), max_length):
                messages.append(line[start : start + max_length])
            continue
        if len(current) + len(line) + 1 > max_length:
            if current:
                messages.append(current)
            current = line
        else:
            if current:
                current += "\n" + line
            else:
                current = line

    if current:
        messages.append(current)

    return messages


def _chunk_prefix(index, total):
    """Return the prefix added to chunked Discord messages."""
    return f"【第 {index}/{total} 部分】\n\n"


def split_discord_messages(content):
    """Split content while reserving room for chunk prefixes."""
    messages = split_message(content, DISCORD_CHUNK_LIMIT)
    if len(messages) <= 1:
        return messages

    # Re-split with the exact prefix budget once the chunk count is known.
    total = len(messages)
    prefix_budget = len(_chunk_prefix(total, total))
    max_body_length = DISCORD_CONTENT_LIMIT - prefix_budget
    messages = split_message(content, max_body_length)

    # If digit growth changed the total, split once more with the final budget.
    total = len(messages)
    prefix_budget = len(_chunk_prefix(total, total))
    max_body_length = DISCORD_CONTENT_LIMIT - prefix_budget
    return split_message(content, max_body_length)


def _post_single_message(channel_id, headers, data, chunk_index):
    """向 Discord 发送一条消息，网络失败时指数退避重试。

    Returns:
        True: 发送成功
        False: 重试耗尽或收到不可重试的 HTTP 错误
    """
    last_err = None
    for attempt, delay in enumerate(_DISCORD_RETRY_DELAYS + (None,), start=1):
        try:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                log(f"  ✅ 第 {chunk_index} 部分发送成功")
                time.sleep(0.5)
                return True
            # 429 Rate limit — honour Retry-After
            if resp.status_code == 429:
                wait = float(
                    resp.json().get(
                        "retry_after",
                        delay if delay is not None else _DISCORD_RETRY_DELAYS[-1],
                    )
                )
                log(f"  ⏳ 触发限速，等待 {wait:.1f}s 后重试 (第 {attempt} 次)")
                time.sleep(wait)
                last_err = "429 rate limit"
                continue
            log(
                f"  ❌ 第 {chunk_index} 部分发送失败: {resp.status_code} - "
                f"{one_line(resp.text, DISCORD_BOT_TOKEN)[:BODY_EXCERPT_CHARS]}"
            )
            return False
        except Exception as e:
            # Sanitised once here: requests' InvalidHeader embeds the whole
            # header value, so a token with a stray CR/LF would be logged at
            # all three sites below.
            last_err = one_line(str(e), DISCORD_BOT_TOKEN)
            if delay is None:
                log(
                    f"  ❌ 发送错误（已重试 {len(_DISCORD_RETRY_DELAYS)} 次）: {last_err}"
                )
                return False
            log(f"  ⚠️  网络错误，{delay}s 后重试 (第 {attempt} 次): {last_err}")
            time.sleep(delay)
    # Exhausted all retries (pure 429 exhaustion — unlikely but safe)
    log(f"  ❌ 第 {chunk_index} 部分重试耗尽: {last_err}")
    return False


def send_to_discord(channel_id, content):
    """发送消息到 Discord 频道，网络失败时最多重试 3 次（指数退避）"""
    messages = split_discord_messages(content)

    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com/dailyinfo, 1.0)",
    }

    for i, msg in enumerate(messages):
        if len(messages) > 1:
            msg = f"{_chunk_prefix(i + 1, len(messages))}{msg}"
        data = {
            "content": msg,
            # Briefing text originates from external feeds; never let it
            # resolve into real pings.
            "allowed_mentions": {"parse": []},
        }

        if not _post_single_message(channel_id, headers, data, i + 1):
            return False

    return True


def _legacy_files(category, date):
    """Return legacy pending Markdown files for a category/date."""
    category_dir = BRIEFINGS_DIR / category
    if not category_dir.is_dir():
        return []
    return [
        path
        for path in sorted(category_dir.iterdir())
        if path.is_file() and date in path.name
    ]


def _legacy_archive_has_briefing(category, date):
    """Return whether ``pushed/`` contains a historical briefing for a day."""
    category_dir = PUSHED_DIR / category
    try:
        if not category_dir.is_dir():
            return False
        return any(
            path.is_file() and date in path.name for path in category_dir.iterdir()
        )
    except OSError as exc:
        # An unreadable archive reads as "no archive": the direction that
        # attempts delivery instead of silently skipping it.
        log(
            f"  ⚠️  无法读取归档目录 {category_dir.name}: "
            f"{one_line(str(exc), DISCORD_BOT_TOKEN)}"
        )
        return False


def _legacy_has_real_pending_file(category, date):
    """Detect a legacy delivery candidate that is not an empty/quality marker."""
    for path in _legacy_files(category, date):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Unreadable counts as a real candidate, like an OSError always
            # did: refusing the fallback is the safe direction, and raising
            # here would escape main()'s FileNotFoundError branch and kill
            # every later category.
            return True
        if not is_placeholder(content) and not is_low_quality_content(content):
            return True
    return False


def _archive_legacy_files(category, date):
    """Archive compatibility Markdown after canonical delivery succeeds.

    Canonical publication and delivery state are already authoritative.  This
    function only maintains the old archive and returns local compatibility
    errors so a failed move cannot cause another Discord send.
    """
    errors = []
    destination_dir = PUSHED_DIR / category
    for source_path in _legacy_files(category, date):
        try:
            content = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # A leftover that cannot be read at all is reported and left in
            # place; the delivery already happened, so it must not fail the
            # day or take the files around it down with it.
            log(
                f"  ⚠️  跳过无法读取的旧文件 {source_path}"
                f"（留在原地，不会重试也不会自动清理）: "
                f"{one_line(str(exc), DISCORD_BOT_TOKEN)}"
            )
            continue
        try:
            if is_placeholder(content) or is_low_quality_content(content):
                source_path.unlink()
                continue
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination_path = destination_dir / source_path.name
            if destination_path.exists():
                source_path.unlink()
            else:
                shutil.move(str(source_path), str(destination_path))
        except OSError as exc:
            errors.append(f"{source_path.name}: {exc}")
    return errors


def publish_canonical_category(
    category,
    channel_id,
    date=None,
    *,
    publication_store=None,
    delivery_store=None,
    force=False,
):
    """Deliver one canonical category/date briefing to Discord.

    ``FileNotFoundError`` means no canonical publication exists for the
    requested date.  Corrupt canonical data is never downgraded to legacy
    Markdown.  The returned tuple is ``(PublishResult, archive_errors)``.
    """
    date = date or _today()
    publication_store = publication_store or PublicationStore()
    delivery_store = delivery_store or DeliveryStateStore()
    briefing_id = f"{category}-{date}"
    bundle = publication_store.load_bundle(briefing_id)
    publisher = DiscordPublisher(
        channel_id,
        transport=send_to_discord,
    )
    result = DeliveryCoordinator(delivery_store).publish(
        bundle,
        publisher,
        force=force,
        legacy_delivered=_legacy_archive_has_briefing(category, date),
    )
    archive_errors = (
        _archive_legacy_files(category, date)
        if result.status in {"success", "skipped"}
        else []
    )
    return result, archive_errors


def is_placeholder(content):
    """Return True when content is a short no-update placeholder."""
    # Placeholders only contain the no-update notice generated by run_pipelines.
    return "📭 过去" in content and "无新内容" in content and len(content.strip()) < 200


def is_low_quality_content(content):
    """Return True for extremely short non-Chinese content."""
    stripped = content.strip()

    if len(stripped) < 100 and not any("一" <= c <= "鿿" for c in stripped):
        return True

    return False


# Categories that post a per-source summary after a fresh delivery.
SUMMARY_CATEGORIES = ("papers",)

# The failure notices ``run_pipelines`` writes.  The no-update notice is
# already covered by ``is_placeholder``; these mirror the non-zero entries of
# ``_PLACEHOLDER_MARKERS`` there and must stay in sync with it.
_FAILURE_MARKERS = (
    ("⚠️ 获取失败", "fetch_failed"),
    (
        "⚠️ 以下文章 AI 摘要生成失败",
        "generation_failed",
    ),
    ("⚠️ AI 生成失败", "generation_failed"),
)


def failure_reason(content):
    """Return the failure reason a run notice records, or None."""
    for marker, reason in _FAILURE_MARKERS:
        if marker in content:
            return reason
    return None


@dataclass(frozen=True)
class SourceStatus:
    """One configured source's outcome for a category/day."""

    name: str
    display_name: str
    status: str  # "pushed" | "no_update" | "failed" | "missing"
    reason: str | None = None

    def to_dict(self):
        return {
            "name": self.name,
            "display_name": self.display_name,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SourceStatusRecord:
    """The source-status sidecar as read back: rows plus summary state."""

    statuses: list
    summary_posted: bool


def _read_source_status_record(category, date):
    """The recorded source list and whether its summary went out, or None."""
    try:
        payload = json.loads(source_status_path(category, date).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    rows = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    statuses = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or row.get("status") not in ("pushed", "no_update", "failed", "missing")
        ):
            # A record that cannot be trusted is not used at all: a bad row
            # would otherwise surface as a mislabeled delivery failure or
            # silently drop a source from every bucket.
            return None
        statuses.append(
            SourceStatus(
                row["name"],
                row.get("display_name", row["name"]),
                row["status"],
                row.get("reason"),
            )
        )
    return SourceStatusRecord(statuses, bool(payload.get("summary_posted")))


def collect_source_status(category, date, *, publication_store=None):
    """Classify every enabled source of ``category`` for ``date``.

    ``pushed`` comes from the canonical bundle, which is authoritative and
    recomputable; "no_update" and "failed" exist only as the day's Markdown
    files, which the archive deletes -- so this must run before
    ``publish_canonical_category``.  A low-frequency source the run skipped
    inside its lookback window counts as ``no_update``, matching the run's own
    reason for writing nothing.  Raises ``FileNotFoundError`` when the day has
    no canonical briefing, like the delivery path it precedes.
    """
    publication_store = publication_store or PublicationStore()
    bundle = publication_store.load_bundle(f"{category}-{date}")

    pushed = {item.source.name for item in bundle.items}
    sources, defaults = _load_category_config(category)
    default_lookback = defaults.get("lookback_hours", 24)
    no_update: set[str] = set()
    failed: dict[str, str] = {}
    for path in _legacy_files(category, date):
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log(
                f"  ⚠️  读取 {path.name} 失败，该源将按缺失计入总结: "
                f"{one_line(str(exc), DISCORD_BOT_TOKEN)}"
            )
            continue
        name = _source_name_from_filename(path.name, sources)
        reason = failure_reason(content)
        if reason is not None:
            failed.setdefault(name, reason)
        elif is_placeholder(content):
            no_update.add(name)

    statuses = []
    for source in sources:
        name = source["name"]
        display_name = source.get("display_name", name)
        if name in pushed:
            statuses.append(SourceStatus(name, display_name, "pushed"))
        elif name in failed:
            statuses.append(SourceStatus(name, display_name, "failed", failed[name]))
        elif name in no_update:
            statuses.append(SourceStatus(name, display_name, "no_update"))
        else:
            lookback = source.get("lookback_hours", default_lookback)
            if lookback > 24 and _pushed_within_lookback(category, name, lookback):
                statuses.append(SourceStatus(name, display_name, "no_update"))
            else:
                statuses.append(SourceStatus(name, display_name, "missing"))
    return statuses


def build_source_summary(category, date, statuses):
    """Render the per-source summary through ``build_push_summary``."""
    return build_push_summary(
        category,
        date,
        [s.name for s in statuses if s.status == "pushed"],
        [s.name for s in statuses if s.status == "no_update"],
        failed_names=[s.name for s in statuses if s.status == "failed"],
    )


def source_status_path(category, date):
    """Return the sidecar path for one category/day."""
    return STATE_DIR / "source_status" / f"{category}-{date}.json"


def _write_json_atomic(path, value):
    """Write JSON through a same-directory temp file and an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_source_status_sidecar(
    category, date, statuses, *, path=None, summary_posted=False
):
    """Persist the day's source-status list for the future Web sink.

    Written only by the run that delivered, while the day's evidence is still
    on disk.  It is the snapshot of that run: a later ``resume`` merge adds
    content to the briefing without updating this file.  ``summary_posted``
    records whether the Discord summary went out, which is what lets a later
    forced redelivery repair one that never did.  Returns False after logging
    when the file cannot be written, so the caller reports it the way it
    reports an archive error.
    """
    path = path or source_status_path(category, date)
    payload = {
        "schema_version": 1,
        "category": category,
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary_posted": bool(summary_posted),
        "counts": {
            "configured": len(statuses),
            "pushed": sum(s.status == "pushed" for s in statuses),
            "no_update": sum(s.status == "no_update" for s in statuses),
            "failed": sum(s.status == "failed" for s in statuses),
            "missing": sum(s.status == "missing" for s in statuses),
        },
        "sources": [status.to_dict() for status in statuses],
    }
    try:
        _write_json_atomic(path, payload)
    except OSError as exc:
        log(f"  ⚠️  源状态文件写入失败: {one_line(str(exc), DISCORD_BOT_TOKEN)}")
        return False
    return True


def post_source_summary(category, date, channel_id, statuses):
    """Post the per-source summary after a fresh delivery, then persist the
    day's source list.  Returns the errors the caller counts."""
    if not statuses:
        # No readable source config (or none enabled): the day cannot be
        # described at all.  Silence here would leave the delivery marked
        # done with no summary and no record anywhere.
        return ["来源配置不可读，来源总结已跳过"]
    errors: list[str] = []
    posted = False
    summary = build_source_summary(category, date, statuses)
    if summary:
        if send_to_discord(channel_id, summary):
            posted = True
            log(f"  ✓ {category} 来源总结已发送")
        else:
            errors.append("来源总结发送失败")
            log(
                f"  ❌ {category} 来源总结发送失败（正文已送达；"
                "可用 --force 重投递补发）"
            )
    elif statuses:
        # A non-empty record (a repair) that renders nothing means the config
        # became unreadable since the delivering run; that cannot be silent.
        # An empty scan is already reported by the guard above.
        errors.append("来源配置不可读，来源总结无法生成")
    if not write_source_status_sidecar(category, date, statuses, summary_posted=posted):
        errors.append("源状态文件写入失败")
    return errors


def _load_category_config(category):
    """Load one category's enabled sources plus the shared defaults.

    A config that parses but has the wrong shape counts as unreadable: it must
    report through the same failed count as a missing file, not vanish into a
    scan degradation.
    """
    try:
        with open(SOURCES_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError(f"top level must be an object, got {type(cfg).__name__}")
        sources = [
            source
            for source in cfg.get("sources", [])
            if source.get("category") == category and source.get("enabled", True)
        ]
        defaults = cfg.get("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
    except Exception as e:
        log(f"  ⚠️  读取 sources.json 失败，无法生成来源总结: {e}")
        return [], {}
    return sources, defaults


def _load_sources_by_category(category):
    """Load enabled sources for a category from config/sources.json."""
    return _load_category_config(category)[0]


def _pushed_within_lookback(category, name, lookback_hours):
    """Whether the archive holds this source inside its lookback window.

    Mirror of ``run_pipelines._already_pushed_within``, which is the reason a
    low-frequency source can legitimately produce no file today: the summary
    reads that as "no new content" rather than a missing briefing.
    """
    pushed_dir = PUSHED_DIR / category
    if not pushed_dir.is_dir():
        return False
    cutoff = time.time() - lookback_hours * 3600
    prefix = f"{name}_briefing_"
    for fpath in pushed_dir.iterdir():
        if fpath.name.startswith(prefix) and fpath.name.endswith(".md"):
            try:
                if fpath.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
    return False


def _source_name_from_filename(filename, sources):
    """Resolve a briefing filename back to a configured source name."""
    for source in sorted(
        sources, key=lambda src: len(src.get("name", "")), reverse=True
    ):
        name = source.get("name", "")
        if filename.startswith(f"{name}_briefing_"):
            return name
    return filename.split("_briefing_", 1)[0]


def _format_source_list(names, display_names):
    """Format source names with configured display names for Discord."""
    if not names:
        return "无"
    return "\n".join(f"- {display_names.get(name, name)} (`{name}`)" for name in names)


def build_push_summary(
    category,
    date,
    pushed_names,
    placeholder_names,
    pending_names=None,
    failed_names=None,
):
    """Build a deterministic per-category push summary message."""
    sources = _load_sources_by_category(category)
    if not sources:
        return ""

    configured_names = [source["name"] for source in sources]
    display_names = {
        source["name"]: source.get("display_name", source["name"]) for source in sources
    }
    pushed_set = set(pushed_names)
    placeholder_set = set(placeholder_names)
    pending_set = set(pending_names or [])
    failed_set = set(failed_names or [])

    no_update_names = [
        name
        for name in configured_names
        if name in placeholder_set
        and name not in pushed_set
        and name not in pending_set
        and name not in failed_set
    ]
    missing_names = [
        name
        for name in configured_names
        if name not in pushed_set
        and name not in placeholder_set
        and name not in pending_set
        and name not in failed_set
    ]
    failed_list = [
        name
        for name in configured_names
        if name in failed_set and name not in pushed_set
    ]

    # The header counts what the list below actually renders: a name that is
    # no longer configured (a stored record predating a config change) must
    # not inflate the count of sources the message shows.
    pushed_list = [n for n in configured_names if n in pushed_set]
    title = (
        "📊 论文频道推送总结"
        if category in ("papers", "arxiv")
        else f"📊 {category} 推送总结"
    )
    lines = [
        f"{title} ({date})",
        "",
        f"✅ 已推送期刊 ({len(pushed_list)}):",
        _format_source_list(pushed_list, display_names),
        "",
        f"📭 今日无文章更新 ({len(no_update_names)}):",
        _format_source_list(no_update_names, display_names),
    ]
    if failed_list:
        lines.extend(
            [
                "",
                f"⚠️ 抓取或摘要失败 ({len(failed_list)}):",
                _format_source_list(failed_list, display_names),
            ]
        )
    if missing_names:
        lines.extend(
            [
                "",
                f"⚠️ 未发现今日简报文件 ({len(missing_names)}):",
                _format_source_list(missing_names, display_names),
            ]
        )
    return "\n".join(lines)


def _cleanup_placeholder_files(filepaths):
    """Remove placeholder files after their no-update status has been reported."""
    for filepath in filepaths:
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, encoding="utf-8") as f:
                content = f.read()
            if is_placeholder(content):
                os.remove(filepath)
        except (OSError, UnicodeDecodeError) as e:
            log(f"  ⚠️  清理 {os.path.basename(filepath)} 出错: {e}")


def push_category(category, channel_id, date=None, *, strict=False):
    """Push every briefing for ``category`` whose filename contains ``date``.

    Args:
        category: Briefing category name (e.g. "papers").
        channel_id: Target Discord channel id.
        date: Date string (YYYY-MM-DD). Defaults to today when omitted so
            existing callers keep working; pass an older date to backfill.
    """
    date = date or _today()
    category_dir = os.path.join(BRIEFINGS_DIR, category)

    if not os.path.exists(category_dir):
        log(f"  ⚠️  {category} 目录不存在")
        return 0

    if category == "arxiv":
        _wait_for_arxiv_generation(date)

    files = [f for f in sorted(os.listdir(category_dir)) if date in f]

    if not files:
        log(f"  ℹ️  {category} 中没有 {date} 的文件，发送无内容提醒")
        notice = f"📭 **{category}** 频道：{date} 暂无新简报"
        sent = send_to_discord(channel_id, notice)
        if strict and not sent:
            raise RuntimeError(f"Discord notice failed for {category} {date}")
        return 0

    log(f"  发现 {len(files)} 份文件...")

    # Keep real briefing files separate from placeholders used for status.
    valid_files = []
    sources = _load_sources_by_category(category)
    placeholder_names = []
    placeholder_paths = []
    pending_names = []
    pushed_names = []
    placeholder_count = 0
    low_quality_count = 0

    for filename in files:
        filepath = os.path.join(category_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if is_placeholder(content):
                placeholder_count += 1
                placeholder_names.append(_source_name_from_filename(filename, sources))
                placeholder_paths.append(filepath)
                log(f"    ⊘ {filename} (无内容，待汇总后清理)")
            elif is_low_quality_content(content):
                low_quality_count += 1
                # Drop low-quality files because they cannot produce useful status.
                os.remove(filepath)
                log(f"    ⊘ {filename} (低质量内容，已删除)")
            else:
                valid_files.append((filename, filepath, content))
        except Exception as e:
            log(f"  ❌ 读取 {filename} 出错: {e}")

    if valid_files:
        log(
            f"  有效文件: {len(valid_files)} 份，空内容: {placeholder_count} 份，低质量: {low_quality_count} 份"
        )
        log("  开始推送...")
    else:
        total_filtered = placeholder_count + low_quality_count
        log(
            f"  全部被过滤 (空内容: {placeholder_count}, 低质量: {low_quality_count}, 共 {total_filtered} 份)，发送无内容提醒"
        )
        summary = (
            build_push_summary(category, date, [], placeholder_names)
            if category in ("papers", "arxiv")
            else ""
        )
        sent = False
        if summary and send_to_discord(channel_id, summary):
            sent = True
            _cleanup_placeholder_files(placeholder_paths)
        else:
            notice = f"📭 **{category}** 频道：{date} 各源均无新内容"
            if send_to_discord(channel_id, notice):
                sent = True
                _cleanup_placeholder_files(placeholder_paths)
        if strict and not sent:
            raise RuntimeError(
                f"Discord empty-content notice failed for {category} {date}"
            )
        return 0

    pushed_count = 0
    delivery_failed = False
    for filename, filepath, content in valid_files:
        try:
            # Send the real briefing before archiving it.
            if send_to_discord(channel_id, content):
                # Move only successfully sent files to the pushed archive.
                pushed_category_dir = os.path.join(PUSHED_DIR, category)
                os.makedirs(pushed_category_dir, exist_ok=True)

                dest_path = os.path.join(pushed_category_dir, filename)
                shutil.move(filepath, dest_path)

                log(f"    ✓ {filename} 推送完成")
                pushed_count += 1
                pushed_names.append(_source_name_from_filename(filename, sources))
                time.sleep(1)  # Avoid sending files back-to-back too quickly.
            else:
                delivery_failed = True
                log(f"    ✗ {filename} 推送失败，保留原位")
                pending_names.append(_source_name_from_filename(filename, sources))

        except Exception as e:
            log(f"  ❌ 处理 {filename} 出错: {e}")

    if category in ("papers", "arxiv"):
        summary = build_push_summary(
            category, date, pushed_names, placeholder_names, pending_names
        )
        if summary and send_to_discord(channel_id, summary):
            _cleanup_placeholder_files(placeholder_paths)
    else:
        _cleanup_placeholder_files(placeholder_paths)

    if strict and delivery_failed:
        raise RuntimeError(
            f"one or more Discord deliveries failed for {category} {date}"
        )
    return pushed_count


def _parse_date(value):
    """Validate and normalise a YYYY-MM-DD date string."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid --date value {value!r}; expected YYYY-MM-DD"
        ) from exc


ALL_CATEGORIES = ["papers", "ai_news", "code", "resource", "arxiv", "weekly"]
DAILY_CATEGORIES = ["papers", "ai_news", "code", "resource", "arxiv"]


def main(date=None, categories=None, force=False):
    date = date or _today()
    active = categories if categories is not None else DAILY_CATEGORIES

    log("=== Discord 推送开始 ===")
    log(f"日期: {date}")
    log(f"频道: {', '.join(active)}")

    delivered = 0
    skipped = 0
    failed = 0
    publication_store = PublicationStore()
    delivery_store = DeliveryStateStore()

    PUSH_ORDER = ["papers", "code", "resource", "ai_news", "arxiv", "weekly"]
    for category in PUSH_ORDER:
        if category not in active:
            continue
        channel_id = DISCORD_CHANNELS.get(category, "")
        if not channel_id:
            log(f"⚠️  {category} 未配置 DISCORD_CHANNEL_{category.upper()}，跳过")
            skipped += 1
            continue
        log(f"推送到 #{category}...")
        if category == "weekly":
            try:
                count = push_category(category, channel_id, date, strict=True)
                delivered += count
                if count == 0:
                    skipped += 1
                log(f"  legacy 小计: {count} 份文件")
            except Exception as exc:
                failed += 1
                log(f"  ❌ {category} legacy delivery failed: {sanitize_error(exc)}")
            continue
        try:
            statuses = None
            already_archived = False
            if category in SUMMARY_CATEGORIES:
                # The freshness marks come first: the delivery below archives
                # the day's files, which are the only record of the sources
                # that produced nothing.
                already_archived = _legacy_archive_has_briefing(category, date)
                if _legacy_files(category, date):
                    # Fresh files mean the day was (re)generated after any
                    # archive; the archive alone must not suppress the summary.
                    already_archived = False
                try:
                    statuses = collect_source_status(
                        category, date, publication_store=publication_store
                    )
                except FileNotFoundError:
                    raise
                except Exception as exc:
                    # The summary is auxiliary; a scan failure must not cost
                    # the day's delivery -- but it is still a failure.
                    log(
                        f"  ⚠️  {category} 来源状态扫描失败，跳过来源总结: "
                        f"{one_line(str(exc), DISCORD_BOT_TOKEN)}"
                    )
                    statuses = None
                    failed += 1
            result, archive_errors = publish_canonical_category(
                category,
                channel_id,
                date,
                publication_store=publication_store,
                delivery_store=delivery_store,
                force=force,
            )
            if result.status == "success":
                delivered += 1
                log(f"  ✓ {category} canonical briefing delivered")
            elif result.status == "skipped":
                skipped += 1
                log(f"  ⊘ {category} already delivered (no Discord call)")
            else:
                # A transport failure is not a delivery: counting it as
                # "skipped" reported the day as delivered and exited 0.
                failed += 1
                detail = f": {result.error}" if result.error else ""
                log(f"  ❌ {category} canonical delivery failed{detail}")
            if archive_errors:
                failed += 1
                log(
                    f"  ❌ {category} legacy archive compatibility failed: "
                    + "; ".join(archive_errors)
                )
            if result.status == "success" and category in SUMMARY_CATEGORIES:
                # Only the run that delivered posts the summary, and only
                # while the day's evidence is still on disk: an archived day
                # (a forced redelivery) would re-scan as "missing" for every
                # source without a bundle entry.
                if not already_archived:
                    if statuses is not None:
                        summary_errors = post_source_summary(
                            category, date, channel_id, statuses
                        )
                        if summary_errors:
                            failed += 1
                            log(
                                f"  ❌ {category} 推送后续步骤失败: "
                                + "; ".join(summary_errors)
                            )
                else:
                    record = _read_source_status_record(category, date)
                    if record is None:
                        log(
                            f"  ⊘ {category} 无当日来源记录，跳过总结"
                            "（无法判断是否已发）"
                        )
                    elif record.summary_posted:
                        log(f"  ⊘ {category} 简报已归档（重投递），跳过来源总结")
                    else:
                        # Archived, yet the record says the summary never went
                        # out: rebuild it from that record.
                        log(f"  ↻ {category} 来源总结未发出，用当日记录补发")
                        summary_errors = post_source_summary(
                            category, date, channel_id, record.statuses
                        )
                        if summary_errors:
                            failed += 1
                            log(
                                f"  ❌ {category} 推送后续步骤失败: "
                                + "; ".join(summary_errors)
                            )
        except FileNotFoundError:
            # Before Phase 2C, pending Markdown was the only source of a
            # delivery candidate. Keep that path for legacy data while making
            # the canonical store authoritative whenever a bundle exists.
            if _legacy_archive_has_briefing(category, date):
                skipped += 1
                log(f"  ⊘ {category} historical pushed/ archive already covers {date}")
            elif _legacy_has_real_pending_file(category, date):
                failed += 1
                log(
                    f"  ❌ {category} has legacy pending Markdown for {date} "
                    "but no canonical Publication; refusing fallback delivery"
                )
            elif _legacy_files(category, date):
                try:
                    count = push_category(category, channel_id, date, strict=True)
                    if count:
                        delivered += count
                    else:
                        skipped += 1
                    log(f"  legacy 小计: {count} 份文件")
                except Exception as exc:
                    failed += 1
                    log(
                        f"  ❌ {category} legacy delivery failed: {sanitize_error(exc)}"
                    )
            else:
                # Preserve the existing no-file notice for categories that
                # have never produced a canonical or legacy briefing.
                try:
                    count = push_category(category, channel_id, date, strict=True)
                    skipped += 1
                    log(f"  no canonical briefing; legacy notice result={count}")
                except Exception as exc:
                    failed += 1
                    log(f"  ❌ {category} notice failed: {sanitize_error(exc)}")
        except (CorruptPublicationError, CorruptDeliveryStateError) as exc:
            failed += 1
            log(f"  ❌ {category} canonical delivery failed: {sanitize_error(exc)}")
        except Exception as exc:
            failed += 1
            log(f"  ❌ {category} canonical delivery failed: {sanitize_error(exc)}")

    log("=== 推送完成 ===")
    log(f"discord: delivered={delivered} skipped={skipped} failed={failed}")

    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Push daily briefings to Discord.")
    parser.add_argument(
        "--date",
        default=None,
        help="Date to push in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help=(
            "Comma-separated list of categories to push "
            "(e.g. 'papers,ai_news,code,resource' or 'weekly'). "
            f"Defaults to all: {','.join(ALL_CATEGORIES)}."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a new Discord attempt even when delivery state is success.",
    )
    args = parser.parse_args()

    resolved_date = _parse_date(args.date) if args.date else None
    resolved_cats = (
        [c.strip() for c in args.categories.split(",") if c.strip()]
        if args.categories
        else None
    )
    sys.exit(main(resolved_date, resolved_cats, args.force))
