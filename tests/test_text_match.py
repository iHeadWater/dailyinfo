"""Tests for ``scripts/text_match.py``.

Both the arXiv and conference pipelines resolve configured phrases through
this module, so its word-boundary rules define what ``exclude_phrases`` and
``keywords`` mean in ``config/sources.json``.
"""

from __future__ import annotations


def test_normalise_text_accepts_non_string_values():
    from text_match import normalise_text

    assert normalise_text(None) == ""
    assert normalise_text(123) == "123"
    assert normalise_text("  Mixed   CASE\tspacing ") == "mixed case spacing"


def test_normalise_text_applies_nfkc_and_casefold():
    from text_match import normalise_text

    # Fullwidth and ligature forms have to collapse onto their ASCII spelling
    # or a configured phrase silently fails to match scraped metadata.
    assert normalise_text("ＬＳＴＭ") == "lstm"
    assert normalise_text("ﬁne-tuning") == "fine-tuning"


def test_phrase_matches_respects_word_boundaries():
    from text_match import normalise_text, phrase_matches

    text = normalise_text("Robust watermarking for large language models")

    # The boundary rule is why config/sources.json lists both spellings.
    assert phrase_matches(text, "watermarking")
    assert not phrase_matches(text, "watermark")
    assert not phrase_matches(normalise_text("watermark removal"), "water")


def test_phrase_matches_allows_simple_plurals():
    from text_match import normalise_text, phrase_matches

    assert phrase_matches(normalise_text("flood forecasts for basins"), "forecast")
    assert phrase_matches(normalise_text("many watersheds"), "watershed")
    # An explicitly plural phrase must not grow a second suffix.
    assert not phrase_matches(normalise_text("watershedses"), "watersheds")


def test_phrase_matches_escapes_punctuation_literally():
    from text_match import normalise_text, phrase_matches

    text = normalise_text("A physics-informed neural network")
    assert phrase_matches(text, "physics-informed")
    # Regex metacharacters must not be interpreted as a pattern.
    assert not phrase_matches(text, "physics.informed")


def test_phrase_matches_ignores_blank_phrases():
    from text_match import phrase_matches

    assert not phrase_matches("anything at all", "")
    assert not phrase_matches("anything at all", "   ")
