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


def _capture_logs() -> list[str]:
    logs: list[str] = []
    rp.log = lambda msg: logs.append(msg)  # type: ignore[assignment]
    return logs


def main() -> int:
    logs = _capture_logs()

    print("primary  :", rp.call_ai.__defaults__[0], "->", rp.DEEPSEEK_API_URL)
    print("fallback :", rp._resolve_fallback_model(None), "->", rp.GLM_API_URL)
    print("=" * 68)

    print("[1] primary, live")
    primary = rp.call_ai(PROMPT, max_tokens=MAX_TOKENS)
    fell_back = any("switching to fallback" in m for m in logs)
    print(f"    {len(primary)} chars, fallback consulted: {fell_back}")
    print("   ", primary[:110])

    print("\n[2] fallback, primary made unreachable")
    logs.clear()
    rp.DEEPSEEK_API_URL = "http://127.0.0.1:9/v1/chat/completions"
    rp.time.sleep = lambda *_: None  # type: ignore[assignment]
    fallback = rp.call_ai(PROMPT, max_tokens=MAX_TOKENS)
    switched = any("switching to fallback" in m for m in logs)
    print(f"    {len(fallback)} chars, fallback consulted: {switched}")
    print("   ", fallback[:110])

    checks = (
        ("primary answers on its own", bool(primary) and not fell_back),
        ("fallback takes over", bool(fallback) and switched),
    )
    print()
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
