"""Delivery state for publication sinks.

Delivery is deliberately separate from the canonical publication models.  A
publication describes what DailyInfo finalized; this module records whether a
briefing was delivered to one particular sink.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Optional
from urllib.parse import quote

from .identity import briefing_id
from .models import CANONICAL_CATEGORIES


DELIVERY_SCHEMA_VERSION = 1
DELIVERY_STATUSES = ("pending", "success", "failed")
_SINK_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_ERROR_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SENSITIVE_ERROR_PATTERNS = (
    re.compile(r"(?i)\bAuthorization\s*:\s*\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"(?i)discord(?:app)?\.com/api/webhooks/"),
    re.compile(r"(?i)\b(?:api[_ -]?key|secret[_ -]?key|access[_ -]?token)\s*[:=]"),
    re.compile(r"(?<![A-Za-z0-9])/(?:Users|home|private/(?:tmp|var)|tmp|var)/"),
)

logger = logging.getLogger(__name__)


class DeliveryValidationError(ValueError):
    """Raised when a delivery state violates the delivery contract."""


class DeliveryStoreError(RuntimeError):
    """Base class for delivery state persistence failures."""


class CorruptDeliveryStateError(DeliveryStoreError):
    """Raised when an existing delivery record cannot be trusted."""


def delivery_key(briefing_id_value: str, sink: str) -> str:
    """Return the deterministic identity of one briefing/sink delivery."""

    validate_briefing_identity(briefing_id_value)
    validate_sink(sink)
    return f"{briefing_id_value}:{sink}"


def validate_briefing_identity(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeliveryValidationError("briefing_id must be non-empty text")
    parts = value.rsplit("-", 3)
    if len(parts) != 4:
        raise DeliveryValidationError("briefing_id must be category-YYYY-MM-DD")
    category, year, month, day = parts
    if category not in CANONICAL_CATEGORIES:
        raise DeliveryValidationError("briefing_id contains an unsupported category")
    if (
        len(year) != 4
        or len(month) != 2
        or len(day) != 2
        or not year.isdigit()
        or not month.isdigit()
        or not day.isdigit()
    ):
        raise DeliveryValidationError("briefing_id must contain YYYY-MM-DD")
    try:
        datetime.strptime(f"{year}-{month}-{day}", "%Y-%m-%d")
    except ValueError as exc:
        raise DeliveryValidationError("briefing_id contains an invalid date") from exc
    if briefing_id(category, f"{year}-{month}-{day}") != value:
        raise DeliveryValidationError("briefing_id is not deterministic")
    return value


def validate_sink(value: str) -> str:
    if not isinstance(value, str) or not _SINK_RE.fullmatch(value):
        raise DeliveryValidationError("sink must be a lowercase machine name")
    return value


def _ensure_aware(value: Optional[datetime], field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise DeliveryValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    normalized = _ensure_aware(value, "timestamp")
    if normalized is None:
        return None
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeliveryValidationError(
            f"{field_name} must be an ISO-8601 string or null"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DeliveryValidationError(f"{field_name} must be ISO-8601") from exc
    return _ensure_aware(parsed, field_name)


def sanitize_error(value: Any) -> Optional[str]:
    """Keep diagnostic errors short and free of URLs or obvious credentials."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _SAFE_ERROR_URL_RE.sub("<redacted-url>", text)
    for pattern in _SENSITIVE_ERROR_PATTERNS:
        if pattern.search(text):
            return "delivery failed (sensitive error details redacted)"
    return text[:1000]


@dataclass(frozen=True)
class DeliveryState:
    """One sink-specific delivery record for one canonical briefing."""

    schema_version: int
    briefing_id: str
    sink: str
    status: str
    attempt_count: int
    first_attempted_at: Optional[datetime]
    last_attempted_at: Optional[datetime]
    delivered_at: Optional[datetime]
    external_ref: Optional[str] = None
    last_error: Optional[str] = None


