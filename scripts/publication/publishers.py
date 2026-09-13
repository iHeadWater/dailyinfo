"""Publisher abstraction and the Phase 2C Discord publisher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Callable, Optional, Protocol

from .delivery import DeliveryStateStore, sanitize_error
from .models import PublicationBundle
from .validation import validate_bundle


logger = logging.getLogger(__name__)


def _default_discord_transport(channel_id: str, content: str) -> bool:
    """Resolve the mature Discord transport lazily to avoid import side effects."""

    try:
        from push_to_discord import send_to_discord
    except ImportError:
        from scripts.push_to_discord import send_to_discord

    return send_to_discord(channel_id, content)


class Publisher(Protocol):
    """Minimal contract for a delivery sink."""

    sink: str

    def publish(self, publication: PublicationBundle) -> "PublishResult": ...


@dataclass(frozen=True)
class PublishResult:
    """Result of one briefing-level external delivery attempt."""

    sink: str
    publication_id: str
    status: str
    attempted_at: Optional[datetime]
    external_ref: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "success"


class DiscordPublisher:
    """Publish a canonical briefing using the existing Discord transport.

    The transport is injected so production can reuse ``send_to_discord`` and
    tests can use a fake without making network requests.  Formatting,
    chunking, retry, and HTTP status handling remain in that existing helper.
    """

    sink = "discord"

    def __init__(
        self,
        channel_id: str,
        *,
        transport: Optional[Callable[[str, str], bool]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("Discord channel_id must be non-empty")
        self.channel_id = channel_id
        self.transport = transport or _default_discord_transport
        self.clock = clock

    def publish(self, publication: PublicationBundle) -> PublishResult:
        validate_bundle(publication)
        attempted_at = self.clock()
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("publisher clock must return a timezone-aware datetime")
        try:
            delivered = self.transport(self.channel_id, publication.briefing.body)
        except Exception as exc:
            return PublishResult(
                sink=self.sink,
                publication_id=publication.briefing.id,
                status="failed",
                attempted_at=attempted_at,
                error=sanitize_error(exc),
            )
        if delivered is not True:
            return PublishResult(
                sink=self.sink,
                publication_id=publication.briefing.id,
                status="failed",
                attempted_at=attempted_at,
                error="Discord transport returned failure",
            )
        return PublishResult(
            sink=self.sink,
            publication_id=publication.briefing.id,
            status="success",
            attempted_at=attempted_at,
        )


class DeliveryCoordinator:
    """Apply delivery state semantics around a Publisher call."""

    def __init__(
        self,
        store: DeliveryStateStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.clock = clock

    def publish(
        self,
        publication: PublicationBundle,
        publisher: Publisher,
        *,
        force: bool = False,
        legacy_delivered: bool = False,
    ) -> PublishResult:
        validate_bundle(publication)
        publication_id = publication.briefing.id
        state = self.store.load(publication_id, publisher.sink)
        if state is not None and state.status == "success" and not force:
            logger.info(
                "publication_id=%s category=%s sink=%s action=noop item_count=%d",
                publication_id,
                publication.briefing.category,
                publisher.sink,
                len(publication.items),
            )
            return PublishResult(
                sink=publisher.sink,
                publication_id=publication_id,
                status="skipped",
                attempted_at=None,
            )
        if state is None and legacy_delivered and not force:
            self.store.bootstrap_legacy_success(publication_id, publisher.sink)
            logger.info(
                "publication_id=%s category=%s sink=%s action=legacy-bootstrap item_count=%d",
                publication_id,
                publication.briefing.category,
                publisher.sink,
                len(publication.items),
            )
            return PublishResult(
                sink=publisher.sink,
                publication_id=publication_id,
                status="skipped",
                attempted_at=None,
            )

        pending = self.store.begin_attempt(
            publication_id,
            publisher.sink,
            attempted_at=self.clock(),
        )
        try:
            result = publisher.publish(publication)
        except Exception as exc:
            result = PublishResult(
                sink=publisher.sink,
                publication_id=publication_id,
                status="failed",
                attempted_at=pending.last_attempted_at,
                error=sanitize_error(exc),
            )
        if (
            result.sink != publisher.sink
            or result.publication_id != publication_id
            or result.status not in ("success", "failed")
        ):
            raise ValueError("Publisher returned a result for the wrong delivery")
        # A write failure here is intentionally propagated.  The external send
        # may already have happened, so silently retrying or claiming success
        # would make the ambiguity invisible to the operator.
        self.store.record_result(result)
        logger.info(
            "publication_id=%s category=%s sink=%s action=%s item_count=%d",
            publication_id,
            publication.briefing.category,
            publisher.sink,
            result.status,
            len(publication.items),
        )
        return result
