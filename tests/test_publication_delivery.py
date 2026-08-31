"""Phase 2C publisher and delivery-state contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from publication import (
    CorruptDeliveryStateError,
    DeliveryCoordinator,
    DeliveryState,
    DeliveryStateStore,
    DeliveryStoreError,
    DeliveryValidationError,
    DiscordPublisher,
    PublicationBriefingInput,
    PublicationFinalizer,
    PublicationItemInput,
    PublicationStore,
    PublishResult,
    bundle_content_hash,
    delivery_key,
    serialize_bundle,
    serialize_delivery_state,
)


UTC = timezone.utc
ATTEMPT_1 = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)
ATTEMPT_2 = datetime(2026, 8, 27, 3, 1, tzinfo=UTC)


def _bundle(category: str = "papers"):
    return PublicationFinalizer().finalize(
        PublicationBriefingInput(
            category=category,
            date="2026-08-27",
            title=f"{category} briefing",
            generated_at=ATTEMPT_1,
            published_at=ATTEMPT_1,
            body=f"# {category}\n\nCanonical briefing body.",
        ),
        [
            PublicationItemInput(
                source_name="nature",
                source_url="https://www.nature.com/articles/delivery-demo",
                external_id="doi:10.1000/delivery-demo",
                source_published_at=None,
                title="A canonical delivery item",
                summary="A structured item summary.",
                why_it_matters="It exercises the delivery boundary.",
                authors=["Researcher"],
                tags=["delivery"],
                language="en",
                retrieved_at=ATTEMPT_1,
                published_at=ATTEMPT_1,
            )
        ],
    )


class FakePublisher:
    def __init__(self, sink="discord", outcomes=("success",)):
        self.sink = sink
        self.outcomes = list(outcomes)
        self.calls = []
        self.times = [ATTEMPT_1, ATTEMPT_2]

    def publish(self, publication):
        self.calls.append(publication)
        status = self.outcomes.pop(0) if self.outcomes else "success"
        return PublishResult(
            sink=self.sink,
            publication_id=publication.briefing.id,
            status=status,
            attempted_at=self.times[min(len(self.calls) - 1, len(self.times) - 1)],
            error="HTTP failure" if status == "failed" else None,
        )


def _coordinator(store):
    times = iter([ATTEMPT_1, ATTEMPT_2])
    return DeliveryCoordinator(store, clock=lambda: next(times))


def test_delivery_identity_is_deterministic_and_sink_specific():
    assert delivery_key("papers-2026-08-27", "discord") == "papers-2026-08-27:discord"
    assert delivery_key("papers-2026-08-27", "discord") != delivery_key(
        "papers-2026-08-27", "web"
    )
    with pytest.raises(DeliveryValidationError):
        delivery_key("papers-2026-08-27", "Discord")
    with pytest.raises(DeliveryValidationError):
        delivery_key("papers-2026-8-27", "discord")


def test_delivery_state_serialization_is_deterministic_and_atomic(tmp_path):
    store = DeliveryStateStore(tmp_path / "deliveries")
    state = store.begin_attempt("papers-2026-08-27", "discord", attempted_at=ATTEMPT_1)
    payload = serialize_delivery_state(state)
    assert json.loads(payload)["first_attempted_at"].endswith("Z")
    assert store.load("papers-2026-08-27", "discord") == state
    store.begin_attempt("papers-2026-08-27", "discord", attempted_at=ATTEMPT_2)
    assert not list((tmp_path / "deliveries" / "discord").glob("*.tmp"))


def test_delivery_state_rejects_naive_timestamp_and_unknown_schema():
    with pytest.raises(DeliveryValidationError):
        DeliveryStateStore().save(
            DeliveryState(
                schema_version=1,
                briefing_id="papers-2026-08-27",
                sink="discord",
                status="pending",
                attempt_count=1,
                first_attempted_at=datetime(2026, 8, 27, 3),
                last_attempted_at=ATTEMPT_1,
                delivered_at=None,
            )
        )
    with pytest.raises(DeliveryValidationError):
        DeliveryStateStore().save(
            DeliveryState(
                schema_version=2,
                briefing_id="papers-2026-08-27",
                sink="discord",
                status="pending",
                attempt_count=0,
                first_attempted_at=None,
                last_attempted_at=None,
                delivered_at=None,
            )
        )


def test_first_delivery_success_then_normal_repeat_is_noop(tmp_path):
    bundle = _bundle()
    publication_store = PublicationStore(tmp_path / "publications")
    publication_store.save(bundle)
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    publisher = FakePublisher()

    coordinator = _coordinator(delivery_store)
    first = coordinator.publish(bundle, publisher)
    second = coordinator.publish(bundle, publisher)

    assert first.status == "success"
    assert second.status == "skipped"
    assert len(publisher.calls) == 1
    state = delivery_store.load(bundle.briefing.id, "discord")
    assert state is not None
    assert state.status == "success"
    assert state.attempt_count == 1


def test_push_main_uses_canonical_bundle_and_skips_second_send(tmp_path, monkeypatch):
    import push_to_discord as push

    bundle = _bundle()
    publication_store = PublicationStore(tmp_path / "publications")
    publication_store.save(bundle)
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    monkeypatch.setattr(push, "PublicationStore", lambda: publication_store)
    monkeypatch.setattr(push, "DeliveryStateStore", lambda: delivery_store)
    monkeypatch.setattr(push, "DISCORD_CHANNELS", {"papers": "channel-1"})
    sent = []
    monkeypatch.setattr(
        push, "send_to_discord", lambda *args: sent.append(args) or True
    )

    first_exit = push.main("2026-08-27", categories=["papers"])
    second_exit = push.main("2026-08-27", categories=["papers"])

    assert first_exit == 0
    assert second_exit == 0
    assert sent == [("channel-1", bundle.briefing.body)]


def test_failed_delivery_is_retryable_and_attempt_count_increments(tmp_path):
    bundle = _bundle()
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    publisher = FakePublisher(outcomes=("failed", "success"))
    coordinator = _coordinator(delivery_store)

    first = coordinator.publish(bundle, publisher)
    second = coordinator.publish(bundle, publisher)

    assert first.status == "failed"
    assert second.status == "success"
    assert len(publisher.calls) == 2
    state = delivery_store.load(bundle.briefing.id, "discord")
    assert state is not None
    assert state.status == "success"
    assert state.attempt_count == 2


def test_force_retries_a_successful_delivery_with_same_identity(tmp_path):
    bundle = _bundle()
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    publisher = FakePublisher(outcomes=("success", "success"))
    coordinator = _coordinator(delivery_store)

    coordinator.publish(bundle, publisher)
    forced = coordinator.publish(bundle, publisher, force=True)

    assert forced.status == "success"
    assert len(publisher.calls) == 2
    state = delivery_store.load(bundle.briefing.id, "discord")
    assert state is not None and state.attempt_count == 2


def test_pending_from_interrupted_process_is_retryable(tmp_path):
    bundle = _bundle()
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    delivery_store.begin_attempt(bundle.briefing.id, "discord", attempted_at=ATTEMPT_1)
    publisher = FakePublisher()

    result = _coordinator(delivery_store).publish(bundle, publisher)

    assert result.status == "success"
    assert len(publisher.calls) == 1
    state = delivery_store.load(bundle.briefing.id, "discord")
    assert state is not None and state.attempt_count == 2


def test_external_success_with_local_state_failure_is_reported(tmp_path):
    bundle = _bundle()

    class FailingResultStore(DeliveryStateStore):
        def record_result(self, result):
            raise DeliveryStoreError("state disk unavailable")

    delivery_store = FailingResultStore(tmp_path / "deliveries")
    publisher = FakePublisher()

    with pytest.raises(DeliveryStoreError, match="state disk unavailable"):
        _coordinator(delivery_store).publish(bundle, publisher)
    assert len(publisher.calls) == 1
    pending = delivery_store.load(bundle.briefing.id, "discord")
    assert pending is not None and pending.status == "pending"


def test_sink_states_are_independent(tmp_path):
    bundle = _bundle()
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    coordinator = _coordinator(delivery_store)

    discord = FakePublisher("discord", ("success",))
    web = FakePublisher("web", ("failed",))
    coordinator.publish(bundle, discord)
    coordinator.publish(bundle, web)

    assert delivery_store.load(bundle.briefing.id, "discord").status == "success"
    assert delivery_store.load(bundle.briefing.id, "web").status == "failed"


def test_corrupt_delivery_state_fails_closed_without_publishing(tmp_path):
    bundle = _bundle()
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    path = delivery_store._path(bundle.briefing.id, "discord")
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")
    publisher = FakePublisher()

    with pytest.raises(CorruptDeliveryStateError):
        _coordinator(delivery_store).publish(bundle, publisher)
    assert publisher.calls == []


def test_discord_publisher_consumes_only_canonical_briefing_body():
    bundle = _bundle()
    sent = []
    publisher = DiscordPublisher(
        "channel-1",
        transport=lambda channel, body: sent.append((channel, body)) or True,
        clock=lambda: ATTEMPT_1,
    )

    result = publisher.publish(bundle)

    assert result.status == "success"
    assert sent == [("channel-1", bundle.briefing.body)]


def test_partial_discord_chunk_failure_is_failed(monkeypatch):
    import push_to_discord as legacy_discord

    responses = iter(
        [
            type("Response", (), {"status_code": 200, "text": ""})(),
            type("Response", (), {"status_code": 500, "text": "server error"})(),
        ]
    )
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append(json["content"])
        return next(responses)

    monkeypatch.setattr(legacy_discord.requests, "post", fake_post)
    monkeypatch.setattr(legacy_discord.time, "sleep", lambda *_: None)
    publisher = DiscordPublisher(
        "channel-1", transport=legacy_discord.send_to_discord, clock=lambda: ATTEMPT_1
    )
    bundle = _bundle()
    bundle.briefing.body = "line\n" + ("x" * 2050) + "\nend"

    result = publisher.publish(bundle)

    assert result.status == "failed"
    assert len(calls) == 2


def test_legacy_pushed_file_bootstraps_success_without_resend(tmp_path, monkeypatch):
    import push_to_discord as push

    bundle = _bundle()
    publication_store = PublicationStore(tmp_path / "publications")
    publication_store.save(bundle)
    monkeypatch.setattr(push, "PUSHED_DIR", tmp_path / "pushed")
    archive = push.PUSHED_DIR / "papers"
    archive.mkdir(parents=True)
    (archive / "nature_briefing_2026-08-27.md").write_text(
        "historical Discord briefing", encoding="utf-8"
    )
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")
    sent = []
    monkeypatch.setattr(
        push, "send_to_discord", lambda *args: sent.append(args) or True
    )

    result, errors = push.publish_canonical_category(
        "papers",
        "channel-1",
        "2026-08-27",
        publication_store=publication_store,
        delivery_store=delivery_store,
    )

    assert result.status == "skipped"
    assert errors == []
    assert sent == []
    state = delivery_store.load(bundle.briefing.id, "discord")
    assert state is not None and state.status == "success" and state.attempt_count == 0


def test_canonical_publish_does_not_mutate_publication_when_delivery_state_changes(
    tmp_path, monkeypatch
):
    import push_to_discord as push

    bundle = _bundle()
    publication_store = PublicationStore(tmp_path / "publications")
    publication_store.save(bundle)
    before = publication_store.load_bundle(bundle.briefing.id)
    before_json = serialize_bundle(before)
    before_hash = bundle_content_hash(before)
    monkeypatch.setattr(push, "send_to_discord", lambda *args: True)

    result, _ = push.publish_canonical_category(
        "papers",
        "channel-1",
        "2026-08-27",
        publication_store=publication_store,
        delivery_store=DeliveryStateStore(tmp_path / "deliveries"),
    )

    after = publication_store.load_bundle(bundle.briefing.id)
    assert result.status == "success"
    assert serialize_bundle(after) == before_json
    assert bundle_content_hash(after) == before_hash


def test_canonical_missing_is_not_silently_replaced_by_markdown(tmp_path):
    import push_to_discord as push

    pending = tmp_path / "briefings" / "papers"
    pending.mkdir(parents=True)
    (pending / "nature_briefing_2026-08-27.md").write_text(
        "legacy body", encoding="utf-8"
    )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(push, "BRIEFINGS_DIR", tmp_path / "briefings")
    try:
        with pytest.raises(FileNotFoundError):
            push.publish_canonical_category(
                "papers",
                "channel-1",
                "2026-08-27",
                publication_store=PublicationStore(tmp_path / "publications"),
                delivery_store=DeliveryStateStore(tmp_path / "deliveries"),
            )
    finally:
        monkeypatch.undo()


def test_archive_failure_does_not_trigger_second_discord_send(tmp_path, monkeypatch):
    import push_to_discord as push

    bundle = _bundle()
    publication_store = PublicationStore(tmp_path / "publications")
    publication_store.save(bundle)
    monkeypatch.setattr(push, "BRIEFINGS_DIR", tmp_path / "briefings")
    monkeypatch.setattr(push, "PUSHED_DIR", tmp_path / "pushed")
    pending = push.BRIEFINGS_DIR / "papers"
    pending.mkdir(parents=True)
    (pending / "nature_briefing_2026-08-27.md").write_text(
        "# A real legacy body\n\n" + "内容" * 100, encoding="utf-8"
    )
    sent = []
    monkeypatch.setattr(
        push, "send_to_discord", lambda *args: sent.append(args) or True
    )
    monkeypatch.setattr(
        push.shutil, "move", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    delivery_store = DeliveryStateStore(tmp_path / "deliveries")

    first, errors = push.publish_canonical_category(
        "papers",
        "channel-1",
        "2026-08-27",
        publication_store=publication_store,
        delivery_store=delivery_store,
    )
    second, _ = push.publish_canonical_category(
        "papers",
        "channel-1",
        "2026-08-27",
        publication_store=publication_store,
        delivery_store=delivery_store,
    )

    assert first.status == "success" and errors
    assert second.status == "skipped"
    assert len(sent) == 1


def test_missing_canonical_real_legacy_file_fails_main_without_send(
    tmp_path, monkeypatch
):
    import push_to_discord as push

    monkeypatch.setattr(push, "BRIEFINGS_DIR", tmp_path / "briefings")
    monkeypatch.setattr(push, "PUSHED_DIR", tmp_path / "pushed")
    monkeypatch.setattr(
        push, "PublicationStore", lambda: PublicationStore(tmp_path / "publications")
    )
    monkeypatch.setattr(
        push, "DeliveryStateStore", lambda: DeliveryStateStore(tmp_path / "deliveries")
    )
    monkeypatch.setattr(push, "DISCORD_CHANNELS", {"papers": "channel-1"})
    pending = push.BRIEFINGS_DIR / "papers"
    pending.mkdir(parents=True)
    (pending / "nature_briefing_2026-08-27.md").write_text(
        "# Legacy real briefing\n\n" + "内容" * 100, encoding="utf-8"
    )
    sent = []
    monkeypatch.setattr(
        push, "send_to_discord", lambda *args: sent.append(args) or True
    )

    assert push.main("2026-08-27", categories=["papers"]) == 1
    assert sent == []
