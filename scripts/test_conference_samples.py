#!/usr/bin/env python3
"""Run a bounded, idempotent conference-paper smoke test.

The production conference state is deliberately not touched.  A separate
SQLite state keeps the first ``--limit`` rendered events per source, while AI
responses are cached by prompt/model hash so reruns do not spend API calls on
the same summaries (or caption reviews).

Example::

    python scripts/test_conference_samples.py --limit 2 --push
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from conference import ConferenceState, run_conference_source  # noqa: E402
from paths import BRIEFINGS_DIR, STATE_DIR  # noqa: E402
from run_pipelines import (  # noqa: E402
    _load_sources,
    _resolve_conference_source,
    call_ai,
    call_vision_ai,
)


def _cache_key(kind: str, prompt: Any, model: str, max_tokens: int) -> str:
    if isinstance(prompt, list):
        serial = []
        for item in prompt:
            if isinstance(item, dict) and item.get("type") == "image_url":
                serial.append({"type": "image_url", "image_url": "<image>"})
            else:
                serial.append(item)
        prompt = serial
    payload = json.dumps(
        {"kind": kind, "model": model, "max_tokens": max_tokens, "prompt": prompt},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cached_call(
    cache_dir: Path,
    kind: str,
    func: Callable[..., str],
    prompt: Any,
    *,
    model: str,
    max_tokens: int,
    key_prompt: Any = None,
    **kwargs: Any,
) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{_cache_key(kind, prompt if key_prompt is None else key_prompt, model, max_tokens)}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            print(f"  [{kind}] cache hit {path.stem[:12]}", flush=True)
            return value["content"]
    except (OSError, json.JSONDecodeError):
        pass
    content = func(prompt, model=model, max_tokens=max_tokens, **kwargs)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"model": model, "content": content}, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    return content


def _cached_text_call(cache_dir: Path):
    def invoke(prompt: str, model: str = "deepseek-v4-pro", max_tokens: int = 1200, **kwargs: Any) -> str:
        return _cached_call(
            cache_dir,
            "summary",
            call_ai,
            prompt,
            model=model,
            max_tokens=max_tokens,
            **kwargs,
        )

    return invoke


def _cached_vision_call(cache_dir: Path):
    def invoke(
        prompt: str,
        images: list[bytes],
        model: str = "deepseek-v4-flash-vision-exp",
        max_tokens: int = 256,
    ) -> str:
        # Include image bytes in the cache key; otherwise different crops with
        # the same caption prompt could incorrectly share a decision.
        image_hashes = [hashlib.sha256(image).hexdigest() for image in images]
        cache_prompt = {"prompt": prompt, "images": image_hashes}
        return _cached_call(
            cache_dir,
            "vision",
            call_vision_ai,
            prompt,
            model=model,
            max_tokens=max_tokens,
            key_prompt=cache_prompt,
            images=images,
        )

    return invoke


def _skip_remaining_pending(state_dir: Path, source: str) -> int:
    """Mark unselected events as test-skipped in the isolated test DB."""

    db = ConferenceState(state_dir / "openreview.sqlite3")
    with db._connect() as conn:  # isolated test database, not production state
        cursor = conn.execute(
            "UPDATE events SET status='test_skipped' "
            "WHERE source=? AND status='pending'",
            (source,),
        )
        return int(cursor.rowcount)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=2, help="papers rendered per conference")
    parser.add_argument("--source", action="append", help="run only this source; repeatable")
    parser.add_argument("--date", default=None, help="briefing date, defaults to today")
    parser.add_argument(
        "--test-root",
        type=Path,
        default=STATE_DIR / "conference_sample_test",
        help="isolated state/cache root",
    )
    parser.add_argument(
        "--briefings-dir",
        type=Path,
        default=BRIEFINGS_DIR,
        help="briefings directory (use the default for Discord push)",
    )
    parser.add_argument("--push", action="store_true", help="push conference briefings to Discord")
    parser.add_argument(
        "--force",
        action="store_true",
        help="force discovery even when the isolated test source is not due",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    cfg, defaults, _ = _load_sources()
    date = args.date
    if not date:
        from datetime import datetime

        date = datetime.now().strftime("%Y-%m-%d")
    names = set(args.source or [])
    sources = [
        source
        for source in cfg.get("sources", [])
        if source.get("category") == "conference"
        and source.get("type") == "api"
        and source.get("enabled", True)
        # ACL is already covered by the production pipeline; keep this
        # bounded cross-conference smoke test focused on the other providers.
        and source.get("provider") != "acl"
        and (not names or source.get("name") in names)
    ]
    if not sources:
        print("No enabled conference sources selected.", file=sys.stderr)
        return 2

    test_root = args.test_root.expanduser()
    cache_dir = test_root / "ai_cache"
    state_dir = test_root / "state"
    cached_text = _cached_text_call(cache_dir)
    cached_vision = _cached_vision_call(cache_dir)
    total = 0
    for source in sources:
        resolved = _resolve_conference_source(source, defaults)
        resolved["max_events_per_briefing"] = args.limit
        resolved["max_relevant_papers"] = args.limit
        resolved["checkpoint"] = {
            **(resolved.get("checkpoint") or {}),
            "page_size": min(100, max(10, args.limit * 25)),
        }
        # CVF indexes contain thousands of detail pages. For a bounded smoke
        # test, retrieve listing metadata first and fetch details only for the
        # two selected candidates during FORUM_POLL.
        if resolved.get("provider") == "cvf":
            resolved["defer_detail_fetch"] = True
        print(f"[{source['name']}] sample run limit={args.limit}", flush=True)
        try:
            result = run_conference_source(
                resolved,
                defaults,
                cached_text,
                state_dir,
                args.briefings_dir.expanduser(),
                date,
                force=args.force,
                logger=lambda message: print(message, flush=True),
                call_vision_ai=cached_vision,
            )
        except Exception as exc:
            print(f"[{source['name']}] FAILED {type(exc).__name__}: {exc}", flush=True)
            continue
        skipped = _skip_remaining_pending(state_dir, source["name"])
        total += result.files_saved
        print(
            f"[{source['name']}] {result.outcome} relevant={result.relevant_papers} "
            f"events={result.events_created} saved={result.files_saved} skipped={skipped}",
            flush=True,
        )

    if args.push:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "push_to_discord.py"),
            "--date",
            date,
            "--categories",
            "conference",
        ]
        print("Running: " + " ".join(command), flush=True)
        return subprocess.run(command, cwd=PROJECT_ROOT).returncode
    print(f"Generated {total} conference briefing file(s); rerun with --push to send them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
