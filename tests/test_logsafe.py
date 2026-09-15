"""Tests for ``scripts/logsafe.py`` — what a log line may contain."""

from __future__ import annotations

import requests

from logsafe import http_error_detail, redact


def test_redact_replaces_secret_and_tolerates_empty():

    assert redact("bearer sk-secret rejected", "sk-secret") == "bearer *** rejected"
    assert redact("nothing to hide", "") == "nothing to hide"
    assert redact("nothing to hide", "sk-absent") == "nothing to hide"


def test_redact_masks_provider_masked_key_tail():
    """DeepSeek echoes 'Your api key: ****<last4>'; that must not survive either."""

    redacted = redact("Your api key: ****abcd is invalid", "sk-0123456789abcd")

    assert "****abcd" not in redacted, redacted


def test_redact_masks_a_repeated_provider_tail_to_a_fixpoint():
    """One pass turns ****abcdabcdabcd into ****abcdabcd -- the tail survives."""

    redacted = redact("****abcdabcdabcd", "sk-0123456789abcd")

    assert "abcd" not in redacted, redacted


def test_http_error_detail_includes_response_body():

    class _FakeResponse:
        text = '{"error": {"message": "model not found"}}'

    exc = requests.HTTPError("400 Client Error")
    exc.response = _FakeResponse()

    detail = http_error_detail(exc, "sk-secret")

    assert "400 Client Error" in detail
    assert "model not found" in detail


def test_http_error_detail_redacts_before_truncating():
    """A secret straddling the 200-char cut must not survive as a prefix."""

    secret = "sk-AAAABBBBCCCCDDDDEEEEFFFF"
    body = "x" * 190 + secret  # 10 chars of the secret fall inside body[:200]

    class _FakeResponse:
        text = body

    exc = requests.HTTPError("400 Client Error")
    exc.response = _FakeResponse()

    detail = http_error_detail(exc, secret)

    assert secret not in detail
    assert "sk-AAAABBB" not in detail, detail


def test_http_error_detail_keeps_output_on_one_line():
    """A body with newlines must not be able to forge extra log lines."""

    class _FakeResponse:
        text = "oops\n[WARN] forged line"

    exc = requests.HTTPError("400 Client Error")
    exc.response = _FakeResponse()

    detail = http_error_detail(exc, "sk-secret")

    assert "\n" not in detail, detail
    assert len(detail.splitlines()) == 1, detail


def test_http_error_detail_flattens_unicode_line_breaks():
    """\\n and \\r are not the only line boundaries a body can carry."""

    class _FakeResponse:
        text = "a\u2028[WARN] forged\x85next\u2029end"

    exc = requests.HTTPError("400 Client Error")
    exc.response = _FakeResponse()

    detail = http_error_detail(exc, "sk-secret")

    assert len(detail.splitlines()) == 1, detail


def test_http_error_detail_survives_a_response_that_raises():

    class _ExplodingResponse:
        @property
        def text(self):
            raise RuntimeError("body unavailable")

    exc = requests.HTTPError("400 Client Error")
    exc.response = _ExplodingResponse()

    assert "400 Client Error" in http_error_detail(exc, "sk-secret")
