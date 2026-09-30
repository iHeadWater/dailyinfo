"""Check both halves of the model switch against the live APIs.

Manual, not CI: it spends real credentials and real money, so it is not
collected by pytest and not run by the workflow. Run it when the model
configuration changes, or when you want to know the fallback still works.

    uv run python tests/check_providers.py

The point is the second assertion in each half. A returned string proves
nothing on its own -- when the primary is broken the fallback answers, and
the output looks identical. What distinguishes them is the log line, so each
check asserts *which* provider answered.

Reading keys and endpoints from run_pipelines means this exercises the real
configuration; it writes nothing to disk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Before importing anything that resolves paths: the default data root is the
# production one.
os.environ.setdefault("DAILYINFO_DATA_ROOT", "/tmp/dailyinfo-provider-check")

import run_pipelines as rp  # noqa: E402

PROMPT = "用一句中文说明什么是水文模型。"
MAX_TOKENS = 200

_FILLER = "Test item: a model shipped, a tool changed, a paper landed. "


def _deep_content_cap() -> int:
    """The largest content cap configured for a use_content source."""
    cfg_path = REPO_ROOT / "config" / "sources.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    caps = [
        s.get("max_content_chars", 12000)
        for s in cfg["sources"]
        if s.get("use_content")
    ]
    return max(caps, default=12000)


def _deep_prompt(cap: int) -> str:
    """An article body of exactly *cap* chars, the size production permits."""
    body = (_FILLER * (cap // len(_FILLER) + 1))[:cap]
    return "以下是今天的AI新闻汇总，请按类别整理关键信息。\n\n" + body


def _call(prompt: str, max_tokens: int, logs: list[str]) -> str:
    """call_ai, with a total failure recorded instead of raised.

    When both providers fail, the raised message names the models but not the
    reason; the redacted provider text sits in *logs*, which is where the FAIL
    report reads it.
    """
    try:
        return rp.call_ai(prompt, max_tokens=max_tokens)
    except rp.BriefingGenerationError as exc:
        logs.append(f"raised: {exc}")
        return ""


def _capture_logs() -> list[str]:
    logs: list[str] = []
    rp.log = lambda msg: logs.append(msg)  # type: ignore[assignment]
    return logs


def main() -> int:
    logs = _capture_logs()
    real_ds_url = rp.DEEPSEEK_API_URL
    cap = _deep_content_cap()
    deep_prompt = _deep_prompt(cap)

    print("primary  :", rp.call_ai.__defaults__[0], "->", rp.DEEPSEEK_API_URL)
    print("fallback :", rp._resolve_fallback_model(None), "->", rp.GLM_API_URL)
    print("deep body:", cap, "chars (largest use_content cap in config)")
    print("=" * 68)

    print("[1] primary, live")
    primary = _call(PROMPT, MAX_TOKENS, logs)
    primary_logs = list(logs)
    fell_back = any("switching to fallback" in m for m in primary_logs)
    print(f"    {len(primary)} chars, fallback consulted: {fell_back}")
    print("   ", primary[:110])

    print("\n[2] fallback, primary made unreachable")
    logs.clear()
    rp.DEEPSEEK_API_URL = "http://127.0.0.1:9/v1/chat/completions"
    rp.time.sleep = lambda *_: None  # type: ignore[assignment]
    fallback = _call(PROMPT, MAX_TOKENS, logs)
    fallback_logs = list(logs)
    switched = any("switching to fallback" in m for m in fallback_logs)
    print(f"    {len(fallback)} chars, fallback consulted: {switched}")
    print("   ", fallback[:110])

    budget = rp._DEEP_CONTENT_MAX_TOKENS

    print(f"\n[3] deep-content budget ({budget}), primary live again")
    logs.clear()
    rp.DEEPSEEK_API_URL = real_ds_url
    deep_primary = _call(deep_prompt, budget, logs)
    deep_primary_logs = list(logs)
    deep_fell_back = any("switching to fallback" in m for m in deep_primary_logs)
    print(f"    {len(deep_primary)} chars, fallback consulted: {deep_fell_back}")

    print(f"\n[4] deep-content budget ({budget}), primary made unreachable")
    logs.clear()
    rp.DEEPSEEK_API_URL = "http://127.0.0.1:9/v1/chat/completions"
    deep_fallback = _call(deep_prompt, budget, logs)
    deep_fallback_logs = list(logs)
    deep_switched = any("switching to fallback" in m for m in deep_fallback_logs)
    print(f"    {len(deep_fallback)} chars, fallback consulted: {deep_switched}")

    checks = (
        ("primary answers on its own", bool(primary) and not fell_back, primary_logs),
        ("fallback takes over", bool(fallback) and switched, fallback_logs),
        (
            "deep-content budget answered by the primary",
            bool(deep_primary) and not deep_fell_back,
            deep_primary_logs,
        ),
        (
            "deep-content budget answered by the fallback",
            bool(deep_fallback) and deep_switched,
            deep_fallback_logs,
        ),
    )
    print()
    for label, ok, check_logs in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            # Already redacted and one-lined by logsafe; printing them is what
            # turns a FAIL into a diagnosis.
            for line in check_logs:
                print(f"        {line}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