def validate_delivery_state(state: DeliveryState) -> DeliveryState:
    if not isinstance(state, DeliveryState):
        raise DeliveryValidationError("expected a DeliveryState")
    if state.schema_version != DELIVERY_SCHEMA_VERSION:
        raise DeliveryValidationError("unsupported DeliveryState schema_version")
    validate_briefing_identity(state.briefing_id)
    validate_sink(state.sink)
    if state.status not in DELIVERY_STATUSES:
        raise DeliveryValidationError(f"unsupported delivery status: {state.status!r}")
    if not isinstance(state.attempt_count, int) or state.attempt_count < 0:
        raise DeliveryValidationError("attempt_count must be a non-negative integer")
    _ensure_aware(state.first_attempted_at, "first_attempted_at")
    _ensure_aware(state.last_attempted_at, "last_attempted_at")
    _ensure_aware(state.delivered_at, "delivered_at")
    if state.attempt_count == 0 and (
        state.first_attempted_at is not None or state.last_attempted_at is not None
    ):
        raise DeliveryValidationError(
            "zero-attempt state cannot have attempt timestamps"
        )
    if state.attempt_count > 0 and (
        state.first_attempted_at is None or state.last_attempted_at is None
    ):
        raise DeliveryValidationError("attempted state must have attempt timestamps")
    if state.status == "success" and state.last_error is not None:
        raise DeliveryValidationError("successful delivery cannot retain last_error")
    if state.status == "failed" and state.attempt_count == 0:
        raise DeliveryValidationError("failed delivery must have an attempt")
    if state.status == "failed" and state.delivered_at is not None:
        raise DeliveryValidationError("failed delivery cannot have delivered_at")
    if state.status == "pending" and state.delivered_at is not None:
        raise DeliveryValidationError("pending delivery cannot have delivered_at")
    if state.status == "pending" and state.last_error is not None:
        raise DeliveryValidationError("pending delivery cannot retain last_error")
    if state.status == "success" and state.delivered_at is not None:
        _ensure_aware(state.delivered_at, "delivered_at")
    if (
        state.status == "success"
        and state.attempt_count > 0
        and state.delivered_at is None
    ):
        raise DeliveryValidationError("attempted success must have delivered_at")
    if state.external_ref is not None:
        if not isinstance(state.external_ref, str) or not state.external_ref.strip():
            raise DeliveryValidationError("external_ref must be non-empty text or null")
        if any(
            pattern.search(state.external_ref) for pattern in _SENSITIVE_ERROR_PATTERNS
        ):
            raise DeliveryValidationError("external_ref contains sensitive data")
    if state.last_error is not None:
        if not isinstance(state.last_error, str) or not state.last_error.strip():
            raise DeliveryValidationError("last_error must be non-empty text or null")
        if any(
            pattern.search(state.last_error) for pattern in _SENSITIVE_ERROR_PATTERNS
        ):
            raise DeliveryValidationError("last_error contains sensitive data")
    return state


def delivery_state_to_dict(state: DeliveryState) -> dict[str, Any]:
    validate_delivery_state(state)
    return {
        "schema_version": state.schema_version,
        "briefing_id": state.briefing_id,
        "sink": state.sink,
        "status": state.status,
        "attempt_count": state.attempt_count,
        "first_attempted_at": _datetime_to_iso(state.first_attempted_at),
        "last_attempted_at": _datetime_to_iso(state.last_attempted_at),
        "delivered_at": _datetime_to_iso(state.delivered_at),
        "external_ref": state.external_ref,
        "last_error": state.last_error,
    }


def delivery_state_from_dict(value: Mapping[str, Any]) -> DeliveryState:
    if not isinstance(value, Mapping):
        raise DeliveryValidationError("serialized delivery state must be an object")
    required = {
        "schema_version",
        "briefing_id",
        "sink",
        "status",
        "attempt_count",
        "first_attempted_at",
        "last_attempted_at",
        "delivered_at",
        "external_ref",
        "last_error",
    }
    missing = required - set(value)
    if missing:
        raise DeliveryValidationError(
            "serialized delivery state is missing: " + ", ".join(sorted(missing))
        )
    state = DeliveryState(
        schema_version=value["schema_version"],
        briefing_id=value["briefing_id"],
        sink=value["sink"],
        status=value["status"],
        attempt_count=value["attempt_count"],
        first_attempted_at=_parse_datetime(
            value["first_attempted_at"], "first_attempted_at"
        ),
        last_attempted_at=_parse_datetime(
            value["last_attempted_at"], "last_attempted_at"
        ),
        delivered_at=_parse_datetime(value["delivered_at"], "delivered_at"),
        external_ref=value["external_ref"],
        last_error=value["last_error"],
    )
    return validate_delivery_state(state)


