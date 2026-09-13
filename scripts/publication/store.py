"""Local canonical Publication Store with atomic file persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from .models import (
    Briefing,
    IdentityConflictError,
    Item,
    PublicationBundle,
    PublicationValidationError,
)
from .serialization import (
    briefing_from_dict,
    briefing_to_dict,
    bundle_content_hash,
    item_from_dict,
    item_to_dict,
    serialize_bundle,
)
from .validation import (
    validate_bundle,
    validate_category,
)


logger = logging.getLogger(__name__)


class PublicationStoreError(RuntimeError):
    """Base class for read/write failures in the canonical store."""


class CorruptPublicationError(PublicationStoreError):
    """Raised when an existing canonical file cannot be trusted."""


@dataclass
class StoreResult:
    action: str
    bundle: PublicationBundle
    content_hash: str


class PublicationStore:
    """Filesystem-backed store for canonical publications.

    Storage layout is an implementation detail.  Item and briefing business
    identities remain the values inside JSON, not path names.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            try:
                from paths import WORKSPACE_ROOT
            except ImportError:
                from scripts.paths import WORKSPACE_ROOT

            root = WORKSPACE_ROOT / "publications"
        self.root = Path(root)

    # ------------------------------------------------------------------
    # Public write/read API
    # ------------------------------------------------------------------
    def save(self, bundle: PublicationBundle) -> StoreResult:
        """Atomically persist a validated bundle and return create/update/no-op."""

        validate_bundle(bundle)
        if self.root.exists():
            self.validate_integrity()
        existing_briefing = self._find_briefing(bundle.briefing.id)
        existing_bundle = None

        if existing_briefing is not None:
            stored_briefing, briefing_path = existing_briefing
            if stored_briefing.category != bundle.briefing.category:
                raise IdentityConflictError(
                    f"briefing identity migration rejected: {bundle.briefing.id}"
                )
            existing_bundle = self._load_bundle_at(briefing_path)
        existing_items = self._find_items(bundle.items)

        persisted_items = []
        for incoming in bundle.items:
            old = existing_items.get(incoming.id)
            if old is not None and old.category != incoming.category:
                raise IdentityConflictError(
                    f"item identity migration rejected: {incoming.id} "
                    f"({old.category!r} -> {incoming.category!r})"
                )
            if old is not None:
                # Relationship membership is set-like storage metadata.  It
                # grows when an item appears in another canonical briefing.
                merged_ids = sorted(set(old.briefing_ids) | set(incoming.briefing_ids))
                incoming = replace(
                    incoming,
                    briefing_ids=merged_ids,
                    # The first canonical publication time is immutable.  The
                    # latest retrieval and explicitly supplied lifecycle
                    # update values remain mutable metadata.
                    published_at=old.published_at,
                    updated_at=(
                        incoming.updated_at
                        if incoming.updated_at is not None
                        else old.updated_at
                    ),
                )
            persisted_items.append(incoming)

        relationship_only_items = []
        persisted_item_ids = {item.id for item in persisted_items}
        if existing_bundle is not None:
            # A briefing update may remove an item.  Remove only this
            # briefing's reverse membership; other briefing memberships stay.
            for old in existing_bundle.items:
                if (
                    old.id not in persisted_item_ids
                    and bundle.briefing.id in old.briefing_ids
                ):
                    relationship_only_items.append(
                        replace(
                            old,
                            briefing_ids=[
                                value
                                for value in old.briefing_ids
                                if value != bundle.briefing.id
                            ],
                        )
                    )

        persisted_briefing = bundle.briefing
        if existing_bundle is not None:
            persisted_briefing = replace(
                bundle.briefing,
                # Preserve the first canonical publication timestamp.
                published_at=existing_bundle.briefing.published_at,
                updated_at=(
                    bundle.briefing.updated_at
                    if bundle.briefing.updated_at is not None
                    else existing_bundle.briefing.updated_at
                ),
            )

        persisted_bundle = PublicationBundle(
            schema_version=bundle.schema_version,
            briefing=persisted_briefing,
            items=persisted_items,
        )
        validate_bundle(persisted_bundle)

        # A semantic hash deliberately excludes relationship/lifecycle
        # metadata.  Compare the complete canonical representation instead so
        # metadata changes are persisted while a truly identical rerun is a
        # no-op.
        if existing_bundle is not None:
            complete_representation_same = (
                serialize_bundle(existing_bundle) == serialize_bundle(persisted_bundle)
                and self._read_briefing_markdown(existing_briefing[1])
                == persisted_bundle.briefing.body
            )
            if complete_representation_same:
                self._log(persisted_bundle, "noop")
                return StoreResult(
                    "noop",
                    existing_bundle,
                    bundle_content_hash(persisted_bundle),
                )
            action = "update"
        else:
            action = "create"

        # Items are written before the briefing reference.  Every individual
        # file is replaced atomically; a crash leaves either the old metadata
        # file or a complete new file, and readback validation detects any
        # incomplete cross-file bundle.
        items_to_write = {item.id: item for item in relationship_only_items}
        items_to_write.update({item.id: item for item in persisted_items})
        for item in items_to_write.values():
            self._atomic_write_json(self._item_path(item), item_to_dict(item))
        self._atomic_write_json(
            self._briefing_path(persisted_bundle.briefing),
            briefing_to_dict(persisted_bundle.briefing),
        )
        self._atomic_write_text(
            self._briefing_markdown_path(persisted_bundle.briefing),
            persisted_bundle.briefing.body,
        )

        self._log(persisted_bundle, action)
        return StoreResult(
            action, persisted_bundle, bundle_content_hash(persisted_bundle)
        )

    def load_item(self, item_id: str, category: Optional[str] = None) -> Item:
        matches = self._find_item_records(item_id, category)
        if not matches:
            raise FileNotFoundError(f"canonical Item not found: {item_id}")
        if len(matches) > 1:
            raise CorruptPublicationError(
                f"duplicate canonical Item identity: {item_id}"
            )
        return matches[0][0]

    def load_briefing(self, briefing_identity: str) -> Briefing:
        match = self._find_briefing(briefing_identity)
        if match is None:
            raise FileNotFoundError(
                f"canonical Briefing not found: {briefing_identity}"
            )
        return match[0]

    def load_bundle(self, briefing_identity: str) -> PublicationBundle:
        match = self._find_briefing(briefing_identity)
        if match is None:
            raise FileNotFoundError(
                f"canonical Briefing not found: {briefing_identity}"
            )
        bundle = self._load_bundle_at(match[1])
        self.validate_integrity()
        return bundle

    def list_briefings(
        self,
        *,
        date_value: Optional[str] = None,
        categories: Optional[Iterable[str]] = None,
    ) -> List[Briefing]:
        """List canonical briefings without consulting legacy Markdown paths."""

        if not self.root.exists():
            return []
        self.validate_integrity()
        selected_categories = None
        if categories is not None:
            selected_categories = {
                validate_category(category) for category in categories
            }
        briefings = self._all_briefings()
        if date_value is not None:
            briefings = [
                briefing
                for briefing in briefings
                if briefing.date.isoformat() == date_value
            ]
        if selected_categories is not None:
            briefings = [
                briefing
                for briefing in briefings
                if briefing.category in selected_categories
            ]
        return sorted(briefings, key=lambda briefing: briefing.id)

    def validate_integrity(self) -> None:
        """Validate every canonical object and both sides of every relation."""

        items = self._all_items()
        briefings = self._all_briefings()
        item_by_id: Dict[str, Item] = {}
        briefing_by_id: Dict[str, Briefing] = {}
        for item in items:
            if item.id in item_by_id:
                raise CorruptPublicationError(
                    f"duplicate canonical Item identity: {item.id}"
                )
            item_by_id[item.id] = item
        for briefing in briefings:
            if briefing.id in briefing_by_id:
                raise CorruptPublicationError(
                    f"duplicate canonical Briefing identity: {briefing.id}"
                )
            briefing_by_id[briefing.id] = briefing

        for briefing in briefings:
            for item_id in briefing.item_ids:
                item = item_by_id.get(item_id)
                if item is None:
                    raise CorruptPublicationError(
                        f"Briefing {briefing.id} references missing Item {item_id}"
                    )
                if item.category != briefing.category:
                    raise CorruptPublicationError(
                        f"category mismatch: {briefing.id} -> {item.id}"
                    )
                if briefing.id not in item.briefing_ids:
                    raise CorruptPublicationError(
                        f"broken reverse relationship: {item.id} -> {briefing.id}"
                    )
        for item in items:
            for briefing_id_value in item.briefing_ids:
                briefing = briefing_by_id.get(briefing_id_value)
                if briefing is None:
                    raise CorruptPublicationError(
                        f"Item {item.id} references missing Briefing {briefing_id_value}"
                    )
                if item.id not in briefing.item_ids:
                    raise CorruptPublicationError(
                        f"broken forward relationship: {briefing.id} -> {item.id}"
                    )

    # ------------------------------------------------------------------
    # Paths and record discovery
    # ------------------------------------------------------------------
    def _item_path(self, item: Item) -> Path:
        return self.root / "items" / item.category / f"{quote(item.id, safe='')}.json"

    def _briefing_path(self, briefing: Briefing) -> Path:
        return (
            self.root
            / "briefings"
            / f"{briefing.date.year:04d}"
            / f"{briefing.date.month:02d}"
            / f"{briefing.date.day:02d}"
            / briefing.category
            / "briefing.json"
        )

    def _briefing_markdown_path(self, briefing: Briefing) -> Path:
        return self._briefing_path(briefing).with_name("briefing.md")

    def _find_items(self, requested: Iterable[Item]) -> Dict[str, Optional[Item]]:
        result: Dict[str, Optional[Item]] = {}
        for item in requested:
            records = self._find_item_records(item.id)
            if len(records) > 1:
                raise CorruptPublicationError(
                    f"duplicate canonical Item identity: {item.id}"
                )
            result[item.id] = records[0][0] if records else None
        return result

    def _find_item_records(
        self, item_id: str, category: Optional[str] = None
    ) -> List[Tuple[Item, Path]]:
        if category is not None:
            validate_category(category)
            paths = list((self.root / "items" / category).rglob("*.json"))
        else:
            paths = list((self.root / "items").rglob("*.json"))
        records = []
        for path in paths:
            if not path.exists():
                continue
            item = self._read_item(path)
            if category is not None and item.category != category:
                raise CorruptPublicationError(
                    f"Item category does not match its requested identity: {item_id}"
                )
            if item.id == item_id:
                records.append((item, path))
        return records

    def _item_path_for_id(self, item_id: str, category: str) -> Path:
        return self.root / "items" / category / f"{quote(item_id, safe='')}.json"

    def _find_briefing(self, identity: str) -> Optional[Tuple[Briefing, Path]]:
        matches = []
        for path in (self.root / "briefings").rglob("briefing.json"):
            if not path.exists():
                continue
            briefing = self._read_briefing(path)
            if briefing.id == identity:
                matches.append((briefing, path))
        if len(matches) > 1:
            raise CorruptPublicationError(
                f"duplicate canonical Briefing identity: {identity}"
            )
        return matches[0] if matches else None

    def _all_items(self) -> List[Item]:
        return [self._read_item(path) for path in (self.root / "items").rglob("*.json")]

    def _all_briefings(self) -> List[Briefing]:
        return [
            self._read_briefing(path)
            for path in (self.root / "briefings").rglob("briefing.json")
        ]

    # ------------------------------------------------------------------
    # Serialization and atomic writes
    # ------------------------------------------------------------------
    def _read_json(self, path: Path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorruptPublicationError(f"cannot read canonical file {path}") from exc

    @staticmethod
    def _read_briefing_markdown(path: Path) -> Optional[str]:
        try:
            return path.with_name("briefing.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def _read_item(self, path: Path) -> Item:
        try:
            return item_from_dict(self._read_json(path))
        except (KeyError, TypeError, ValueError, PublicationValidationError) as exc:
            raise CorruptPublicationError(
                f"invalid canonical Item file {path}"
            ) from exc

    def _read_briefing(self, path: Path) -> Briefing:
        try:
            return briefing_from_dict(self._read_json(path))
        except (KeyError, TypeError, ValueError, PublicationValidationError) as exc:
            raise CorruptPublicationError(
                f"invalid canonical Briefing file {path}"
            ) from exc

    def _load_bundle_at(self, briefing_path: Path) -> PublicationBundle:
        briefing = self._read_briefing(briefing_path)
        try:
            items = [
                self.load_item(item_id, briefing.category)
                for item_id in briefing.item_ids
            ]
        except (FileNotFoundError, CorruptPublicationError) as exc:
            raise CorruptPublicationError(
                f"invalid canonical bundle for {briefing.id}: missing Item or invalid Item"
            ) from exc
        bundle = PublicationBundle(briefing.schema_version, briefing, items)
        try:
            return validate_bundle(bundle)
        except PublicationValidationError as exc:
            raise CorruptPublicationError(
                f"invalid canonical bundle for {briefing.id}"
            ) from exc

    def _atomic_write_json(self, path: Path, value: dict) -> None:
        self._atomic_write_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    def _atomic_write_text(self, path: Path, content: str) -> None:
        temp_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
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
                # File replacement is still atomic on platforms where opening
                # a directory for fsync is unsupported.
                pass
        except OSError as exc:
            try:
                if temp_name is not None:
                    os.unlink(temp_name)
            except OSError:
                pass
            raise PublicationStoreError(f"atomic write failed for {path}") from exc

    @staticmethod
    def _log(bundle: PublicationBundle, action: str) -> None:
        logger.info(
            "publication_id=%s category=%s action=%s item_count=%d",
            bundle.briefing.id,
            bundle.briefing.category,
            action,
            len(bundle.items),
        )
