"""Transport-independent read-only queries with an explicit visibility policy."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Callable, TypeVar

from .publication.delivery import DeliveryStateStore
from .publication.models import CANONICAL_CATEGORIES
from .publication.serialization import (
    briefing_to_dict,
    bundle_content_hash,
    datetime_to_iso,
    item_to_dict,
)
from .publication.store import CorruptPublicationError, PublicationStore
from .publication.validation import validate_category, validate_id

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
T = TypeVar("T")


class QueryError(ValueError):
    """A public error code, deliberately without paths or stored content."""


class _BoundedStore(PublicationStore):
    def __init__(self, root):
        super().__init__(root)
        self._item_indexes = ContextVar("mcp_item_indexes", default=None)

    @contextmanager
    def read_snapshot(self):
        token = self._item_indexes.set({})
        try:
            yield
        finally:
            self._item_indexes.reset(token)

    def _find_item_records(self, item_id, category=None):
        indexes = self._item_indexes.get()
        if indexes is None:
            return super()._find_item_records(item_id, category)
        if category is not None:
            validate_category(category)
        if category not in indexes:
            base = self.root / "items"
            if category is not None:
                base = base / category
            records = {}
            for path in base.rglob("*.json"):
                if not path.exists():
                    continue
                item = self._read_item(path)
                if category is not None and item.category != category:
                    raise CorruptPublicationError("Item category mismatch")
                records.setdefault(item.id, []).append((item, path))
            indexes[category] = records
        return indexes[category].get(item_id, [])

    def _read_json(self, path: Path):
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise CorruptPublicationError("canonical file exceeds read limit")
        return json.loads(raw)


def _digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _page(cursor, scope, version, total):
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if value["scope"] != _digest(scope):
            raise ValueError
        offset = value["offset"]
        if type(offset) is not int or offset < 0 or offset > total:
            raise ValueError
        if value["version"] != version:
            raise QueryError("STALE_CURSOR")
        return offset
    except QueryError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise QueryError("INVALID_CURSOR") from None


def _cursor(scope, version, offset):
    raw = json.dumps(
        {"scope": _digest(scope), "version": version, "offset": offset}
    ).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise QueryError("INVALID_ARGUMENT")


def _day(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise QueryError("INVALID_ARGUMENT") from None


class ReadOnlyQueries:
    def __init__(
        self, root: Path, allowed_sinks=("discord",), *, visibility="delivered"
    ):
        self.root = Path(root)
        if visibility not in {"delivered", "generated"}:
            raise ValueError("Invalid content visibility")
        self.visibility = visibility
        self.allowed_sinks = tuple(allowed_sinks)
        if not self.allowed_sinks or set(self.allowed_sinks) - {"discord", "web"}:
            raise ValueError("Invalid configured delivery channels")
        self.store = _BoundedStore(self.root / "publications")
        self.deliveries = DeliveryStateStore(self.root / "deliveries")

    def _signature(self):
        """Observe existing files only; unlike a producer, never create a lock."""
        entries = []
        directories = (
            ("publications", "deliveries")
            if self.visibility == "delivered"
            else ("publications",)
        )
        for name in directories:
            base = self.root / name
            if base.is_symlink():
                raise OSError("symlinked data directory")
            if not base.exists():
                continue
            for path in base.rglob("*"):
                if path.is_symlink():
                    raise OSError("symlinked data entry")
                if not path.is_file() or path.suffix not in {".json", ".md"}:
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise OSError("unsupported data entry")
                entries.append(
                    (
                        str(path.relative_to(self.root)),
                        info.st_ino,
                        info.st_mtime_ns,
                        info.st_size,
                    )
                )
                if len(entries) > 100000:
                    raise OSError("catalog exceeds scan limit")
        return sorted(entries)

    def _consistent(self, operation: Callable[[], T]) -> T:
        for _ in range(2):
            try:
                before = self._signature()
                with self.store.read_snapshot():
                    result = operation()
                if before == self._signature():
                    if (
                        len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                        > MAX_RESPONSE_BYTES
                    ):
                        raise QueryError("CONTENT_TOO_LARGE")
                    return result
            except QueryError:
                raise
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                raise QueryError("CONTENT_UNAVAILABLE") from None
        raise QueryError("CONTENT_CHANGED")

    def _sink(self, sink):
        if self.visibility == "generated":
            if sink is not None:
                raise QueryError("INVALID_ARGUMENT")
            return None
        sink = sink or self.allowed_sinks[0]
        if sink not in self.allowed_sinks:
            raise QueryError("INVALID_ARGUMENT")
        return sink

    def _identity(self, identity):
        try:
            validate_id(identity)
        except ValueError:
            raise QueryError("INVALID_ARGUMENT") from None

    def _delivery(self, identity, sink):
        if self.visibility == "generated":
            return {"content_state": "generated", "sink": None, "delivered_at": None}
        state = self.deliveries.load(identity, sink)
        if state is None or state.status != "success":
            return None
        return {
            "content_state": "delivered",
            "sink": sink,
            "delivered_at": datetime_to_iso(state.delivered_at),
        }

    def _bundle(self, identity, sink):
        delivery = self._delivery(identity, sink)
        if delivery is None:
            raise QueryError("NOT_FOUND")
        try:
            bundle = self.store.load_bundle(identity)
        except FileNotFoundError:
            raise QueryError("NOT_FOUND") from None
        return bundle, delivery

    def list_briefings(
        self,
        date_from=None,
        date_to=None,
        categories=None,
        sink=None,
        limit=20,
        cursor=None,
    ):
        start, end = _day(date_from), _day(date_to)
        if start and end and start > end:
            raise QueryError("INVALID_ARGUMENT")
        if categories is not None and (
            not isinstance(categories, list)
            or any(c not in CANONICAL_CATEGORIES for c in categories)
        ):
            raise QueryError("INVALID_ARGUMENT")
        _limit(limit, 100)
        sink = self._sink(sink)
        selected = sorted(set(categories)) if categories is not None else None
        scope = ["briefings", self.visibility, start, end, selected, sink]

        def read():
            rows = []
            for briefing in self.store.list_briefings(categories=selected):
                day = briefing.date.isoformat()
                if (start and day < start) or (end and day > end):
                    continue
                delivery = self._delivery(briefing.id, sink)
                if delivery:
                    rows.append(
                        {
                            "id": briefing.id,
                            "date": day,
                            "category": briefing.category,
                            "title": briefing.title,
                            "item_count": len(briefing.item_ids),
                            **delivery,
                        }
                    )
            rows.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
            version = _digest(rows)
            offset = _page(cursor, scope, version, len(rows))
            next_offset = offset + limit
            return {
                "briefings": rows[offset:next_offset],
                "total": len(rows),
                "sink": sink,
                "visibility": self.visibility,
                "next_cursor": (
                    _cursor(scope, version, next_offset)
                    if next_offset < len(rows)
                    else None
                ),
            }

        return self._consistent(read)

    def get_briefing(self, briefing_id, sink=None, limit=12000, cursor=None):
        self._identity(briefing_id)
        _limit(limit, 20000)
        sink = self._sink(sink)
        scope = ["body", self.visibility, briefing_id, sink]

        def read():
            bundle, delivery = self._bundle(briefing_id, sink)
            version = bundle_content_hash(bundle)
            value = briefing_to_dict(bundle.briefing)
            body = value.pop("body")
            offset = _page(cursor, scope, version, len(body))
            next_offset = offset + limit
            return {
                **value,
                **delivery,
                "body": body[offset:next_offset],
                "body_total_chars": len(body),
                "content_version": version,
                "next_cursor": (
                    _cursor(scope, version, next_offset)
                    if next_offset < len(body)
                    else None
                ),
            }

        return self._consistent(read)

    def get_item(self, briefing_id, item_id, sink=None):
        self._identity(briefing_id)
        self._identity(item_id)
        sink = self._sink(sink)

        def read():
            bundle, delivery = self._bundle(briefing_id, sink)
            for item in bundle.items:
                if item.id == item_id:
                    value = item_to_dict(item)
                    # Other memberships may refer to briefings not yet delivered.
                    value["briefing_ids"] = [briefing_id]
                    return {"briefing_id": briefing_id, "item": value, **delivery}
            raise QueryError("NOT_FOUND")

        return self._consistent(read)
