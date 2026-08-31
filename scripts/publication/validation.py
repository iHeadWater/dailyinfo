"""Fail-closed validation for Publication Contract v1."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import ipaddress
import re
from typing import Any, List, Optional, Sequence, Union
from urllib.parse import urlsplit

from .identity import briefing_id
from .models import (
    CANONICAL_CATEGORIES,
    Briefing,
    Item,
    PublicationBundle,
    PublicationValidationError,
    SCHEMA_VERSION,
    SourceMetadata,
)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bAuthorization\s*:\s*\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(
        r"https?://(?:canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+"
    ),
    re.compile(r"https?://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\b(?:sk|rk|ghp|github_pat|xox[baprs])-[-_A-Za-z0-9]{10,}\b"),
    re.compile(r"(?i)Traceback \(most recent call last\)"),
    re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|private/(?:tmp|var)|tmp|var)/[^\s]+"),
    re.compile(
        r"(?i)\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|::1|"
        r"10\.(?:\d{1,3}\.){2}\d{1,3}|"
        r"192\.168\.(?:\d{1,3}\.)?\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.(?:\d{1,3}\.)?\d{1,3})(?::\d+)?\b"
    ),
)


def validate_category(category: str) -> str:
    if category not in CANONICAL_CATEGORIES:
        raise PublicationValidationError(
            f"unsupported canonical category: {category!r}"
        )
    return category


def validate_id(value: str, field_name: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PublicationValidationError(
            f"invalid {field_name}: expected a non-empty safe stable id"
        )
    return value


def _security_check(value: Any, field_name: str) -> None:
    if not isinstance(value, str):
        return
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise PublicationValidationError(
                f"public publication field {field_name!r} contains blocked internal/secret data"
            )


def validate_public_source_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise PublicationValidationError(
            "source.url must be an absolute public http(s) URL"
        )
    try:
        parsed = urlsplit(url.strip())
    except ValueError as exc:
        raise PublicationValidationError("source.url is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise PublicationValidationError(
            "source.url must use absolute http:// or https://"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise PublicationValidationError("source.url contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise PublicationValidationError("source.url contains an invalid port")
    if parsed.username or parsed.password:
        raise PublicationValidationError("source.url must not contain credentials")
    try:
        hostname = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise PublicationValidationError("source.url has an invalid hostname") from exc
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        raise PublicationValidationError("source.url must not target a local host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        raise PublicationValidationError("source.url must not target an internal IP")
    _security_check(url, "source.url")
    return url.strip()


def _ensure_aware(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PublicationValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


DateLike = Union[date, datetime, str]


def normalize_datetime(
    value: DateLike,
    field_name: str,
    *,
    date_timezone=None,
) -> datetime:
    """Normalize timestamps to aware UTC; date-only source values use business TZ."""

    if isinstance(value, datetime):
        return _ensure_aware(value, field_name)
    if isinstance(value, date):
        if date_timezone is None:
            raise PublicationValidationError(
                f"{field_name} date-only value requires a configured timezone"
            )
        return datetime.combine(value, time.min, tzinfo=date_timezone).astimezone(
            timezone.utc
        )
    if isinstance(value, str):
        raw = value.strip()
        if _ISO_DATE_RE.fullmatch(raw):
            if date_timezone is None:
                raise PublicationValidationError(
                    f"{field_name} date-only value requires a configured timezone"
                )
            try:
                parsed_date = date.fromisoformat(raw)
            except ValueError as exc:
                raise PublicationValidationError(
                    f"{field_name} must contain a valid calendar date"
                ) from exc
            return datetime.combine(
                parsed_date, time.min, tzinfo=date_timezone
            ).astimezone(timezone.utc)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PublicationValidationError(
                f"{field_name} must be an ISO-8601 datetime"
            ) from exc
        return _ensure_aware(parsed, field_name)
    raise PublicationValidationError(f"{field_name} must be a date/datetime value")


def normalize_optional_datetime(
    value: Optional[DateLike], field_name: str, *, date_timezone=None
):
    if value is None:
        return None
    return normalize_datetime(value, field_name, date_timezone=date_timezone)


def normalize_date(value: DateLike) -> date:
    if isinstance(value, datetime):
        raise PublicationValidationError(
            "briefing date must be a calendar date, not datetime"
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE_RE.fullmatch(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise PublicationValidationError("invalid briefing date") from exc
    raise PublicationValidationError("briefing date must be YYYY-MM-DD")


def normalize_text_list(
    values: Sequence[str], field_name: str, *, unique: bool = False
) -> List[str]:
    if not isinstance(values, (list, tuple)):
        raise PublicationValidationError(f"{field_name} must be a list of strings")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise PublicationValidationError(
                f"{field_name} must contain non-empty strings"
            )
        text = value.strip()
        if unique:
            if text in seen:
                continue
            seen.add(text)
        result.append(text)
    if unique:
        result.sort()
    return result


def _validate_common_text(value: Any, field_name: str, *, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise PublicationValidationError(f"{field_name} must be non-empty text")
    _security_check(value, field_name)
    return value


def validate_item(item: Item) -> Item:
    if not isinstance(item, Item):
        raise PublicationValidationError("expected an Item")
    if item.schema_version != SCHEMA_VERSION:
        raise PublicationValidationError("unsupported Item schema_version")
    validate_id(item.id, "Item.id")
    validate_category(item.category)
    _validate_common_text(item.title, "Item.title")
    if not isinstance(item.source, SourceMetadata):
        raise PublicationValidationError("Item.source must be source metadata")
    if not isinstance(item.source.name, str) or not item.source.name.strip():
        raise PublicationValidationError("Item.source.name must be non-empty")
    _security_check(item.source.name, "Item.source.name")
    validate_public_source_url(item.source.url)
    if item.source.external_id is not None:
        _validate_common_text(item.source.external_id, "Item.source.external_id")
    if not isinstance(item.authors, list):
        raise PublicationValidationError("Item.authors must be a list")
    if not isinstance(item.tags, list):
        raise PublicationValidationError("Item.tags must be a list")
    authors = normalize_text_list(item.authors, "Item.authors")
    tags = normalize_text_list(item.tags, "Item.tags", unique=True)
    for value in authors:
        _security_check(value, "Item.authors")
    for value in tags:
        _security_check(value, "Item.tags")
    if item.source_published_at is not None:
        _ensure_aware(item.source_published_at, "Item.source_published_at")
    _ensure_aware(item.retrieved_at, "Item.retrieved_at")
    _ensure_aware(item.published_at, "Item.published_at")
    if item.updated_at is not None:
        _ensure_aware(item.updated_at, "Item.updated_at")
    _validate_common_text(item.summary, "Item.summary")
    if item.why_it_matters is not None:
        _validate_common_text(item.why_it_matters, "Item.why_it_matters")
    _validate_common_text(item.language, "Item.language")
    if not isinstance(item.briefing_ids, list):
        raise PublicationValidationError("Item.briefing_ids must be a list")
    for value in item.briefing_ids:
        validate_id(value, "Item.briefing_ids entry")
    if len(item.briefing_ids) != len(set(item.briefing_ids)):
        raise PublicationValidationError("Item.briefing_ids must be unique")
    return item


def validate_briefing(briefing: Briefing) -> Briefing:
    if not isinstance(briefing, Briefing):
        raise PublicationValidationError("expected a Briefing")
    if briefing.schema_version != SCHEMA_VERSION:
        raise PublicationValidationError("unsupported Briefing schema_version")
    validate_category(briefing.category)
    if not isinstance(briefing.date, date) or isinstance(briefing.date, datetime):
        raise PublicationValidationError("Briefing.date must be a date")
    expected_id = briefing_id(briefing.category, briefing.date.isoformat())
    if briefing.id != expected_id:
        raise PublicationValidationError(
            f"Briefing.id must be deterministic: expected {expected_id!r}"
        )
    validate_id(briefing.id, "Briefing.id")
    _validate_common_text(briefing.title, "Briefing.title")
    _ensure_aware(briefing.generated_at, "Briefing.generated_at")
    _ensure_aware(briefing.published_at, "Briefing.published_at")
    if briefing.updated_at is not None:
        _ensure_aware(briefing.updated_at, "Briefing.updated_at")
    if not isinstance(briefing.item_ids, list):
        raise PublicationValidationError("Briefing.item_ids must be a list")
    for value in briefing.item_ids:
        validate_id(value, "Briefing.item_ids entry")
    if len(briefing.item_ids) != len(set(briefing.item_ids)):
        raise PublicationValidationError("Briefing.item_ids must be unique")
    _validate_common_text(briefing.body, "Briefing.body")
    return briefing


def validate_bundle(bundle: PublicationBundle) -> PublicationBundle:
    if not isinstance(bundle, PublicationBundle):
        raise PublicationValidationError("expected a PublicationBundle")
    if bundle.schema_version != SCHEMA_VERSION:
        raise PublicationValidationError("unsupported PublicationBundle schema_version")
    validate_briefing(bundle.briefing)
    if not isinstance(bundle.items, list):
        raise PublicationValidationError("PublicationBundle.items must be a list")
    seen = set()
    for item in bundle.items:
        validate_item(item)
        if item.id in seen:
            raise PublicationValidationError(f"duplicate Item identity: {item.id}")
        seen.add(item.id)
        if item.category != bundle.briefing.category:
            raise PublicationValidationError("Briefing and Item categories must match")
        if item.id not in bundle.briefing.item_ids:
            raise PublicationValidationError(
                f"Briefing.item_ids is missing Item.id {item.id!r}"
            )
        if bundle.briefing.id not in item.briefing_ids:
            raise PublicationValidationError(
                f"Item.briefing_ids is missing Briefing.id {bundle.briefing.id!r}"
            )
    if set(bundle.briefing.item_ids) != seen:
        missing = sorted(set(bundle.briefing.item_ids) - seen)
        raise PublicationValidationError(
            f"Briefing references Item ids not present in bundle: {missing}"
        )
    return bundle
