"""Keep credentials out of log lines.

Every script here talks to an authenticated endpoint and logs its failures,
and the exception text can carry the credential: ``requests``' InvalidHeader
embeds the whole header value, and a provider's 401 body may echo a masked
tail. These helpers are the one place that decides what a log line may
contain, so a fix lands everywhere instead of in whichever script happened
to be edited.
"""

from __future__ import annotations

import re

import requests

# Providers sometimes echo a masked tail: "Your api key: ****abcd is invalid".
_MASK = "****"

# Longest provider-controlled excerpt that reaches a log line.
BODY_EXCERPT_CHARS = 200


def redact(text: str, secret: str) -> str:
    """Replace a credential with a marker before it reaches the log."""
    if not secret:
        return text
    if secret in text:
        text = text.replace(secret, "***")
    if len(secret) >= 4:
        # One linear pass. A loop was O(n^2) on a body shaped "****" + tail * n,
        # which a provider can produce at will -- it knows the last four.
        text = re.sub(r"\*{4}(?:" + re.escape(secret[-4:]) + r")+", _MASK, text)
    return text


def one_line(text: str, secret: str) -> str:
    """Redact a credential and flatten every line break into one log line.

    ``splitlines`` covers the full set of boundaries (\\n, \\r, U+2028, U+2029,
    U+0085, \\v, \\f), so a provider-controlled string cannot forge log lines.
    """
    return " ".join(redact(text, secret).splitlines())


def http_error_detail(exc: requests.RequestException, secret: str) -> str:
    """One-line error summary: credential scrubbed, 4xx body excerpted.

    ``raise_for_status`` puts no response body in its message, so a rejected
    model name would otherwise surface as a bare status code. The body is
    redacted *before* it is cut, so a credential straddling the cut cannot
    survive as a prefix.
    """
    detail = str(exc)
    try:
        body = str(getattr(getattr(exc, "response", None), "text", "") or "").strip()
    except Exception:  # an unreadable body must not hide the error itself
        body = ""
    if body:
        detail = f"{detail} body={redact(body, secret)[:BODY_EXCERPT_CHARS]}"
    return one_line(detail, secret)
