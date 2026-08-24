"""Shared phrase matching for keyword and exclusion filters.

``conference.py`` and ``paper_retrieval.py`` both filter papers with the same
normalisation and word-boundary rules. Keeping one copy matters for filter
semantics: a fix applied to only one of two identical matchers silently makes
the two pipelines disagree about what a configured phrase means.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def normalise_text(value: Any) -> str:
    """Casefold, NFKC-normalise and collapse whitespace.

    Accepts any value so callers can pass optional metadata fields directly.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def phrase_matches(text: str, phrase: str) -> bool:
    """Return True when *phrase* occurs in already-normalised *text*.

    Word boundaries prevent ``water`` from matching ``watermark``; phrases with
    punctuation still get escaped and matched literally. Note that the same
    rule stops ``watermark`` from matching ``watermarking``, so configuration
    has to list both forms when it means to catch both.
    """

    phrase = normalise_text(phrase)
    if not phrase:
        return False
    plural = (
        r"(?:s|es)?"
        if re.fullmatch(r"[a-z]+", phrase) and not phrase.endswith("s")
        else ""
    )
    return re.search(rf"(?<!\w){re.escape(phrase)}{plural}(?!\w)", text) is not None
