"""Best-effort extraction of author affiliations from paper sources.

Publisher pages are preferred when they expose affiliation metadata.  Most
conference pages only expose authors, so the first page of the PDF is used as
the common fallback.  Extraction is deliberately conservative: an empty
list is better than presenting a guessed institution as fact.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup


_AFFILIATION_SELECTORS = (
    "[itemprop='affiliation']",
    "[itemprop='author-affiliation']",
    "meta[name='citation_author_institution']",
    "meta[name='citation_author_affiliation']",
    ".affiliation",
    ".affiliations",
    ".author-affiliation",
    ".author_affiliation",
    "[class*='affiliation']",
    "[id*='affiliation']",
)
_AFFILIATION_HINT = re.compile(
    r"\b(?:university|institute|institution|college|school|laborator(?:y|ies)|lab|"
    r"research center|centre|department|academy|hospital|technology|corp\.?|inc\.?|"
    r"\beth\b|imec|ugent|inria|cnrs|epfl|caltech|mit|cmu)\b",
    re.I,
)


def _clean(value: Any) -> str:
    text = unescape(str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" \t\r\n;,*")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = _clean(value)
        if not value or len(value) < 3:
            continue
        key = re.sub(r"\W+", " ", value.casefold()).strip()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def extract_affiliations_from_html(html: str) -> list[str]:
    """Extract explicit affiliation metadata from a publisher detail page."""

    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    values: list[str] = []
    for node in soup.select(",".join(_AFFILIATION_SELECTORS)):
        value = (
            node.get("content")
            if node.name == "meta"
            else node.get_text(" ", strip=True)
        )
        value = _clean(value)
        if value and len(value) <= 240 and (
            _AFFILIATION_HINT.search(value) or node.name == "meta"
        ):
            values.append(value)
    return _dedupe(values)


def extract_affiliations_from_pdf(pdf_bytes: bytes) -> list[str]:
    """Extract likely institution lines from the first two PDF pages."""

    if not pdf_bytes:
        return []
    try:
        import pymupdf as fitz  # type: ignore

        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            text = "\n".join(
                document[index].get_text("text", sort=True)
                for index in range(min(len(document), 2))
            )
        finally:
            document.close()
    except Exception:
        return []

    # Affiliations normally occur between the author line and Abstract.  Do
    # not scan the full article, where institution names in references would
    # produce false positives.
    head = re.split(r"\b(?:abstract|keywords?)\b", text, maxsplit=1, flags=re.I)[0]
    candidates: list[str] = []
    for line in head.splitlines():
        line = _clean(line)
        line = re.sub(r"^[\d*†‡§]+\s*", "", line)
        line = re.sub(r"^[*†‡§]+\s*", "", line)
        if not line or len(line) > 240 or "@" in line:
            continue
        if _AFFILIATION_HINT.search(line):
            # Two-column PDF extraction often merges numbered affiliations
            # into one line: ``1 University A  2 Institute B``. Split those
            # markers while keeping the institution text intact.
            pieces = re.split(r"(?<!\w)(?:[1-9]\d?)\s+(?=[A-Z])", line)
            candidates.extend(
                piece for piece in pieces if _AFFILIATION_HINT.search(piece)
            )
    return _dedupe(candidates)
