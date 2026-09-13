"""Tests for the Phase 2A canonical publication layer."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from datasource import Item as PipelineItem
import run_pipelines as pipeline_runner
from publication import (
    IdentityConflictError,
    PublicationBriefingInput,
    PublicationFinalizer,
    PublicationItemInput,
    PublicationStore,
    StructuredPublicationAdapter,
    source_namespace,
    validate_bundle,
    validate_category,
    validate_item,
)
from publication.models import PublicationValidationError
from publication.serialization import (
    briefing_from_dict,
    bundle_content_hash,
    bundle_to_dict,
    canonical_json,
    deserialize_bundle,
    item_content_hash,
    item_from_dict,
    serialize_bundle,
)
from publication.store import CorruptPublicationError
from publication.identity import resolve_item_id


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures" / "publication_v1"


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 27, hour, minute, tzinfo=UTC)


def _item_input(category: str, index: int = 1, *, external_id: str | None = None):
    source_names = {
        "papers": "nature",
        "ai_news": "smolai_news",
        "code": "github_trending",
        "resource": "dlut_zhxw",
        "arxiv": "arxiv_cs_ai",
    }
    source_urls = {
        "papers": f"https://www.nature.com/articles/demo-{index}",
        "ai_news": f"https://news.smol.ai/p/demo-{index}",
        "code": f"https://github.com/dailyinfo/demo-{index}",
        "resource": f"https://news.dlut.edu.cn/info/demo-{index}.htm",
        "arxiv": f"https://arxiv.org/abs/2608.1234{index}",
    }
    default_external = {
        "papers": f"doi:10.1000/demo.{index}",
        "ai_news": f"smolai-{index}",
        "code": f"dailyinfo/demo-{index}",
        "resource": f"dlut-news-{index}",
        "arxiv": f"2608.1234{index}",
    }
    return PublicationItemInput(
        source_name=source_names[category],
        source_url=source_urls[category],
        external_id=external_id or default_external[category],
        source_published_at="2026-08-26",
        title=f"{category} item {index}",
        summary=f"Structured summary for {category} item {index}.",
        why_it_matters=f"Why the {category} item matters.",
        authors=["Researcher A", "Researcher B"],
        tags=[category, "ai-for-science", category],
        language="en" if category in {"papers", "ai_news", "arxiv"} else "zh",
        retrieved_at=_ts(1),
        published_at=_ts(1, 5),
    )


def _bundle(category: str = "papers", *, item_inputs=None):
    finalizer = PublicationFinalizer()
    briefing_input = PublicationBriefingInput(
        category=category,
        date="2026-08-27",
        title=f"{category} daily briefing",
        generated_at=_ts(1),
        published_at=_ts(1, 5),
        body=f"# {category} daily briefing\n\n1. structured item",
    )
    return finalizer.finalize(
        briefing_input,
        item_inputs or [_item_input(category)],
    )


@pytest.mark.parametrize("category", ["papers", "ai_news", "code", "resource", "arxiv"])
def test_realistic_category_fixtures_finalize(category):
    bundle = _bundle(category)
    assert bundle.briefing.category == category
    assert bundle.briefing.id == f"{category}-2026-08-27"
    assert len(bundle.items) == 1
    assert bundle.items[0].briefing_ids == [bundle.briefing.id]
    assert bundle.items[0].source.url.startswith(("http://", "https://"))


def test_category_contract_and_item_id_validation():
    assert validate_category("papers") == "papers"
    with pytest.raises(PublicationValidationError):
        validate_category("conference")
    with pytest.raises(PublicationValidationError):
        validate_category("social")

    item = _bundle().items[0]
    item.id = "../not-safe"
    with pytest.raises(PublicationValidationError, match="invalid Item.id"):
        validate_item(item)

    bad_explicit_id = _item_input("papers")
    bad_explicit_id.explicit_id = "../not-safe"
    with pytest.raises(PublicationValidationError, match="invalid Item.id"):
        _bundle(item_inputs=[bad_explicit_id])


def test_unsupported_schema_and_duplicate_identity_fail_closed():
    bundle = _bundle()
    bundle.schema_version = 2
    with pytest.raises(
        PublicationValidationError, match="unsupported PublicationBundle"
    ):
        validate_bundle(bundle)

    bundle = _bundle()
    bundle.items.append(deepcopy(bundle.items[0]))
    with pytest.raises(PublicationValidationError, match="duplicate Item identity"):
        validate_bundle(bundle)

    item = _bundle().items[0]
    item.schema_version = 2
    with pytest.raises(PublicationValidationError, match="unsupported Item"):
        validate_item(item)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a",
        "/relative",
        "https://localhost/a",
        "http://127.0.0.1/a",
        "https://10.0.0.1/a",
    ],
)
def test_public_source_url_fails_closed(url):
    item = _bundle().items[0]
    item.source.url = url
    with pytest.raises(PublicationValidationError):
        validate_item(item)


def test_naive_timestamp_fails_closed():
    with pytest.raises(PublicationValidationError, match="timezone-aware"):
        _bundle(
            item_inputs=[
                PublicationItemInput(
                    **{
                        **_item_input("papers").__dict__,
                        "retrieved_at": datetime(2026, 8, 27, 1),
                    }
                )
            ]
        )


def test_briefing_identity_and_source_date_are_deterministic():
    first = _bundle("arxiv")
    second = _bundle("arxiv")
    assert first.briefing.id == second.briefing.id == "arxiv-2026-08-27"
    assert first.items[0].id == second.items[0].id
    assert first.items[0].source_published_at == datetime(2026, 8, 25, 16, tzinfo=UTC)


def test_item_identity_ignores_mutable_content_and_timestamps():
    original = _item_input("papers")
    changed = deepcopy(original)
    changed.title = "Corrected title"
    changed.summary = "Corrected summary"
    changed.retrieved_at = _ts(4)
    changed.published_at = _ts(4, 5)
    assert (
        _bundle(item_inputs=[original]).items[0].id
        == _bundle(item_inputs=[changed]).items[0].id
    )


def test_lifecycle_timestamps_do_not_change_semantic_hash():
    first = _bundle()
    second = deepcopy(first)
    second.items[0].retrieved_at = _ts(2)
    second.items[0].published_at = _ts(2, 5)
    second.items[0].updated_at = _ts(3)
    second.briefing.generated_at = _ts(2)
    second.briefing.published_at = _ts(2, 5)
    second.briefing.updated_at = _ts(3)

    assert item_content_hash(first.items[0]) == item_content_hash(second.items[0])
    assert bundle_content_hash(first) == bundle_content_hash(second)


def test_external_id_namespace_isolation_and_determinism():
    first = resolve_item_id(
        source_name="OpenReview",
        source_url="https://openreview.net/forum?id=abc123",
        external_id="123",
    )
    same_namespace = resolve_item_id(
        source_name="Open Review",
        source_url="https://openreview.net/forum?id=other",
        external_id="123",
    )
    other_namespace = resolve_item_id(
        source_name="Other Source",
        source_url="https://example.org/items/123",
        external_id="123",
    )

    assert source_namespace("OpenReview") == source_namespace("open review")
    assert first == same_namespace
    assert first != other_namespace
    assert first == resolve_item_id(
        source_name="OpenReview",
        source_url="https://openreview.net/forum?id=abc123",
        external_id="123",
    )


def test_url_identity_ignores_common_tracking_parameters():
    first = _item_input("papers")
    first.external_id = None
    second = deepcopy(first)
    second.source_url += "?utm_source=dailyinfo&gclid=ignored"
    assert (
        _bundle(item_inputs=[first]).items[0].id
        == _bundle(item_inputs=[second]).items[0].id
    )


def test_source_url_is_canonicalized_and_invalid_lists_fail_closed():
    item = _item_input("papers")
    item.source_url += "?gclid=ignored&utm_source=dailyinfo"
    canonical = _bundle(item_inputs=[item]).items[0]
    assert canonical.source.url.endswith("/articles/demo-1")

    raw = PipelineItem(
        title="Invalid structured item",
        date="2026-08-26",
        url="https://example.org/articles/invalid",
        extra={"summary": "A summary", "authors": None, "tags": []},
    )
    adapted = StructuredPublicationAdapter.item_from_pipeline(
        raw,
        source_name="example_rss",
        retrieved_at=_ts(1),
        published_at=_ts(1, 5),
    )
    with pytest.raises(PublicationValidationError, match="authors"):
        _bundle(item_inputs=[adapted])


def test_bidirectional_integrity_rejects_missing_and_mismatched_relationships():
    bundle = _bundle()
    bundle.items[0].briefing_ids = []
    with pytest.raises(PublicationValidationError, match="missing Briefing.id"):
        validate_bundle(bundle)

    bundle = _bundle()
    bundle.briefing.item_ids = []
    with pytest.raises(PublicationValidationError, match="missing Item.id"):
        validate_bundle(bundle)

    bundle = _bundle()
    bundle.items[0].category = "code"
    with pytest.raises(PublicationValidationError, match="categories"):
        validate_bundle(bundle)


def test_security_guard_blocks_secrets_and_debug_paths():
    item = _bundle().items[0]
    item.summary = "Authorization: Bearer abcdefghijklmnop"
    with pytest.raises(PublicationValidationError):
        validate_item(item)

    item = _bundle().items[0]
    item.summary = "Debug file at /Users/cylenlc/private/token.json"
    with pytest.raises(PublicationValidationError):
        validate_item(item)


def test_serialization_round_trip_and_deterministic_hash():
    bundle = _bundle()
    payload = serialize_bundle(bundle)
    restored = deserialize_bundle(payload)
    assert bundle_to_dict(restored) == bundle_to_dict(bundle)
    assert bundle_content_hash(restored) == bundle_content_hash(bundle)

    reordered = deepcopy(bundle)
    reordered.items = list(reversed(reordered.items))
    reordered.items[0].tags = list(reversed(reordered.items[0].tags))
    reordered.items[0].briefing_ids = list(reversed(reordered.items[0].briefing_ids))
    assert bundle_content_hash(reordered) == bundle_content_hash(bundle)
    assert bundle_to_dict(reordered) == bundle_to_dict(bundle)
    assert canonical_json({"b": 2, "a": "中文"}) == canonical_json(
        {"a": "中文", "b": 2}
    )


def test_golden_contract_fixture_round_trip():
    payload = (FIXTURES / "bundle.json").read_text(encoding="utf-8")
    bundle = deserialize_bundle(payload)
    assert bundle_to_dict(bundle) == json.loads(payload)
    assert (
        deserialize_bundle(serialize_bundle(bundle)).briefing.id == "papers-2026-08-27"
    )
    assert (
        item_from_dict(
            json.loads((FIXTURES / "item.json").read_text(encoding="utf-8"))
        ).id
        == "nature-2508.12345"
    )
    assert (
        briefing_from_dict(
            json.loads((FIXTURES / "briefing.json").read_text(encoding="utf-8"))
        ).id
        == "papers-2026-08-27"
    )


def test_pipeline_shaped_item_uses_structured_fields_without_markdown_parsing():
    raw = PipelineItem(
        title="A paper from the current RSS pipeline",
        date="2026-08-26",
        url="https://example.org/articles/42",
        content="This raw article body is not a canonical summary.",
        extra={
            "external_id": "rss-guid-42",
            "summary": "Explicit structured summary retained by an adapter.",
            "why_it_matters": "The adapter keeps the source metadata available.",
            "authors": ["Author"],
            "tags": ["rss"],
            "content_language": "en",
        },
    )
    adapted = StructuredPublicationAdapter.item_from_pipeline(
        raw,
        source_name="example_rss",
        retrieved_at=_ts(1),
        published_at=_ts(1, 5),
    )
    bundle = _bundle("papers", item_inputs=[adapted])
    assert bundle.items[0].summary.startswith("Explicit structured")
    assert bundle.items[0].source.external_id == "rss-guid-42"


def test_existing_pipeline_helper_to_finalizer_store_round_trip(tmp_path):
    raw = PipelineItem(
        title="A paper retained by the papers pipeline",
        date="2026-08-26",
        url="https://example.org/papers/42",
        extra={
            "external_id": "rss-guid-42",
            "summary": "A structured summary is carried beside the generated body.",
            "authors": ["Pipeline Author"],
            "tags": ["hydrology"],
            "content_language": "en",
        },
    )
    body, returned_items = pipeline_runner._merge_briefing_parts(
        SimpleNamespace(display_name="Nature"),
        [("1. **A paper retained by the papers pipeline** - summary", [raw])],
    )
    adapted = StructuredPublicationAdapter.items(
        returned_items,
        source_name="nature",
        retrieved_at=_ts(1),
        published_at=_ts(1, 5),
    )
    briefing = PublicationBriefingInput(
        category="papers",
        date="2026-08-27",
        title="papers daily briefing",
        generated_at=_ts(1),
        published_at=_ts(1, 5),
        body=body,
    )
    bundle = PublicationFinalizer().finalize(briefing, adapted)
    store = PublicationStore(tmp_path / "publications")
    store.save(bundle)
    assert store.load_bundle(bundle.briefing.id).items[0].id == bundle.items[0].id


def test_store_create_readback_noop_update_and_atomic_overwrite(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    first = _bundle()
    assert store.save(first).action == "create"
    assert store.load_item(first.items[0].id).id == first.items[0].id
    assert store.load_briefing(first.briefing.id).id == first.briefing.id
    assert store.load_bundle(first.briefing.id).briefing.body == first.briefing.body

    rerun = deepcopy(first)
    rerun.briefing.generated_at = _ts(2)
    rerun.briefing.published_at = _ts(2, 5)
    rerun.briefing.updated_at = _ts(2, 10)
    for item in rerun.items:
        item.retrieved_at = _ts(2)
        item.published_at = _ts(2, 5)
        item.updated_at = _ts(2, 10)
    first_rerun = store.save(rerun)
    assert first_rerun.action == "update"
    reloaded = store.load_bundle(first.briefing.id)
    assert reloaded.items[0].retrieved_at == _ts(2)
    assert reloaded.items[0].published_at == first.items[0].published_at
    assert reloaded.items[0].updated_at == _ts(2, 10)
    assert reloaded.briefing.generated_at == _ts(2)
    assert reloaded.briefing.published_at == first.briefing.published_at
    assert reloaded.briefing.updated_at == _ts(2, 10)
    assert store.save(rerun).action == "noop"

    source_metadata_update = deepcopy(first)
    source_metadata_update.items[0].updated_at = _ts(3)
    assert item_content_hash(source_metadata_update.items[0]) == item_content_hash(
        first.items[0]
    )
    assert store.save(source_metadata_update).action == "update"

    updated = deepcopy(source_metadata_update)
    updated.items[0].summary = "A semantically changed summary."
    assert store.save(updated).action == "update"
    loaded = store.load_bundle(first.briefing.id)
    assert loaded.items[0].summary == "A semantically changed summary."
    assert (
        len(list((tmp_path / "publications" / "briefings").rglob("briefing.json"))) == 1
    )
    assert not list((tmp_path / "publications").rglob("*.tmp"))


def test_store_rejects_item_category_identity_migration(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    store.save(
        _bundle("papers", item_inputs=[_item_input("papers", external_id="same-id")])
    )
    conflicting = _bundle(
        "code", item_inputs=[_item_input("code", external_id="same-id")]
    )
    # Use the same exact item identity while keeping the incoming category code.
    conflicting.items[0].id = store.load_bundle("papers-2026-08-27").items[0].id
    conflicting.items[0].briefing_ids = [conflicting.briefing.id]
    conflicting.briefing.item_ids = [conflicting.items[0].id]
    with pytest.raises(IdentityConflictError, match="item identity migration"):
        store.save(conflicting)


def test_store_rejects_corrupt_existing_file(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    bundle = _bundle()
    store.save(bundle)
    briefing_path = (
        tmp_path
        / "publications"
        / "briefings"
        / "2026"
        / "08"
        / "27"
        / "papers"
        / "briefing.json"
    )
    briefing_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptPublicationError):
        store.load_bundle(bundle.briefing.id)


def test_store_global_integrity_rejects_missing_item(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    bundle = _bundle()
    store.save(bundle)
    item_path = next((tmp_path / "publications" / "items").rglob("*.json"))
    item_path.unlink()
    with pytest.raises(CorruptPublicationError, match="missing Item"):
        store.load_bundle(bundle.briefing.id)


def test_store_global_integrity_rejects_reverse_relationship_mismatch(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    bundle = _bundle()
    store.save(bundle)
    item_path = next((tmp_path / "publications" / "items").rglob("*.json"))
    item_data = json.loads(item_path.read_text(encoding="utf-8"))
    item_data["briefing_ids"] = []
    item_path.write_text(json.dumps(item_data), encoding="utf-8")
    with pytest.raises(CorruptPublicationError, match="reverse relationship"):
        store.validate_integrity()


def test_store_preserves_item_relationships_across_briefings(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    first = _bundle()
    store.save(first)
    second_input = PublicationBriefingInput(
        category="papers",
        date="2026-08-28",
        title="papers second briefing",
        generated_at=_ts(1),
        published_at=_ts(1, 5),
        body="# second",
    )
    second = PublicationFinalizer().finalize(second_input, [_item_input("papers")])
    second.items[0].id = first.items[0].id
    second.briefing.item_ids = [first.items[0].id]
    assert store.save(second).action == "create"
    assert set(store.load_item(first.items[0].id).briefing_ids) == {
        "papers-2026-08-27",
        "papers-2026-08-28",
    }
    assert item_content_hash(first.items[0]) == item_content_hash(second.items[0])
    assert store.save(second).action == "noop"
    assert store.load_item(first.items[0].id).briefing_ids == [
        "papers-2026-08-27",
        "papers-2026-08-28",
    ]
    store.validate_integrity()


def test_store_persists_relationship_only_removal(tmp_path):
    store = PublicationStore(tmp_path / "publications")
    first = _bundle(item_inputs=[_item_input("papers", 1), _item_input("papers", 2)])
    store.save(first)
    removed = first.items[1]

    reduced = deepcopy(first)
    reduced.items = [reduced.items[0]]
    reduced.briefing.item_ids = [reduced.items[0].id]

    assert store.save(reduced).action == "update"
    reloaded_removed = store.load_item(removed.id)
    assert item_content_hash(removed) == item_content_hash(reloaded_removed)
    assert reloaded_removed.briefing_ids == []
    store.validate_integrity()
