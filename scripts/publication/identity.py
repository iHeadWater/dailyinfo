"""Stable identity resolution for source items and daily briefings."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_ARXIV_ID_RE = re.compile(r"(?<!\d)(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)", re.I)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
_DOI_PREFIX_RE = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", re.I)
_REPO_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"
)


def briefing_id(category: str, publication_date: str) -> str:
    """Return the only allowed identity for a category/date briefing."""

    return f"{category}-{publication_date}"


def canonicalize_source_url(url: str) -> str:
    """Canonicalize a public URL for deterministic URL-derived identities.

    Fragments and common analytics parameters are not publication identity.
    Other query parameters are retained and sorted because they may identify a
    real article endpoint.
    """

    parsed = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    hostname = (parsed.hostname or "").lower()
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), "")
    )


def source_namespace(source_name: str) -> str:
    """Return a stable machine namespace from a configured source key.

    Source names are configuration identifiers in the current adapter API,
    but callers may spell separators differently.  Removing separators makes
    ``OpenReview``, ``Open Review`` and ``openreview`` one namespace.  arXiv
    source aliases share the globally stable ``arxiv`` namespace.
    """

    normalized = re.sub(r"[^a-z0-9]", "", source_name.lower())
    if normalized.startswith("arxiv"):
        return "arxiv"
    return normalized or "source"


def normalize_arxiv_id(value: Optional[str]) -> Optional[str]:
    """Return the base arXiv identifier, intentionally dropping ``vN``."""

    if not isinstance(value, str) or not value.strip():
        return None
    match = _ARXIV_ID_RE.search(value.strip())
    return match.group(1) if match else None


def normalize_doi(value: Optional[str]) -> Optional[str]:
    """Return a conservative, case-folded DOI representation."""

    if not isinstance(value, str) or not value.strip():
        return None
    candidate = _DOI_PREFIX_RE.sub("", value.strip()).rstrip(".,;)")
    if not _DOI_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def normalize_repo_id(value: Optional[str]) -> Optional[str]:
    """Return a conservative ``namespace/name`` repository identity."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _REPO_ID_RE.fullmatch(candidate) else None


def _repo_id_from_url(value: str) -> Optional[str]:
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    return normalize_repo_id("/".join(parts[-2:]))


def normalize_external_id(
    *, source_name: str, source_url: str, external_id: Optional[str]
) -> Optional[str]:
    """Normalize known upstream identities without inventing one.

    A malformed known identity is discarded so the resolver deterministically
    falls back to the canonical public URL.  Unknown source identities remain
    unchanged after whitespace trimming.
    """

    raw = external_id.strip() if isinstance(external_id, str) else None
    source_lower = source_name.lower()
    if "arxiv" in source_lower:
        return normalize_arxiv_id(raw) or normalize_arxiv_id(source_url)

    if "github" in source_lower or "huggingface" in source_lower:
        return normalize_repo_id(raw) or _repo_id_from_url(source_url)

    is_doi_source = (
        "crossref" in source_lower
        or "api.crossref.org" in source_url.lower()
        or bool(raw and _DOI_PREFIX_RE.match(raw))
        or bool(raw and _DOI_RE.match(raw))
    )
    doi = normalize_doi(raw) or normalize_doi(source_url)
    if doi is not None:
        return doi
    if is_doi_source:
        return None
    return raw


def resolve_item_id(
    *,
    source_name: str,
    source_url: str,
    external_id: Optional[str] = None,
    explicit_id: Optional[str] = None,
) -> str:
    """Resolve a stable id without using title, summary, or timestamps.

    Explicit ids are trusted as already-canonical input and validated later.
    Known source identities get readable ids where that is unambiguous.  All
    other stable external identities and URLs use a SHA-256 digest of that
    stable identity, never of mutable content.
    """

    if explicit_id:
        return explicit_id

    namespace = source_namespace(source_name)
    stable_external = (
        normalize_external_id(
            source_name=source_name,
            source_url=source_url,
            external_id=external_id,
        )
        or ""
    )
    source_lower = source_name.lower()
    if stable_external and "arxiv" in source_lower:
        arxiv_id = normalize_arxiv_id(stable_external)
        if arxiv_id:
            return f"arxiv-{arxiv_id}"

    if stable_external:
        digest = sha256(
            f"external:{namespace}:{stable_external}".encode("utf-8")
        ).hexdigest()
        return f"{namespace}-{digest[:24]}"

    canonical_url = canonicalize_source_url(source_url)
    digest = sha256(f"url:{namespace}:{canonical_url}".encode("utf-8")).hexdigest()
    return f"{namespace}-{digest[:24]}"