def serialize_delivery_state(state: DeliveryState) -> str:
    return json.dumps(
        delivery_state_to_dict(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class DeliveryStateStore:
    """Atomic filesystem store for sink-specific delivery state."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            try:
                from paths import WORKSPACE_ROOT
            except ImportError:
                from scripts.paths import WORKSPACE_ROOT

            root = WORKSPACE_ROOT / "deliveries"
        self.root = Path(root)

    def _path(self, briefing_id_value: str, sink: str) -> Path:
        validate_briefing_identity(briefing_id_value)
        validate_sink(sink)
        return self.root / sink / f"{quote(briefing_id_value, safe='')}.json"

    def load(self, briefing_id_value: str, sink: str) -> Optional[DeliveryState]:
        path = self._path(briefing_id_value, sink)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            state = delivery_state_from_dict(payload)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            DeliveryValidationError,
        ) as exc:
            raise CorruptDeliveryStateError(
                f"invalid delivery state file {path}"
            ) from exc
        if state.briefing_id != briefing_id_value or state.sink != sink:
            raise CorruptDeliveryStateError(
                f"delivery state identity mismatch in {path}"
            )
        return state

    def save(self, state: DeliveryState) -> DeliveryState:
        validate_delivery_state(state)
        self._atomic_write_json(
            self._path(state.briefing_id, state.sink), delivery_state_to_dict(state)
        )
        return state

    def begin_attempt(
        self,
        briefing_id_value: str,
        sink: str,
        *,
        attempted_at: datetime,
    ) -> DeliveryState:
        attempted_at = _ensure_aware(attempted_at, "attempted_at")
        assert attempted_at is not None
        existing = self.load(briefing_id_value, sink)
        attempt_count = existing.attempt_count + 1 if existing else 1
        state = DeliveryState(
            schema_version=DELIVERY_SCHEMA_VERSION,
            briefing_id=briefing_id_value,
            sink=sink,
            status="pending",
            attempt_count=attempt_count,
            first_attempted_at=(
                existing.first_attempted_at if existing else attempted_at
            ),
            last_attempted_at=attempted_at,
            delivered_at=None,
            external_ref=None,
            last_error=None,
        )
        return self.save(state)

    def record_result(self, result: Any) -> DeliveryState:
        from .publishers import PublishResult

        if not isinstance(result, PublishResult):
            raise DeliveryStoreError("record_result expects a PublishResult")
        if result.status not in ("success", "failed"):
            raise DeliveryStoreError("only success or failed results can be recorded")
        existing = self.load(result.publication_id, result.sink)
        if existing is None or existing.status != "pending":
            raise DeliveryStoreError("delivery result has no matching pending attempt")
        if result.attempted_at is None:
            raise DeliveryStoreError("delivery result must have attempted_at")
        attempted_at = _ensure_aware(result.attempted_at, "attempted_at")
        assert attempted_at is not None
        state = DeliveryState(
            schema_version=DELIVERY_SCHEMA_VERSION,
            briefing_id=existing.briefing_id,
            sink=existing.sink,
            status=result.status,
            attempt_count=existing.attempt_count,
            first_attempted_at=existing.first_attempted_at,
            last_attempted_at=existing.last_attempted_at,
            delivered_at=attempted_at if result.status == "success" else None,
            external_ref=result.external_ref,
            last_error=(
                sanitize_error(result.error) if result.status == "failed" else None
            ),
        )
        return self.save(state)

    def bootstrap_legacy_success(
        self, briefing_id_value: str, sink: str
    ) -> DeliveryState:
        """Record a historical pushed/ marker without inventing an attempt time."""

        existing = self.load(briefing_id_value, sink)
        if existing is not None:
            if existing.status == "success":
                return existing
            raise DeliveryStoreError(
                f"cannot bootstrap legacy success over {existing.status} state"
            )
        return self.save(
            DeliveryState(
                schema_version=DELIVERY_SCHEMA_VERSION,
                briefing_id=briefing_id_value,
                sink=sink,
                status="success",
                attempt_count=0,
                first_attempted_at=None,
                last_attempted_at=None,
                delivered_at=None,
                external_ref=None,
                last_error=None,
            )
        )

    @staticmethod
    def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
        temp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except OSError as exc:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise DeliveryStoreError(
                f"atomic delivery state write failed for {path}"
            ) from exc
