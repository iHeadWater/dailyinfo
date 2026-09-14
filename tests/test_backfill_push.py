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


def test_discord_send_error_is_logged_without_the_token(monkeypatch):
    """http.client.putheader raises with the whole header value."""
    import backfill_push as bp

    logs: list[str] = []
    monkeypatch.setattr(bp, "log", lambda msg: logs.append(msg))

    def boom(req, timeout=None):
        raise RuntimeError("Invalid header value b'Bot sk-bot-secret\\r\\n'")

    monkeypatch.setattr(bp.urllib.request, "urlopen", boom)

    assert bp.discord_send("sk-bot-secret", "chan-1", "hello") is False

    joined = "\n".join(logs)
    assert "Discord send failed" in joined, logs
    assert "sk-bot-secret" not in joined, joined
