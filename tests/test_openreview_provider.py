from types import SimpleNamespace

import pytest


def _capabilities():
    from openreview_provider import VenueCapabilities

    return VenueCapabilities(
        venue_id="ICLR.cc/2026/Conference",
        submission_invitation="ICLR.cc/2026/Conference/-/Submission",
        submission_venue_id="ICLR.cc/2026/Conference/Submission",
    )


def test_content_value_unwraps_v2_and_plain_values():
    from openreview_provider import content_value

    content = {"title": {"value": "Paper"}, "year": 2026}
    assert content_value(content, "title") == "Paper"
    assert content_value(content, "year") == 2026
    assert content_value(content, "missing", "fallback") == "fallback"


def test_discover_venue_uses_dynamic_submission_name():
    from openreview_provider import OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Blind_Submission"},
            "submission_venue_id": {"value": "Venue/Under_Review"},
        }
    )
    client = SimpleNamespace(get_group=lambda _venue: group)
    provider = OpenReviewProvider({"venue_id": "Venue/2026/Conference"}, client=client)

    capabilities = provider.discover_venue()

    assert (
        capabilities.submission_invitation == "Venue/2026/Conference/-/Blind_Submission"
    )
    assert capabilities.submission_venue_id == "Venue/Under_Review"


def test_discover_venue_reports_private_submissions():
    from openreview_provider import OpenReviewNotPublic, OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Submission"},
            "public_submissions": {"value": False},
        }
    )
    provider = OpenReviewProvider(
        {"venue_id": "NeurIPS.cc/2026/Conference"},
        client=SimpleNamespace(get_group=lambda _venue: group),
    )

    with pytest.raises(OpenReviewNotPublic, match="does not expose public submissions"):
        provider.discover_venue()


def test_authenticated_discover_venue_allows_public_notes_when_group_flag_is_false():
    from openreview_provider import OpenReviewProvider

    group = SimpleNamespace(
        content={
            "submission_name": {"value": "Submission"},
            "public_submissions": {"value": False},
        }
    )
    provider = OpenReviewProvider(
        {"venue_id": "ICML.cc/2026/Conference"},
        client=SimpleNamespace(get_group=lambda _venue: group),
    )
    provider._authenticated = True

    capabilities = provider.discover_venue()

    assert capabilities.submission_invitation == (
        "ICML.cc/2026/Conference/-/Submission"
    )


def test_authenticated_public_only_filters_private_notes():
    from openreview_provider import OpenReviewProvider

    public = {
        "id": "public",
        "forum": "public",
        "readers": ["everyone"],
        "content": {"title": {"value": "Public"}},
    }
    private = {
        "id": "private",
        "forum": "private",
        "readers": ["Venue/Authors"],
        "content": {"title": {"value": "Private"}},
    }
    client = SimpleNamespace(get_all_notes=lambda **_kwargs: [public, private])
    provider = OpenReviewProvider(
        {"venue_id": "ICLR.cc/2026/Conference", "public_only": True},
        client=client,
    )
    provider._authenticated = True

    papers = provider.fetch_submissions(_capabilities())

    assert [paper["title"] for paper in papers] == ["Public"]


def test_submission_normalizes_link_to_code_alias():
    from openreview_provider import OpenReviewProvider

    note = SimpleNamespace(
        id="paper-1",
        forum="paper-1",
        readers=["everyone"],
        content={
            "title": {"value": "Paper"},
            "Link To Code": {"value": "https://github.com/example/paper"},
        },
        invitations=["Venue/2026/Conference/-/Submission"],
        cdate=1,
        tmdate=2,
    )
    provider = OpenReviewProvider(
        {"venue_id": "Venue/2026/Conference"}, client=SimpleNamespace()
    )

    paper = provider.normalize_submission(note, _capabilities())

    assert paper["code_url"] == "https://github.com/example/paper"


def test_partial_credentials_are_rejected(monkeypatch):
    import openreview_provider as provider_module
    from openreview_provider import OpenReviewConfigError, OpenReviewProvider

    monkeypatch.setattr(provider_module, "ENV_FILE", provider_module.Path("/missing"))
    monkeypatch.setenv("OPENREVIEW_USERNAME", "user@example.com")
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)
    with pytest.raises(OpenReviewConfigError, match="must be set together"):
        OpenReviewProvider({"venue_id": "Venue/2026/Conference"})


def test_credentials_can_be_loaded_from_project_env(tmp_path, monkeypatch):
    import openreview_provider as provider_module

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENREVIEW_USERNAME=user@example.com\nOPENREVIEW_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provider_module, "ENV_FILE", env_file)
    monkeypatch.delenv("OPENREVIEW_USERNAME", raising=False)
    monkeypatch.delenv("OPENREVIEW_PASSWORD", raising=False)

    assert provider_module._load_env_value("OPENREVIEW_USERNAME") == "user@example.com"
    assert provider_module._load_env_value("OPENREVIEW_PASSWORD") == "secret"


def test_nonpublic_pipeline_mode_is_rejected():
    from openreview_provider import OpenReviewConfigError, OpenReviewProvider

    with pytest.raises(OpenReviewConfigError, match="public_only=false"):
        OpenReviewProvider(
            {"venue_id": "Venue/2026/Conference", "public_only": False},
            client=SimpleNamespace(),
        )


def test_openreview_errors_have_stable_outcomes():
    from openreview_provider import (
        OpenReviewConfigError,
        OpenReviewNotPublic,
        classify_openreview_error,
    )

    assert (
        classify_openreview_error(OpenReviewConfigError("bad config"))
        == "INVALID_CONFIG"
    )
    assert classify_openreview_error(OpenReviewNotPublic("private")) == "NOT_PUBLIC"
    assert (
        classify_openreview_error(RuntimeError("Invalid username or password"))
        == "AUTH_REQUIRED"
    )
    assert (
        classify_openreview_error(RuntimeError("Challenge verification required"))
        == "AUTH_REQUIRED"
    )
    assert classify_openreview_error(RuntimeError("429 rate limit")) == "RATE_LIMITED"
    assert classify_openreview_error(RuntimeError("Group Not Found")) == "INVALID_VENUE"


def test_submission_pages_use_after_cursor_and_exact_counts():
    from openreview_provider import OpenReviewProvider

    notes = [
        SimpleNamespace(
            id=f"note-{i}",
            forum=f"note-{i}",
            cdate=i,
            tmdate=i,
            readers=["everyone"],
            content={"title": {"value": f"Paper {i}"}},
            invitations=["Venue/2026/Conference/-/Submission"],
        )
        for i in range(1, 5)
    ]
    calls = []

    def get_notes(**kwargs):
        calls.append(kwargs)
        after = kwargs.get("after")
        if kwargs.get("with_count"):
            return notes[:2], len(notes)
        if after == "note-2":
            return notes[2:]
        return []

    client = SimpleNamespace(get_notes=get_notes)
    provider = OpenReviewProvider({"venue_id": "Venue/2026/Conference"}, client=client)
    pages = list(provider.iter_submission_pages(_capabilities(), page_size=2))

    assert [len(page.papers) for page in pages] == [2, 2]
    assert [page.cursor_after for page in pages] == ["note-2", "note-4"]
    assert [page.raw_count for page in pages] == [2, 2]
    assert calls[0]["with_count"] is True
    assert calls[1]["after"] == "note-2"
    assert calls[2]["after"] == "note-4"
