"""Phase 2B-F source-time and stable-identity contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json

from datasource import Item as PipelineItem
from publication import (
    PublicationBriefingInput,
    PublicationFinalizer,
    PublicationItemInput,
    StructuredPublicationAdapter,
    bundle_content_hash,
    deserialize_bundle,
    serialize_bundle,
)
from publication.identity import resolve_item_id


UTC = timezone.utc


def _ts(hour: int) -> datetime:
    return datetime(2026, 8, 27, hour, tzinfo=UTC)


def _briefing(category: str = "papers") -> PublicationBriefingInput:
    return PublicationBriefingInput(
        category=category,
        date="2026-08-27",
        title=f"{category} briefing",
        generated_at=_ts(2),
        published_at=_ts(3),
        body=f"# {category}\n\n1. source item",
    )


def _item(
    *,
    category: str = "papers",
    source_name: str = "example_source",
    url: str = "https://example.org/items/1",
    external_id: str | None = "source-item-1",
    source_published_at: str | None = "2026-08-25T12:00:00+00:00",
) -> PublicationItemInput:
    return PublicationItemInput(
        source_name=source_name,
        source_url=url,
        external_id=external_id,
        source_published_at=source_published_at,
        title="Stable source title",
        summary="A structured summary.",
        authors=["Source Author"],
        tags=["test"],
        language="en",
        retrieved_at=_ts(1),
        published_at=_ts(3),
    )


def test_observation_date_is_not_source_publication_time():
    raw = PipelineItem(
        title="Observed listing",
        date="2026-08-27",
        url="https://example.org/listing/1",
        extra={"summary": "A structured summary.", "content_language": "en"},
    )
    adapted = StructuredPublicationAdapter.item_from_pipeline(
        raw,
        source_name="example_source",
        retrieved_at=_ts(1),
        published_at=_ts(3),
    )

    assert adapted.source_published_at is None
    canonical = PublicationFinalizer().finalize(_briefing(), [adapted]).items[0]
    assert canonical.source_published_at is None
    assert canonical.retrieved_at == _ts(1)
    assert canonical.published_at == _ts(3)


def test_rss_without_guid_uses_namespaced_url_fallback():
    raw = PipelineItem(
        title="RSS item without GUID",
        date="2026-08-27",
        url="https://example.org/items/no-guid",
        extra={"summary": "A structured summary.", "content_language": "en"},
    )
    adapted = StructuredPublicationAdapter.item_from_pipeline(
        raw,
        source_name="example_rss",
        retrieved_at=_ts(1),
        published_at=_ts(3),
    )
    canonical = PublicationFinalizer().finalize(_briefing(), [adapted]).items[0]

    assert canonical.source.external_id is None
    assert canonical.id == resolve_item_id(
        source_name="example_rss",
        source_url=raw.url,
        external_id=None,
    )


def test_missing_source_time_is_deterministic_null_and_present_time_is_hashed():
    missing = PublicationFinalizer().finalize(
        _briefing(), [_item(source_published_at=None)]
    )
    missing_again = PublicationFinalizer().finalize(
        _briefing(), [_item(source_published_at=None)]
    )
    present = PublicationFinalizer().finalize(_briefing(), [_item()])

    payload = json.loads(serialize_bundle(missing))
    assert payload["items"][0]["source_published_at"] is None
    assert bundle_content_hash(missing) == bundle_content_hash(missing_again)
    assert bundle_content_hash(missing) != bundle_content_hash(present)
    assert (
        deserialize_bundle(serialize_bundle(missing)).items[0].source_published_at
        is None
    )


def test_arxiv_versions_normalize_to_one_base_item_identity():
    version_one = _item(
        category="arxiv",
        source_name="arxiv_cs_ai",
        url="https://arxiv.org/abs/2608.12345v1",
        external_id=None,
    )
    version_two = _item(
        category="arxiv",
        source_name="arxiv_cs_ai",
        url="https://arxiv.org/abs/2608.12345v2",
        external_id="2608.12345v2",
    )

    first = PublicationFinalizer().finalize(_briefing("arxiv"), [version_one]).items[0]
    second = PublicationFinalizer().finalize(_briefing("arxiv"), [version_two]).items[0]

    assert first.source.external_id == second.source.external_id == "2608.12345"
    assert first.id == second.id == "arxiv-2608.12345"


def test_doi_representations_normalize_to_one_identity():
    first = (
        PublicationFinalizer()
        .finalize(
            _briefing(),
            [_item(source_name="crossref", external_id="10.1234/ABC")],
        )
        .items[0]
    )
    second = (
        PublicationFinalizer()
        .finalize(
            _briefing(),
            [
                _item(
                    source_name="crossref",
                    external_id="https://doi.org/10.1234/abc",
                    url="https://doi.org/10.1234/abc",
                )
            ],
        )
        .items[0]
    )

    assert first.source.external_id == second.source.external_id == "10.1234/abc"
    assert first.id == second.id


def test_malformed_known_identity_does_not_become_hashed_identity():
    canonical = (
        PublicationFinalizer()
        .finalize(
            _briefing(),
            [
                _item(
                    source_name="crossref",
                    external_id="doi:not-a-doi",
                    url="https://example.org/papers/1",
                )
            ],
        )
        .items[0]
    )

    assert canonical.source.external_id is None
    assert canonical.id == resolve_item_id(
        source_name="crossref",
        source_url="https://example.org/papers/1",
        external_id=None,
    )


def test_source_namespaces_isolate_equal_external_ids():
    github = resolve_item_id(
        source_name="github_trending",
        source_url="https://github.com/org/repo",
        external_id="org/repo",
    )
    huggingface = resolve_item_id(
        source_name="huggingface_models",
        source_url="https://huggingface.co/org/repo",
        external_id="org/repo",
    )
    assert github != huggingface


def test_github_full_name_and_huggingface_repo_id_are_machine_identities():
    github_raw = PipelineItem(
        title="Trending display description",
        date="2026-08-27",
        url="https://github.com/org/repo",
        extra={
            "full_name": "org/repo",
            "summary": "A structured summary.",
        },
    )
    hf_raw = PipelineItem(
        title="Display name can change",
        date="2026-08-27",
        url="https://huggingface.co/org/model",
        extra={
            "repo_id": "org/model",
            "name": "A mutable display name",
            "summary": "A structured summary.",
        },
    )

    github = StructuredPublicationAdapter.item_from_pipeline(
        github_raw,
        source_name="github_trending",
        retrieved_at=_ts(1),
        published_at=_ts(3),
    )
    hf = StructuredPublicationAdapter.item_from_pipeline(
        hf_raw,
        source_name="huggingface_models",
        retrieved_at=_ts(1),
        published_at=_ts(3),
    )

    assert github.external_id == "org/repo"
    assert hf.external_id == "org/model"
