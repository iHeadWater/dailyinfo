"""Tests for ``scripts/backfill_push.py``."""

from __future__ import annotations


def test_ai_failure_is_logged_without_the_credential(monkeypatch):
    import backfill_push as bp

    logs: list[str] = []
    monkeypatch.setattr(bp, "log", lambda msg: logs.append(msg))

    def boom(*args, **kwargs):
        raise RuntimeError("auth header rejected: Bearer sk-super-secret")

    monkeypatch.setattr(bp, "call_ai", boom)

    assert bp._generate_briefing("nature", "prompt", "sk-super-secret") is None

    joined = "\n".join(logs)
    assert "AI call failed" in joined, logs
    assert "sk-super-secret" not in joined, joined


def test_a_successful_generation_is_returned_unchanged(monkeypatch):
    import backfill_push as bp

    monkeypatch.setattr(bp, "call_ai", lambda prompt, key: "1. **A**\n   > 摘要。")

    assert (
        bp._generate_briefing("nature", "prompt", "sk-key") == "1. **A**\n   > 摘要。"
    )
